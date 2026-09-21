"""smini.steps.project — 项目层薄壳（访谈式本体构建 v1.1）。

「项目」是跨来源（多次访谈 + 多份文档抽取）的持久化建模单元：
一个项目 = 一份累计本体模型（consolidated，宿主契约格式）+ 来源清单。

目录布局
--------
::

    projects/
    ├── index.json                  # 项目注册表（列出全部项目）
    └── <项目名>/
        ├── project.json            # 项目状态：累计模型 + 来源清单 + 冲突
        ├── interviews/<会话id>/    # 每次访谈（session.json + host-contract + 渲染）
        ├── runs/<runid>/           # 项目模式下的文档抽取（结构与现有 runs/ 一致）
        └── 04-extraction.html      # 项目累计模型总览渲染

归并规则（§5.3，确定性）
------------------------
- 各类目按锚点键并集；同锚点 → 保留最新来源值（时间戳靠后胜）+ 登记冲突
  （白名单语义字段差异才登记，surface/evidence 类不计入）；
- 冲突不静默覆盖：登记 unresolved，由宿主在后续访谈/抽取时优先裁决；
- covered 每轮重算；consolidated 可从 sources 清单重建（rebuild）。

本模块只做确定性文件/归并工作；提问、判定、覆盖度评估全部由宿主 LLM 声明式完成。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from ..ids import stable_id
from ..types import utcnow

#: 宿主契约的十一类（与 SKILL.md §契约一致）
CONTRACT_KINDS: tuple[str, ...] = (
    "entities", "relations", "attributes", "rules", "processes",
    "states", "functions", "temporal", "actions", "constraints", "permissions",
)

#: 各类目的锚点键（唯一身份，与 smini/ids.py 内容寻址语义一致）
ANCHOR_KEYS: dict[str, tuple[str, ...]] = {
    "entities": ("canonical",),
    "relations": ("subject", "predicate", "object"),
    "attributes": ("entity", "name"),
    "rules": ("subject", "condition", "action", "modality"),
    "processes": ("name",),
    "states": ("object", "name"),
    "functions": ("name", "subject", "formula"),
    "temporal": ("subject", "kind", "value"),
    "actions": ("name", "actor", "level", "trigger"),
    "constraints": ("subject", "type", "description"),
    "permissions": ("actor", "action", "effect"),
}

#: 各类目参与冲突判定的语义字段（surface/evidence/text 类不计入，
#: 避免"同一实体不同写法"被误报为冲突）
SEMANTIC_FIELDS: dict[str, tuple[str, ...]] = {
    "entities": ("type",),
    "relations": ("object_type", "prop_kind", "strength"),
    "attributes": ("value_type", "required"),
    "rules": ("modality", "rule_type", "output_type", "certainty"),
    "processes": ("description", "flow_type", "approval_chain"),
    "states": (),
    "functions": ("output_type",),
    "temporal": ("anchor",),
    "actions": ("level", "side_effect", "target"),
    "constraints": ("type",),
    "permissions": ("effect", "scope", "role", "actor_type", "data_scope"),
}

#: 项目目录名中需要清洗掉的字符（跨平台安全：/ : * ? " < > | 与控制字符）
_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _safe_dirname(name: str) -> str:
    """把项目名清洗成安全目录名。空名 → 「未命名项目」。"""
    cleaned = _UNSAFE.sub("-", (name or "").strip())
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    return cleaned or "未命名项目"


def _anchor_value(item: dict, kind: str) -> tuple:
    """取条目锚点键的值（缺失补空串，保证可哈希、可比较）。"""
    return tuple(str(item.get(f) or "") for f in ANCHOR_KEYS[kind])


def _semantic_delta(old: dict, new: dict, kind: str) -> list[str]:
    """同一锚点下，白名单语义字段的差异清单（空 = 无冲突）。"""
    diffs: list[str] = []
    for f in SEMANTIC_FIELDS[kind]:
        a, b = old.get(f), new.get(f)
        if a != b:
            diffs.append(f"{f}: {a!r} → {b!r}")
    return diffs


def _new_project(name: str, domain: str = "", goal: str = "") -> dict:
    now = utcnow().isoformat(timespec="seconds")
    return {
        "project_id": stable_id("project", name, now, prefix="pv_"),
        "name": name,
        "domain": domain,
        "goal": goal,
        "status": "ACTIVE",
        "created_at": now,
        "updated_at": now,
        "active_session": None,
        "sources": [],
        "consolidated": {k: [] for k in CONTRACT_KINDS},
        "covered": {k: 0 for k in CONTRACT_KINDS},
        "conflicts": [],
    }


def _count_covered(contract: dict) -> dict[str, int]:
    return {k: len(contract.get(k) or []) for k in CONTRACT_KINDS}


class ProjectManager:
    """项目生命周期 + 来源归并（确定性薄壳）。"""

    def __init__(self, base_dir: str | Path = "projects") -> None:
        self.root = Path(base_dir)

    # ------------------------------------------------------------------ 注册表
    def index_path(self) -> Path:
        return self.root / "index.json"

    def _load_index(self) -> dict:
        if not self.index_path().exists():
            return {"projects": []}
        try:
            return json.loads(self.index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"projects": []}

    def _save_index(self, idx: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path().write_text(
            json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")

    def _sync_index(self, name: str, project: dict) -> None:
        idx = self._load_index()
        entry = {
            "project_id": project["project_id"],
            "name": name,
            "domain": project.get("domain", ""),
            "status": project.get("status", "ACTIVE"),
            "updated_at": project.get("updated_at", ""),
            "sources": len(project.get("sources") or []),
        }
        idx["projects"] = [e for e in idx["projects"] if e["name"] != name]
        idx["projects"].append(entry)
        idx["projects"].sort(key=lambda e: e["name"])
        self._save_index(idx)

    # ------------------------------------------------------------------ 目录
    def project_dir(self, name: str) -> Path:
        return self.root / _safe_dirname(name)

    def project_path(self, name: str) -> Path:
        return self.project_dir(name) / "project.json"

    # ------------------------------------------------------------------ 生命周期
    def create(self, name: str, domain: str = "", goal: str = "") -> dict:
        """创建项目（幂等：已存在则直接打开）。"""
        name = (name or "").strip() or "未命名项目"
        d = self.project_dir(name)
        p = self.project_path(name)
        if p.exists():
            return self.open(name)
        d.mkdir(parents=True, exist_ok=True)
        project = _new_project(name, domain, goal)
        p.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        self._sync_index(name, project)
        return project

    def open(self, name: str) -> dict:
        p = self.project_path(name)
        if not p.exists():
            raise FileNotFoundError(f"项目不存在：{name}（可用 project create 创建）")
        return json.loads(p.read_text(encoding="utf-8"))

    def save(self, name: str, project: dict) -> dict:
        project["updated_at"] = utcnow().isoformat(timespec="seconds")
        self.project_path(name).write_text(
            json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        self._sync_index(name, project)
        return project

    def list(self) -> list[dict]:
        return self._load_index()["projects"]

    def archive(self, name: str) -> dict:
        project = self.open(name)
        project["status"] = "ARCHIVED"
        return self.save(name, project)

    # ------------------------------------------------------------------ 归并
    def append_source(
        self,
        name: str,
        ref: str,
        contract: dict,
        kind: str = "interview",
        meta: dict | None = None,
    ) -> dict:
        """把来源契约归并进项目 consolidated（§5.3）。

        Args:
            name: 项目名。
            ref: 来源引用，如 ``interviews/ivw_xxx`` / ``runs/run_xxx``。
            contract: 宿主契约格式（十一件事 + 可选 document_meta）。
            kind: 来源类型（interview / document）。
            meta: 来源附加信息（如 turns / doc_id / status）。
        """
        project = self.open(name)
        merged, new_conflicts = _merge_contract(
            project["consolidated"], contract, source_ref=ref,
            existing_sources=[s["ref"] for s in project["sources"]],
        )
        project["consolidated"] = merged
        project["covered"] = _count_covered(merged)
        project["conflicts"].extend(new_conflicts)
        now = utcnow().isoformat(timespec="seconds")
        project["sources"].append({
            "kind": kind, "ref": ref, "updated_at": now, **(meta or {}),
        })
        # 来源契约落盘（<项目>/<ref>/host-contract.json）——rebuild 的重建依据；
        # 若调用方已写过同路径文件则幂等覆盖（内容一致）
        src_dir = self.project_dir(name) / ref
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "host-contract.json").write_text(
            json.dumps(contract, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.save(name, project)

    def rebuild(self, name: str) -> dict:
        """从 sources 清单重算 consolidated（校验用：应等于当前 consolidated）。"""
        project = self.open(name)
        base: dict = {k: [] for k in CONTRACT_KINDS}
        for s in project["sources"]:
            src = self.root / _safe_dirname(name) / f"{s['ref']}/host-contract.json"
            if not src.exists():
                raise FileNotFoundError(f"来源产物缺失：{src}")
            contract = json.loads(src.read_text(encoding="utf-8"))
            base, _ = _merge_contract(base, contract, source_ref=s["ref"], existing_sources=[])
        project["consolidated"] = base
        project["covered"] = _count_covered(base)
        return self.save(name, project)

    def consolidated(self, name: str) -> dict:
        """取项目累计模型（宿主契约格式）。"""
        return dict(self.open(name)["consolidated"])

    # ------------------------------------------------------------------ 辅助
    def ensure_session_dir(self, name: str, session_id: str) -> Path:
        d = self.project_dir(name) / "interviews" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d


def _merge_contract(
    base: dict,
    incoming: dict,
    source_ref: str,
    existing_sources: list[str],
) -> tuple[dict, list[dict]]:
    """把一份宿主契约归并进累计模型。

    返回 (新累计模型, 新增冲突清单)。同锚点条目以 incoming 为准整条替换；
    白名单语义字段有差异时登记冲突（unresolved），不静默覆盖。
    """
    out: dict = {k: list(base.get(k) or []) for k in CONTRACT_KINDS}
    conflicts: list[dict] = []

    for kind in CONTRACT_KINDS:
        items = incoming.get(kind) or []
        if not items:
            continue
        by_anchor: dict[tuple, dict] = {}
        order: list[tuple] = []
        for it in out[kind]:
            key = _anchor_value(it, kind)
            by_anchor[key] = it
            order.append(key)
        for it in items:
            key = _anchor_value(it, kind)
            if key in by_anchor:
                old = by_anchor[key]
                diffs = _semantic_delta(old, it, kind)
                if diffs and existing_sources:
                    conflicts.append({
                        "anchor": f"{kind}:{'|'.join(key)}",
                        "between": [existing_sources[-1], source_ref],
                        "issue": "两来源对同一锚点描述不一致：" + "；".join(diffs),
                        "resolved": False,
                    })
                by_anchor[key] = it  # 同锚点 → 以最新来源为准
            else:
                by_anchor[key] = it
                order.append(key)
        out[kind] = [by_anchor[k] for k in order]

    # document_meta：来源累计模型以首个非空 domain 为参考，不强制覆盖
    dm = base.get("document_meta") or {}
    if not dm.get("domain") and (incoming.get("document_meta") or {}).get("domain"):
        dm["domain"] = incoming["document_meta"]["domain"]
    if dm:
        out["document_meta"] = dm

    return out, conflicts
