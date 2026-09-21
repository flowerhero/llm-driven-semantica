"""项目层 + 访谈控制器薄壳测试（访谈式本体构建 v1.1）。

覆盖：
- ProjectManager：create 幂等 / 目录清洗 / index 注册 / append_source 归并
  （锚点并集、同锚点更新、白名单差异登记冲突、covered 重算）/ rebuild / archive；
- InterviewController：会话创建（初始 model_draft = 项目累计模型）/
  append_turn（delta 合并、covered、history）/ 追加访谈（核心诉求：二次会话
  承接累计模型）/ finalize（host-contract + ExtractionResult + 渲染 + 项目归并）。

核心机制不变：薄壳只做确定性工作；契约格式与宿主契约完全一致；ID 由
ExtractStep 消费层补算；渲染复用 render_viewer.py。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from smini.steps.interview import InterviewController, merge_delta
from smini.steps.project import ProjectManager

_DOMAIN = "金融/适当性"

_SAMPLE_ANSWER_1 = {
    "entities": [
        {"surface": "证券经营机构", "canonical": "证券经营机构", "type": "ORGANIZATION"},
        {"surface": "投资者", "canonical": "投资者", "type": "CONCEPT"},
    ],
    "relations": [
        {"subject": "证券经营机构", "predicate": "对", "object": "投资者",
         "object_type": "CONCEPT", "evidence": "对投资者履行适当性管理义务",
         "prop_kind": "DEPENDENCY"},
    ],
    "rules": [
        {"subject": "证券经营机构", "condition": "投资者风险等级变化",
         "action": "书面风险警示并重新匹配", "modality": "OBLIGATION",
         "evidence": "应当书面风险警示并重新匹配"},
    ],
}

_SAMPLE_ANSWER_2 = {
    "entities": [
        {"surface": "投资者", "canonical": "投资者", "type": "CONCEPT"},
        {"surface": "客户经理", "canonical": "客户经理", "type": "PERSON"},
    ],
    "attributes": [
        {"entity": "投资者", "name": "风险等级", "value": "中高风险",
         "value_type": "ENUM", "evidence": "投资者风险承受能力等级"},
    ],
    "processes": [
        {"name": "适当性管理流程", "description": "评估-匹配-回访",
         "steps": [
             {"label": "评估风险承受能力", "kind": "TASK", "actor": "证券经营机构",
              "evidence": ""},
             {"label": "匹配适当产品", "kind": "GATEWAY", "actor": "",
              "evidence": ""},
         ],
         "flows": [
             {"from": 0, "to": 1, "type": "SEQUENCE", "condition": "", "evidence": ""},
         ]},
    ],
    "permissions": [
        {"actor": "客户经理", "action": "发起风险评估", "effect": "PERMIT",
         "scope": "", "role": "适当性管理岗", "actor_type": "HUMAN",
         "data_scope": "OWN", "evidence": "有权发起"},
    ],
}


class _Tmp:
    """临时项目根（每个测试独立，避免污染项目真实 projects/ 目录）。"""

    def __init__(self):
        self._d = tempfile.TemporaryDirectory()
        self.root = Path(self._d.name)

    def pm(self) -> ProjectManager:
        return ProjectManager(self.root)

    def ctl(self) -> InterviewController:
        return InterviewController(self.pm())

    def cleanup(self):
        self._d.cleanup()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.cleanup()


def _host_answer(question: str = "", **kw) -> dict:
    """宿主交卷样例：固定 delta + 无收尾。"""
    d = {"delta": kw.get("delta") or {}, "next_question": question,
         "closing": kw.get("closing", False), "note": kw.get("note", "")}
    return d


class ProjectManagerTest(unittest.TestCase):
    def test_create_idempotent_and_index(self):
        with _Tmp() as t:
            pm = t.pm()
            p1 = pm.create("适当性管理项目", domain=_DOMAIN)
            p2 = pm.create("适当性管理项目")     # 幂等：复用
            self.assertEqual(p1["project_id"], p2["project_id"])
            self.assertEqual(pm.list(), [{
                "project_id": p1["project_id"], "name": "适当性管理项目",
                "domain": _DOMAIN, "status": "ACTIVE",
                "updated_at": p1["updated_at"], "sources": 0,
            }])
            self.assertTrue(pm.project_path("适当性管理项目").exists())

    def test_safe_dirname(self):
        with _Tmp() as t:
            pm = t.pm()
            pm.create('项目/名:*?"<>|', domain="")
            self.assertTrue(pm.project_dir('项目/名:*?"<>|').exists())
            # 清洗后目录名
            self.assertEqual(pm.project_dir('项目/名:*?"<>|').name, "项目-名")

    def test_append_source_merge_and_conflict(self):
        with _Tmp() as t:
            pm = t.pm()
            pm.create("P", domain=_DOMAIN)
            a1 = {"entities": [
                {"surface": "机构", "canonical": "证券经营机构", "type": "ORGANIZATION"},
            ], "rules": [
                {"subject": "机构", "condition": "", "action": "应当尽责",
                 "modality": "OBLIGATION", "evidence": "应当"},
            ]}
            pm.append_source("P", "interviews/ivw_1", a1, kind="interview",
                             meta={"turns": 1})
            # 同锚点更新（entities：同一 canonical 不同 type → 冲突登记 + 取新）
            a2 = {"entities": [
                {"surface": "机构", "canonical": "证券经营机构", "type": "CONCEPT"},
            ], "rules": [
                {"subject": "机构", "condition": "", "action": "应当尽责",
                 "modality": "OBLIGATION", "evidence": "应当"},
            ]}
            pm.append_source("P", "interviews/ivw_2", a2, kind="interview",
                             meta={"turns": 2})
            p = pm.open("P")
            ents = p["consolidated"]["entities"]
            self.assertEqual(len(ents), 1)               # 并集：不重复
            self.assertEqual(ents[0]["type"], "CONCEPT")  # 同锚点 → 取最新
            self.assertEqual(p["covered"]["entities"], 1)
            self.assertEqual(p["covered"]["rules"], 1)
            # 白名单差异 → 冲突登记（unresolved）
            cf = [c for c in p["conflicts"] if not c["resolved"]]
            self.assertEqual(len(cf), 1)
            self.assertIn("entities", cf[0]["anchor"])
            self.assertIn("type", cf[0]["issue"])
            self.assertEqual(len(p["sources"]), 2)

    def test_rebuild_from_sources(self):
        with _Tmp() as t:
            pm = t.pm()
            pm.create("P")
            pm.append_source("P", "interviews/ivw_1",
                             {"entities": [{"surface": "A", "canonical": "A",
                                            "type": "CONCEPT"}]})
            before = pm.open("P")["consolidated"]
            rebuilt = pm.rebuild("P")
            self.assertEqual(rebuilt["consolidated"], before)
            self.assertEqual(rebuilt["covered"]["entities"], 1)

    def test_archive(self):
        with _Tmp() as t:
            pm = t.pm()
            pm.create("P")
            pm.archive("P")
            self.assertEqual(pm.open("P")["status"], "ARCHIVED")


class InterviewControllerTest(unittest.TestCase):
    def _run_two_turns(self, t: _Tmp, closing: bool = True):
        ctl = t.ctl()
        s1 = ctl.create_session("P", domain=_DOMAIN)
        self.assertEqual(s1["status"], "OPEN")
        self.assertEqual(s1["covered"]["entities"], 0)   # 新项目 → 空起步
        s2 = ctl.append_turn("P", "我们机构对投资者履行适当性义务。",
                             _host_answer("问题1？", delta=_SAMPLE_ANSWER_1))
        self.assertEqual(s2["turn"], 1)
        self.assertEqual(s2["covered"]["entities"], 2)
        self.assertEqual(s2["covered"]["rules"], 1)
        s3 = ctl.append_turn(
            "P", "投资者有风险等级，客户经理可以发起评估。",
            _host_answer("问题2？", delta=_SAMPLE_ANSWER_2, closing=closing))
        return ctl, s3

    def test_create_session_bootstrap(self):
        with _Tmp() as t:
            ctl, s3 = self._run_two_turns(t, closing=False)
            self.assertEqual(s3["turn"], 2)
            self.assertEqual(s3["covered"]["entities"], 3)     # 并集去重
            self.assertEqual(s3["covered"]["attributes"], 1)
            self.assertEqual(s3["covered"]["processes"], 1)
            self.assertEqual(s3["covered"]["permissions"], 1)
            self.assertEqual(len(s3["asked"]), 2)
            self.assertEqual(len(s3["history"]), 4)  # user+host × 2

    def test_append_session_inherits_consolidated(self):
        """核心诉求：项目建好后再次访谈 → 初始 model_draft = 项目累计模型。"""
        with _Tmp() as t:
            ctl = t.ctl()
            ctl.create_session("P", domain=_DOMAIN)
            ctl.append_turn("P", "答1", _host_answer("问题1？", delta=_SAMPLE_ANSWER_1))
            ctl.finalize("P")                       # 第一次访谈收尾 → 归并项目
            p = t.pm().open("P")
            self.assertEqual(p["sources"][0]["kind"], "interview")
            self.assertIsNone(p["active_session"])
            self.assertEqual(p["covered"]["entities"], 2)

            s2 = ctl.create_session("P")            # 第二次访谈（追加）
            self.assertEqual(s2["covered"]["entities"], 2)  # 初始即累计
            self.assertEqual(s2["model_draft"]["entities"],
                             p["consolidated"]["entities"])

    def test_merge_delta_update_semantics(self):
        """同锚点条目 → 更新而非追加。"""
        draft = {"entities": [{"surface": "机构", "canonical": "机构",
                               "type": "ORGANIZATION"}]}
        merged = merge_delta(draft, {"entities": [
            {"surface": "证券公司", "canonical": "机构", "type": "ORGANIZATION"},
        ]})
        self.assertEqual(len(merged["entities"]), 1)
        self.assertEqual(merged["entities"][0]["surface"], "证券公司")

    def test_finalize_artifacts_and_render(self):
        with _Tmp() as t:
            ctl, _ = self._run_two_turns(t, closing=True)   # 第二轮 closing → 自动收尾
            p = t.pm().open("P")
            self.assertIsNone(p["active_session"])
            self.assertEqual(p["sources"][0]["status"], "COMPLETE")
            self.assertEqual(p["covered"]["entities"], 3)
            sidir = t.root / "P" / "interviews" / p["sources"][0]["ref"].split("/")[1]
            for name in ("session.json", "host-contract.json",
                         "04-extraction.json", "04-extraction.html"):
                self.assertTrue((sidir / name).exists(), name)
            # host-contract 是宿主契约格式（十一件事键齐全）
            contract = json.loads((sidir / "host-contract.json").read_text(encoding="utf-8"))
            for k in ("entities", "relations", "attributes", "rules", "processes",
                      "states", "functions", "temporal", "actions", "constraints",
                      "permissions"):
                self.assertIn(k, contract)
            self.assertEqual(contract["document_meta"]["source"],
                             "interview://P")
            # ExtractionResult 落盘
            ex = json.loads((sidir / "04-extraction.json").read_text(encoding="utf-8"))
            self.assertTrue(ex["mentions"])
            # 项目总览渲染
            self.assertTrue((t.root / "P" / "04-extraction.html").exists())
            self.assertTrue((t.root / "P" / "host-contract.json").exists())

    def test_abort_keeps_trace(self):
        with _Tmp() as t:
            ctl = t.ctl()
            ctl.create_session("P")
            ctl.append_turn("P", "答", _host_answer("q", delta=_SAMPLE_ANSWER_1))
            s = ctl.abort("P")
            self.assertEqual(s["status"], "ABORTED")
            p = t.pm().open("P")
            self.assertIsNone(p["active_session"])

    def test_append_with_materials_archives_run(self):
        """混合模式材料归档：runs/<runid>/ 产物齐全 + document 来源登记 + 归并幂等。"""
        with _Tmp() as t:
            # 造一个材料目录（含两个文件）
            mat_dir = t.root / "材料A"
            mat_dir.mkdir()
            (mat_dir / "产品介绍.docx").write_text("材料内容A", encoding="utf-8")
            (mat_dir / "数据表.xlsx").write_text("材料内容B", encoding="utf-8")

            ctl = t.ctl()
            ctl.create_session("P", domain=_DOMAIN)
            s = ctl.append_turn(
                "P", "基于材料梳理了产品实体。",
                _host_answer("问题？", delta=_SAMPLE_ANSWER_1),
                materials=[mat_dir])

            # 会话 meta 记录 runid
            mr = (s.get("meta") or {}).get("material_runs")
            self.assertEqual(len(mr), 1)
            runid = mr[0]
            self.assertTrue(runid.startswith("run_"))
            run_dir = t.root / "P" / "runs" / runid

            # ① 材料原文归档（保留目录名）
            self.assertTrue((run_dir / "materials" / "材料A" / "产品介绍.docx").exists())
            self.assertTrue((run_dir / "materials" / "材料A" / "数据表.xlsx").exists())
            # ② 文档来源契约 + 薄壳映射 + 渲染 四件套
            for name in ("host-contract.json", "04-extraction.json",
                         "04-extraction.html"):
                self.assertTrue((run_dir / name).exists(), name)
            contract = json.loads(
                (run_dir / "host-contract.json").read_text(encoding="utf-8"))
            self.assertEqual(contract["document_meta"]["source"],
                             f"document://{runid}")
            self.assertEqual(len(contract["entities"]), 2)   # = 本轮 delta 快照
            ex = json.loads(
                (run_dir / "04-extraction.json").read_text(encoding="utf-8"))
            self.assertTrue(ex["mentions"])

            # ③ 项目登记 document 来源 + consolidated 并入
            p = t.pm().open("P")
            self.assertEqual(p["sources"][-1]["kind"], "document")
            self.assertEqual(p["sources"][-1]["ref"], f"runs/{runid}")
            self.assertEqual(p["covered"]["entities"], 2)

            # ④ rebuild 幂等（document 来源契约 = 访谈 delta 快照，并集无冲突）
            rebuilt = t.pm().rebuild("P")
            self.assertEqual(rebuilt["consolidated"], p["consolidated"])
            self.assertEqual(
                [c for c in rebuilt["conflicts"] if not c["resolved"]], [])

    def test_append_materials_missing_dir_raises(self):
        with _Tmp() as t:
            ctl = t.ctl()
            ctl.create_session("P")
            with self.assertRaises(Exception):
                ctl.append_turn("P", "答", _host_answer("q"),
                                materials=[t.root / "不存在"])

    def test_ingest_document_merges_without_dup(self):
        """项目模式文档抽取归并：与访谈来源同锚点 → 并集不重复。"""
        with _Tmp() as t:
            pm = t.pm()
            pm.create("P", domain=_DOMAIN)
            # 访谈来源已有同锚点实体
            pm.append_source("P", "interviews/ivw_1",
                             {"entities": [{"surface": "机构", "canonical": "机构",
                                            "type": "ORGANIZATION"}]})
            # 文档来源：同一 canonical + 新实体
            pm.append_source("P", "runs/run_abc", {
                "entities": [
                    {"surface": "证券公司", "canonical": "机构", "type": "ORGANIZATION"},
                    {"surface": "监管部门", "canonical": "监管部门", "type": "ORGANIZATION"},
                ]}, kind="document", meta={"doc_id": "run_abc"})
            p = pm.open("P")
            ents = p["consolidated"]["entities"]
            self.assertEqual(len(ents), 2)              # 并集：不重复
            self.assertEqual({e["canonical"] for e in ents}, {"机构", "监管部门"})
            self.assertEqual(p["covered"]["entities"], 2)
            self.assertEqual(len(p["sources"]), 2)      # 两个来源都登记
            self.assertEqual(p["sources"][1]["kind"], "document")
            # 同锚点实体 type 一致 → 无冲突登记
            self.assertEqual([c for c in p["conflicts"] if not c["resolved"]], [])


if __name__ == "__main__":
    unittest.main()
