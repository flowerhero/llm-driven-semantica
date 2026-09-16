"""业务流程抽取（v4 · Rule-by-Contract）测试。

契约即规则架构下，宿主交卷五件套：entities + relations + attributes + rules
+ processes。本测试证明流程通道正确、兜底、可消费：

- processes → Process（extractor="llm.proc.v1"、kind/flow.type 独立枚举透传、
  process_id/step_id/flow_id 内容寻址）。
- kind 非法 → TASK 兜底；flow.type 非法 → SEQUENCE 兜底（不自创类型）。
- flows 下标越界 → 丢弃该 flow（LLM 下标不可信，Python 惰性校验）。
- 流程不入实体-边图：build_kg 仅在 metadata 登记 process_count。
- 流程与关系/属性/规则分界：顺序/阶段/流程性描述进 processes，纯事实/
  纯规范保持 relations/attributes/rules（五件套并行不互斥）。
- 全链路跑通、幂等、validate 通过、renderer 可渲染流程清单 Tab。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smini import build_pipeline, make_context  # noqa: E402
from smini.llm import StubLLMProvider  # noqa: E402
from smini.protocols import RunContext  # noqa: E402
from smini.steps.extract import ExtractStep  # noqa: E402
from smini.steps.build_kg import BuildKGStep  # noqa: E402
from smini.types import (  # noqa: E402
    EntityType,
    FlowType,
    NormalizedDocument,
    RuleModality,
    SourceRef,
    SourceType,
    StepKind,
)

_SAMPLE = (
    "证券经营机构应当对投资者履行适当性管理义务。"
    "首先，投资者填写《投资者基本信息表》和《投资者风险承受能力评估问卷》。"
    "然后，证券经营机构根据评估问卷对投资者的风险承受能力进行综合评估，"
    "确定其风险承受能力等级。"
    "评估完成后，证券经营机构应当匹配与其风险承受能力等级相适应的产品或服务。"
    "匹配成功后，向投资者进行风险揭示，并签署相关文件。"
    "最后，证券经营机构应当持续开展投资者回访。"
    "产品或服务风险等级由低至高至少划分为五级。"
)

# 宿主交卷：五件套
_LLM_EXTRACT = {
    "entities": [
        {"surface": "证券经营机构", "type": "ORGANIZATION", "canonical": "证券经营机构"},
        {"surface": "投资者", "type": "CONCEPT", "canonical": "投资者"},
        {"surface": "风险承受能力等级", "type": "CONCEPT", "canonical": "风险承受能力等级"},
        {"surface": "产品或服务风险等级", "type": "CONCEPT", "canonical": "产品或服务风险等级"},
    ],
    "relations": [
        {"subject": "证券经营机构", "predicate": "评估", "object": "风险承受能力等级",
         "object_type": "CONCEPT", "evidence": "对投资者的风险承受能力进行综合评估"},
    ],
    "attributes": [
        {"entity": "产品或服务风险等级", "name": "等级划分", "value": "五级",
         "value_type": "ENUM", "evidence": "产品或服务风险等级由低至高至少划分为五级"},
    ],
    "rules": [
        {"subject": "证券经营机构", "condition": "",
         "action": "应当对投资者履行适当性管理义务", "modality": "OBLIGATION",
         "evidence": "证券经营机构应当对投资者履行适当性管理义务"},
    ],
    "processes": [
        {
            "name": "投资者适当性管理流程",
            "description": "从了解客户到回访的完整适当性管理流程",
            "steps": [
                {"label": "填写《投资者基本信息表》和《投资者风险承受能力评估问卷》",
                 "kind": "TASK", "actor": "投资者",
                 "evidence": "投资者填写《投资者基本信息表》和《投资者风险承受能力评估问卷》"},
                {"label": "综合评估风险承受能力等级", "kind": "TASK",
                 "actor": "证券经营机构",
                 "evidence": "对投资者的风险承受能力进行综合评估"},
                {"label": "确定风险承受能力等级", "kind": "GATEWAY",
                 "actor": "证券经营机构", "evidence": "确定其风险承受能力等级"},
                {"label": "匹配产品或服务风险等级", "kind": "GATEWAY",
                 "actor": "证券经营机构",
                 "evidence": "匹配与其风险承受能力等级相适应的产品或服务"},
                {"label": "风险揭示并签署文件", "kind": "TASK",
                 "actor": "证券经营机构", "evidence": "进行风险揭示，并签署相关文件"},
                {"label": "持续开展投资者回访", "kind": "EVENT",
                 "actor": "证券经营机构", "evidence": "持续开展投资者回访"},
            ],
            "flows": [
                {"from": 0, "to": 1, "type": "SEQUENCE", "condition": "",
                 "evidence": "首先…然后"},
                {"from": 1, "to": 2, "type": "SEQUENCE", "condition": "", "evidence": ""},
                {"from": 2, "to": 3, "type": "CONDITIONAL", "condition": "评估完成后",
                 "evidence": "评估完成后，应当匹配"},
                {"from": 3, "to": 4, "type": "SEQUENCE", "condition": "", "evidence": ""},
                {"from": 4, "to": 5, "type": "SEQUENCE", "condition": "", "evidence": ""},
            ],
        }
    ],
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/process-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class ProcessExtractPathTest(unittest.TestCase):
    def test_processes_map_to_process_objects(self):
        """processes → Process：extractor、kind/flow.type 透传、ID 内容寻址。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(res.stats["mode"], "llm")
        self.assertEqual(res.stats["processes"], 1, "应统计 1 条流程")

        proc = res.processes[0]
        self.assertEqual(proc.name, "投资者适当性管理流程")
        self.assertEqual(proc.extractor, "llm.proc.v1")
        self.assertEqual(len(proc.steps), 6)
        self.assertEqual(len(proc.flows), 5)

        # step_id / flow_id 已补算，process_id 内容寻址
        self.assertTrue(proc.process_id.startswith("p_"))
        for st in proc.steps:
            self.assertTrue(st.step_id.startswith("s_"))
        for f in proc.flows:
            self.assertTrue(f.flow_id.startswith("f_"))

        # kind 独立枚举透传
        by_kind = {st.kind for st in proc.steps}
        self.assertEqual(by_kind, {StepKind.TASK, StepKind.GATEWAY, StepKind.EVENT})
        # flow.type 独立枚举透传
        types = {f.type for f in proc.flows}
        self.assertEqual(types, {FlowType.SEQUENCE, FlowType.CONDITIONAL})
        # CONDITIONAL 分支带 condition
        cond = [f for f in proc.flows if f.type == FlowType.CONDITIONAL][0]
        self.assertEqual(cond.condition, "评估完成后")
        self.assertEqual(cond.from_index, 2)
        self.assertEqual(cond.to_index, 3)

        # actor 用实体 canonical，不强建新实体
        self.assertEqual(proc.steps[1].actor, "证券经营机构")

    def test_kind_and_flow_type_invalid_fall_back(self):
        """kind 非法 → TASK 兜底；flow.type 非法 → SEQUENCE 兜底。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["processes"][0]["steps"].append(
            {"label": "异常步骤", "kind": "NOT_A_KIND", "actor": "", "evidence": ""})
        extract["processes"][0]["flows"].append(
            {"from": 0, "to": 1, "type": "NOT_A_FLOW", "condition": "", "evidence": ""})
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        proc = res.processes[0]
        weird_step = [s for s in proc.steps if s.label == "异常步骤"][0]
        self.assertEqual(weird_step.kind, StepKind.TASK, "非法 kind 应兜底 TASK")
        self.assertEqual(proc.flows[-1].type, FlowType.SEQUENCE, "非法 flow.type 应兜底 SEQUENCE")

    def test_out_of_bounds_flow_dropped(self):
        """flows 下标越界 → 丢弃该 flow（LLM 下标不可信）。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["processes"][0]["flows"].append(
            {"from": 99, "to": 100, "type": "SEQUENCE", "condition": "", "evidence": ""})
        extract["processes"][0]["flows"].append(
            {"from": -1, "to": 0, "type": "SEQUENCE", "condition": "", "evidence": ""})
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(len(res.processes[0].flows), 5,
                         "越界/负数下标的 flow 应被丢弃")

    def test_process_id_content_addressed_and_deduped(self):
        """process_id 内容寻址 + 同流程去重（幂等）。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res1 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        res2 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        ids1 = {p.process_id for p in res1.processes}
        ids2 = {p.process_id for p in res2.processes}
        self.assertEqual(ids1, ids2, "process_id 应内容寻址幂等")
        self.assertEqual(len(ids1), 1)

        # 同流程重复出现 → 合并为一条
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["processes"].append(json.loads(json.dumps(extract["processes"][0])))
        stub2 = _make_stub(_SAMPLE, extract)
        res3 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub2))[0]
        self.assertEqual(len(res3.processes), 1, "重复流程应按 process_id 合并")

    def test_processes_separate_from_others(self):
        """流程与关系/属性/规则分界：五件套并行不互斥。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        # 纯事实（证券经营机构 评估 风险承受能力等级）→ relations
        rel_rels = [r for r in res.relations if r.extractor == "llm.rel.v1"]
        self.assertEqual(len(rel_rels), 1)
        self.assertEqual(rel_rels[0].predicate, "评估")
        # 字面量事实（等级划分=五级）→ attributes
        attr_trips = [t for t in res.triplets if t.value_type is not None]
        self.assertEqual(len(attr_trips), 1)
        # 规范（应当履行义务）→ rules
        self.assertEqual(len(res.rules), 1)
        # 流程性描述 → processes
        self.assertEqual(len(res.processes), 1)
        self.assertEqual(res.stats["rules"], 1)
        self.assertEqual(res.stats["processes"], 1)


class ProcessBuildKGTest(unittest.TestCase):
    def test_processes_not_in_graph_but_counted_in_metadata(self):
        """流程不入实体-边图；build_kg 仅在 metadata 登记 process_count。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ex = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        graph = BuildKGStep().transform(ex, RunContext(llm=stub))

        # 图谱里没有流程名/步骤（流程是持续体，不是实体间事实）
        proc_names = {p for p in graph.stats.by_predicate if "流程" in p}
        self.assertEqual(proc_names, set(), "流程不应进入实体-边图谱")
        # metadata 登记 process_count
        self.assertEqual(graph.metadata.get("process_count"), 1)
        self.assertEqual(graph.metadata.get("rule_count"), 1)


class ProcessFullPipelineTest(unittest.TestCase):
    def test_full_pipeline_with_processes(self):
        """全链路：流程作为独立产出保留，不破坏原有链路。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = make_context([_SAMPLE])
        pipeline = build_pipeline([_SAMPLE], llm=stub)
        state, results = pipeline.run(ctx=ctx)
        self.assertEqual(len(results), 3)  # extract-only：ingest → extract → build_kg
        # 流程保留在 extraction 阶段
        self.assertEqual(state.extractions[0].stats["processes"], 1)
        self.assertEqual(len(state.extractions[0].processes), 1)
        # 图谱照常建出（流程不影响实体/边）
        self.assertGreaterEqual(len(state.graph.entities), 4)
        self.assertEqual(state.graph.metadata.get("process_count"), 1)


class ProcessValidateTest(unittest.TestCase):
    def test_validate_extract_with_processes(self):
        """validate extract：含 processes 的 04-extraction.json 通过校验。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_processes_validate"
        out_dir.mkdir(exist_ok=True)
        jp = out_dir / "04-extraction.json"
        jp.write_text(payload, encoding="utf-8")

        proc = subprocess.run(
            [sys.executable, "-m", "smini.cli", "validate", "extract",
             "--in", str(jp)],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"validate 应通过：{proc.stderr}")
        jp.unlink(missing_ok=True)
        out_dir.rmdir()

    def test_render_viewer_processes_tab(self):
        """render_viewer 对含 processes 的 04-extraction.json 渲染成功。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_processes_render"
        out_dir.mkdir(exist_ok=True)
        jp = out_dir / "04-extraction.json"
        jp.write_text(payload, encoding="utf-8")
        hp = out_dir / "04-extraction.html"

        script = Path(__file__).resolve().parent.parent / "skills" / "smini-extract" / "scripts" / "render_viewer.py"
        proc = subprocess.run(
            [sys.executable, str(script), "--in", str(jp), "--out", str(hp)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"render_viewer 应成功：{proc.stderr}")
        html = hp.read_text(encoding="utf-8")
        self.assertIn("流程清单", html, "HTML 应含流程清单 Tab")
        self.assertIn("投资者适当性管理流程", html, "HTML 应含流程名")
        self.assertIn("综合评估风险承受能力等级", html, "HTML 应含步骤 label")
        jp.unlink(missing_ok=True)
        hp.unlink(missing_ok=True)
        out_dir.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
