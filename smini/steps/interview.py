"""smini.steps.interview — 访谈控制器薄壳（访谈式本体构建 v1.1）。

访谈 = 信息源替换：以多轮对话为输入，逐步构建/完善本体模型。
会话永远挂靠项目（§5.4）：初始 model_draft = 项目累计模型（consolidated），
收尾后归并回项目 —— 这就是「项目建立后随时可追加访谈」的落盘保证。

宿主-薄壳接缝（每轮）
--------------------
- 薄壳向宿主提供：会话摘要（covered / canonical 清单 / agenda / 未决冲突 / 最近历史）；
- 宿主交卷：:

    {
      "delta": {十一件事的**子集**，只含本轮新增/修改条目},
      "next_question": str,   # 下一个问题（null/空 = 不提问）
      "closing": bool,        # true = 请求收尾
      "note": str             # 给用户看的进展说明（如「已记录 2 个实体」）
    }

- 薄壳只做确定性工作：合并 delta（锚点键并集/更新）、更新 covered/history、
  落盘 session.json、收尾时走既有薄壳（inject → ExtractStep → validate → 渲染 → 归并项目）。

本模块不生成问题、不判定收尾、不评估覆盖度 —— 全部由宿主 LLM 声明式完成。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ids import stable_id
from ..types import SourceRef, SourceType, NormalizedDocument, to_dict, utcnow
from .extract import ExtractStep
from .project import CONTRACT_KINDS, _count_covered, ProjectManager

#: 收尾时注入 ExtractStep 的虚拟文档 ID 前缀
_IV_DOC_PREFIX = "ivd_"

#: 每轮宿主交卷的 delta 允许出现的键
_DELTA_KEYS = set(CONTRACT_KINDS)


class InterviewError(RuntimeError):
    """访谈控制器错误（会话不存在 / 状态非法 / 宿主交卷缺失等）。"""


def _new_session(
    project_name: str,
    project_ref: str,
    domain: str,
    goal: str,
    initial_draft: dict,
) -> dict:
    now = utcnow().isoformat(timespec="seconds")
    session_id = stable_id("iv", project_name, now, prefix="ivw_")
    return {
        "session_id": session_id,
        "project": project_name,
        "project_ref": project_ref,
        "domain": domain,
        "goal": goal,
        "status": "OPEN",
        "turn": 0,
        "model_draft": dict(initial_draft),
        "covered": _count_covered(initial_draft),
        "agenda": [],
        "asked": [],
        "history": [],
        "conflicts": [],
        "meta": {"created_at": now, "updated_at": now, "source": "interview"},
    }


def _anchor(item: dict, kind: str) -> tuple:
    """取宿主契约条目的锚点键（与 project.ANCHOR_KEYS 一致）。"""
    from .project import ANCHOR_KEYS
    return tuple(str(item.get(f) or "") for f in ANCHOR_KEYS[kind])


def merge_delta(model_draft: dict, delta: dict) -> dict:
    """把宿主本轮交卷的 delta 合并进 model_draft（锚点键并集/更新）。

    - 只处理十一类；同锚点 → 整条替换为 delta 版本（更新语义）；
    - 枚举校验/ID 补算不在本层做 —— 收尾时由 ExtractStep 薄壳统一兜底；
    - document_meta 由薄壳在收尾时强制生成 source，宿主可交卷 domain 等。
    """
    out: dict = {k: list(model_draft.get(k) or []) for k in CONTRACT_KINDS}
    for kind in CONTRACT_KINDS:
        items = delta.get(kind) or []
        if not items:
            continue
        by_anchor: dict[tuple, dict] = {}
        order: list[tuple] = []
        for it in out[kind]:
            key = _anchor(it, kind)
            by_anchor[key] = it
            order.append(key)
        for it in items:
            key = _anchor(it, kind)
            if key not in by_anchor:
                order.append(key)
            by_anchor[key] = it
        out[kind] = [by_anchor[k] for k in order]
    return out


class InterviewController:
    """访谈控制器：会话生命周期 + delta 合并 + 收尾接线（确定性薄壳）。"""

    def __init__(self, projects: ProjectManager | None = None) -> None:
        self.pm = projects or ProjectManager()

    # ------------------------------------------------------------------ 会话生命周期
    def create_session(
        self,
        project_name: str,
        domain: str = "",
        goal: str = "",
        *,
        force_new: bool = False,
    ) -> dict:
        """为项目开一场访谈会话。

        - 项目不存在 → 自动创建（访谈强制挂靠项目）；
        - 项目已有 OPEN 会话且未 force_new → 返回现有会话（恢复语义）；
        - 会话初始 model_draft = 项目累计模型（§5.4 追加访谈的落盘保证）。
        """
        project = self.pm.create(project_name, domain=domain, goal=goal)
        if not force_new and project.get("active_session"):
            sid = project["active_session"]
            s = self._read_session(project_name, sid)
            if s is not None:
                return s
        # 新建会话
        ref = self.pm.project_dir(project_name).name
        session = _new_session(
            project["name"], ref, project.get("domain") or domain,
            project.get("goal") or goal, self.pm.consolidated(project["name"]))
        sid = session["session_id"]
        self._write_session(project_name, sid, session)
        project["active_session"] = sid
        self.pm.save(project_name, project)
        return session

    def open_session(self, project_name: str) -> dict:
        """打开项目当前会话：有 OPEN 则恢复，否则新建。"""
        return self.create_session(project_name)

    def _session_dir(self, project_name: str, session_id: str) -> Path:
        return self.pm.project_dir(project_name) / "interviews" / session_id

    def _session_path(self, project_name: str, session_id: str) -> Path:
        return self._session_dir(project_name, session_id) / "session.json"

    def _read_session(self, project_name: str, session_id: str) -> dict | None:
        p = self._session_path(project_name, session_id)
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))

    def _write_session(self, project_name: str, session_id: str, session: dict) -> None:
        d = self.pm.ensure_session_dir(project_name, session_id)
        session["meta"]["updated_at"] = utcnow().isoformat(timespec="seconds")
        (d / "session.json").write_text(
            json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ 每轮推进
    def append_turn(
        self,
        project_name: str,
        user_text: str,
        host: dict,
        materials: list[str | Path] | None = None,
    ) -> dict:
        """推进一轮访谈。

        Args:
            project_name: 项目名。
            user_text: 用户本轮回答（原话，写入 history）。
            host: 宿主交卷 {delta?, next_question?, closing?, note?}。
            materials: 本轮混合模式读取的材料目录列表（可选，可多次）。
                薄壳把原文归档到 runs/<runid>/materials/、以本轮 delta 快照作为
                该文档来源的抽取契约（host-contract + 04-extraction + html），
                并 append_source(kind="document") 登记来源（项目模式文档抽取路径）。
        """
        project = self.pm.open(project_name)
        sid = project.get("active_session")
        if not sid:
            raise InterviewError(f"项目「{project_name}」无进行中的访谈会话")
        session = self._read_session(project_name, sid)
        if session is None:
            raise InterviewError(f"会话缺失：{sid}")
        if session["status"] != "OPEN":
            raise InterviewError(f"会话已结束（status={session['status']}），无法继续追加")

        delta = dict(host.get("delta") or {})
        question = str(host.get("next_question") or "").strip()
        note = str(host.get("note") or "").strip()
        closing = bool(host.get("closing"))

        # 混合模式材料归档（先于 delta 合并：本轮 delta 快照 = 该文档来源的抽取契约）
        material_runids: list[str] = []
        for mdir in (materials or []):
            material_runids.append(
                self._attach_materials(project_name, sid, mdir, delta))
        if material_runids:
            runs = list(session.get("meta", {}).get("material_runs") or [])
            session["meta"]["material_runs"] = runs + material_runids

        # 合并 delta（宿主契约子集 → model_draft 锚点并集/更新）
        if delta:
            session["model_draft"] = merge_delta(session["model_draft"], delta)
        session["covered"] = _count_covered(session["model_draft"])
        session["history"].append({"role": "user", "text": user_text})
        if delta:
            session["history"].append({"role": "host", "text": note, "delta": delta})
        elif note:
            session["history"].append({"role": "host", "text": note})
        if question:
            session["asked"].append(question)
        session["turn"] += 1

        if closing:
            session["status"] = "CLOSING"

        self._write_session(project_name, sid, session)
        if closing:
            self.finalize(project_name)
            session = self._read_session(project_name, sid)
        return session

    # ------------------------------------------------------------------ 混合模式材料归档
    def _unique_runid(self, project_name: str) -> str:
        """生成项目内唯一的 runid：run_<时间戳>；已存在则追加序号。"""
        base = f"run_{utcnow().strftime('%Y%m%d_%H%M%S')}"
        runid = base
        i = 2
        while (self.pm.project_dir(project_name) / "runs" / runid).exists():
            runid = f"{base}_{i}"
            i += 1
        return runid

    def _attach_materials(
        self,
        project_name: str,
        session_id: str,
        materials_dir: str | Path,
        delta: dict,
    ) -> str:
        """把本轮混合模式读取的材料目录归档为项目 runs/<runid>/ 文档来源。

        产物（projects/<项目名>/runs/<runid>/）：
          materials/            材料原文档案（保留目录名，可审计全文）
          host-contract.json    本轮 delta 快照 = 该文档来源的抽取契约
          04-extraction.json    薄壳映射后的 ExtractionResult（同一 ExtractStep）
          04-extraction.html    渲染查看器
        随后 append_source(kind="document") 归并进项目 consolidated 并登记来源
        （锚点并集，与访谈 finalize 的 interview 来源归并幂等安全）。

        Returns:
            runid（如 run_20260921_113000）。
        """
        src = Path(materials_dir)
        if not src.is_dir():
            raise InterviewError(f"材料目录不存在：{src}")
        runid = self._unique_runid(project_name)
        run_dir = self.pm.project_dir(project_name) / "runs" / runid
        (run_dir / "materials").mkdir(parents=True, exist_ok=True)

        # 1) 原文归档（保留目录名，避免同名文件冲突）
        shutil.copytree(src, run_dir / "materials" / src.name, dirs_exist_ok=True)

        # 2) 本轮 delta 快照 = 该文档来源的抽取契约（宿主契约格式，可审计/rebuild）
        contract = {k: list(delta.get(k) or []) for k in CONTRACT_KINDS}
        contract["document_meta"] = {
            "source": f"document://{runid}",
            "doc_type": "materials",
        }
        (run_dir / "host-contract.json").write_text(
            json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

        # 3) 薄壳映射 → 04-extraction.json（与收尾同一 ExtractStep 路径）
        from ..llm import HostAgentLLMProvider
        nd = NormalizedDocument(
            doc_id=f"{_IV_DOC_PREFIX}{runid}",
            source=SourceRef.of(f"document://{runid}", SourceType.TEXT,
                                checksum="materials"),
            text="",
        )
        llm = HostAgentLLMProvider(contract=contract)
        from ..protocols import RunContext
        res = ExtractStep().transform([nd], RunContext(llm=llm))[0]
        (run_dir / "04-extraction.json").write_text(
            json.dumps(to_dict(res), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

        # 4) 渲染查看器（复用 render_viewer.py）
        self._render(run_dir / "host-contract.json", run_dir / "04-extraction.html")

        # 5) 归并进项目（kind=document；锚点并集与 interview 来源幂等安全）
        self.pm.append_source(
            project_name, f"runs/{runid}", contract, kind="document",
            meta={"doc_id": runid, "materials_dir": str(src),
                  "session": session_id})
        return runid

    # ------------------------------------------------------------------ 收尾接线
    def finalize(self, project_name: str) -> dict:
        """收尾：model_draft → 既有薄壳（inject → ExtractStep）→ 落盘 → 归并项目。

        产物（projects/<项目名>/interviews/<sid>/）：
          host-contract.json  宿主契约（十一件事 + document_meta.source=interview://）
          04-extraction.json  薄壳映射后的 ExtractionResult
          04-extraction.html  渲染查看器
        随后归并进项目 consolidated，并刷新项目总览渲染。
        """
        project = self.pm.open(project_name)
        sid = project.get("active_session")
        if not sid:
            raise InterviewError(f"项目「{project_name}」无进行中的访谈会话")
        session = self._read_session(project_name, sid)
        if session is None:
            raise InterviewError(f"会话缺失：{sid}")
        if session["status"] == "COMPLETE":
            return session

        contract = dict(session["model_draft"])
        contract["document_meta"] = dict(contract.get("document_meta") or {})
        contract["document_meta"].setdefault("source", f"interview://{project_name}")

        sdir = self._session_dir(project_name, sid)
        sdir.mkdir(parents=True, exist_ok=True)
        # 1) host-contract.json（宿主契约原样，可审计）
        (sdir / "host-contract.json").write_text(
            json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")

        # 2) 既有薄壳：虚拟文档 + HostAgentLLMProvider → ExtractStep
        from ..llm import HostAgentLLMProvider
        nd = NormalizedDocument(
            doc_id=f"{_IV_DOC_PREFIX}{sid}",
            source=SourceRef.of(f"interview://{project_name}", SourceType.TEXT,
                                checksum="interview"),
            text="",
        )
        llm = HostAgentLLMProvider(contract=contract)
        from ..protocols import RunContext
        res = ExtractStep().transform([nd], RunContext(llm=llm))[0]
        (sdir / "04-extraction.json").write_text(
            json.dumps(to_dict(res), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

        # 3) 渲染查看器（复用 render_viewer.py，宿主契约格式直接支持）
        self._render(sdir / "host-contract.json", sdir / "04-extraction.html")

        # 4) 归并进项目
        self.pm.append_source(
            project_name, f"interviews/{sid}", contract,
            kind="interview",
            meta={"turns": session["turn"], "status": "COMPLETE"})
        project = self.pm.open(project_name)
        project["active_session"] = None
        self.pm.save(project_name, project)

        # 5) 项目累计模型总览渲染
        self._render_project_overview(project_name)

        session["status"] = "COMPLETE"
        session["meta"]["completed_at"] = utcnow().isoformat(timespec="seconds")
        self._write_session(project_name, sid, session)
        return session

    def abort(self, project_name: str) -> dict:
        """中止当前会话（保留轨迹，清 active_session）。"""
        project = self.pm.open(project_name)
        sid = project.get("active_session")
        if sid:
            session = self._read_session(project_name, sid)
            if session is not None and session["status"] == "OPEN":
                session["status"] = "ABORTED"
                self._write_session(project_name, sid, session)
            project["active_session"] = None
            self.pm.save(project_name, project)
            return session
        raise InterviewError(f"项目「{project_name}」无进行中的会话")

    # ------------------------------------------------------------------ 渲染
    def _renderer_script(self) -> Path:
        root = Path(__file__).resolve().parent.parent.parent
        return root / "skills/smini-extract/scripts/render_viewer.py"

    def _render(self, inp: Path, out: Path) -> None:
        script = self._renderer_script()
        if not script.exists():
            return  # 渲染器缺失不阻断收尾（数据产物已落盘）
        proc = subprocess.run(
            [sys.executable, str(script), "--in", str(inp), "--out", str(out)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise InterviewError(f"渲染失败：{proc.stderr}")

    def _render_project_overview(self, project_name: str) -> None:
        """把项目 consolidated 渲染为 projects/<名>/04-extraction.html 总览。"""
        pdir = self.pm.project_dir(project_name)
        contract = self.pm.consolidated(project_name)
        contract["document_meta"] = dict(contract.get("document_meta") or {})
        contract["document_meta"].setdefault("source", f"project://{project_name}")
        (pdir / "host-contract.json").write_text(
            json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        self._render(pdir / "host-contract.json", pdir / "04-extraction.html")

    # ------------------------------------------------------------------ 摘要
    def summary(self, project_name: str) -> dict:
        """给宿主的会话状态摘要：covered / 关键 canonical / agenda / 未决冲突。"""
        project = self.pm.open(project_name)
        sid = project.get("active_session")
        session = None
        if sid:
            session = self._read_session(project_name, sid)
        consolidated = self.pm.consolidated(project_name)
        canonicals = [e.get("canonical") for e in consolidated.get("entities") or []]
        unresolved = [c for c in project.get("conflicts") or [] if not c.get("resolved")]
        return {
            "project": project_name,
            "domain": project.get("domain", ""),
            "session": sid,
            "status": session["status"] if session else None,
            "turn": session["turn"] if session else 0,
            "covered": session["covered"] if session else _count_covered(consolidated),
            "canonicals": canonicals,
            "agenda": session["agenda"] if session else [],
            "conflicts_unresolved": unresolved,
        }
