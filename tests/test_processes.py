"""业务流程抽取（v4 · Rule-by-Contract）测试。

契约即规则架构下，宿主交卷十一件套：entities + relations + attributes + rules
+ processes + states + functions + temporal + actions + constraints + permissions。
本测试证明流程通道正确、兜底、可消费：

- processes → ProcessDefinition（extractor="llm.pr.v1"、process_id/step_id
  内容寻址；步骤自动补 id；无步骤 → 丢弃并登记降级）。
- flows → 锚点命中的流向（跨 step 引用/自引用判定、命中/未命中分别统计）；
  step_id 列表未命中 → 降级，提示人工回流。
- process 内嵌子流程引用 sub_process_ref：无引用 → 无待办事项；
  有引用 → sub_process_ref 保留给宿主（待办事项触发宿主后续调用）。
- 流程不进实体-边图：build_kg 仅在 metadata 登记 process_count。
- 全链路跑通、幂等、validate 通过、renderer 渲染 3 个流程相关 Tab。
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
    NormalizedDocument,
    SourceRef,
    SourceType,
)

_SAMPLE = (
    "开户流程：客户提交开户申请，营业部审核资料，总部复核并开通账户，客户完成激活。"
    "若资料不全，营业部退回补充。"
    "销户流程：客户提交销户申请，营业部核查无未了结事项，总部复核后注销账户。"
)

# 宿主交卷：十一件套
_LLM_EXTRACT = {
    "entities": [
        {"surface": "客户", "type": "CONCEPT", "canonical": "客户"},
        {"surface": "营业部", "type": "ORGANIZATION", "canonical": "营业部"},
        {"surface": "总部", "type": "ORGANIZATION", "canonical": "总部"},
    ],
    "relations": [],
    "attributes": [],
    "rules": [],
    "processes": [
        {
            "name": "开户流程", "trigger": "客户提交开户申请",
            "steps": [
                {"name": "提交申请", "actor": "客户", "action": "提交开户申请",
                 "outcome": "申请已受理", "evidence": "客户提交开户申请"},
                {"name": "审核资料", "actor": "营业部", "action": "审核开户资料",
                 "outcome": "资料通过", "evidence": "营业部审核资料"},
                {"name": "复核开通", "actor": "总部", "action": "复核并开通账户",
                 "outcome": "账户已开通", "evidence": "总部复核并开通账户"},
                {"name": "完成激活", "actor": "客户", "action": "完成激活",
                 "outcome": "账户可正常使用", "evidence": "客户完成激活"},
            ],
            "flows": [
                {"from": 0, "to": 1, "label": "资料齐全", "evidence": ""},
                {"from": 1, "to": 2, "label": "资料通过", "evidence": ""},
                {"from": 2, "to": 3, "label": "已开通", "evidence": ""},
                {"from": 99, "to": 100, "label": "越界", "evidence": ""},
            ],
            "preconditions": ["客户具备有效身份证明"],
            "postconditions": ["账户处于激活状态"],
            "sub_process_ref": [],
            "evidence": "开户流程：客户提交开户申请，营业部审核资料，总部复核并开通账户，客户完成激活。若资料不全，营业部退回补充。",
        },
        {
            "name": "销户流程", "trigger": "客户提交销户申请",
            "steps": [
                {"name": "提交销户申请", "actor": "客户", "action": "提交销户申请",
                 "outcome": "申请已受理", "evidence": "客户提交销户申请"},
                {"name": "核查事项", "actor": "营业部", "action": "核查无未了结事项",
                 "outcome": "无未了结事项", "evidence": "营业部核查无未了结事项"},
                {"name": "复核注销", "actor": "总部", "action": "复核后注销账户",
                 "outcome": "账户已注销", "evidence": "总部复核后注销账户"},
            ],
            "flows": [
                {"from": 0, "to": 1, "label": "", "evidence": ""},
                {"from": 1, "to": 2, "label": "", "evidence": ""},
            ],
            "preconditions": [],
            "postconditions": ["账户已注销"],
            "sub_process_ref": ["开户流程"],
            "evidence": "销户流程：客户提交销户申请，营业部核查无未了结事项，总部复核后注销账户。",
        },
    ],
    "states": [],
    "functions": [],
    "temporal": [],
    "actions": [],
    "constraints": [],
    "permissions": [],
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/process-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class ProcessExtractPathTest(unittest.TestCase):
    def test_processes_map_to_definitions(self):
        """processes → ProcessDefinition：extractor、ID、步骤、flows、stats。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(res.stats["mode"], "llm")
        self.assertEqual(res.stats["processes"], 2)

        p0, p1 = res.processes[0], res.processes[1]
        # ID 内容寻址
        self.assertTrue(p0.process_id.startswith("pr_"))
        self.assertTrue(p0.steps[0].step_id.startswith("stp_"))
        # 基本信息
        self.assertEqual(p0.name, "开户流程")
        self.assertEqual(p0.trigger, "客户提交开户申请")
        self.assertEqual(len(p0.steps), 4)
        self.assertEqual(p0.steps[2].actor, "总部")
        self.assertEqual(p0.steps[2].action, "复核并开通账户")
        self.assertEqual(p0.extractor, "llm.pr.v1")
        # 前置/后置条件（v7 借鉴七模型）
        self.assertEqual(p0.preconditions, ["客户具备有效身份证明"])
        self.assertEqual(p0.postconditions, ["账户处于激活状态"])

    def test_flows_anchored_and_out_of_range_dropped(self):
        """flows：锚点命中为边；越界（from/to 超出 steps 下标）丢弃。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        p0 = res.processes[0]
        self.assertEqual(len(p0.flows), 3, "越界 flow 应被丢弃，仅剩 3 条有效")
        self.assertEqual([(f.from_index, f.to_index) for f in p0.flows],
                         [(0, 1), (1, 2), (2, 3)])
        self.assertEqual(p0.flows[0].label, "资料齐全")
        # 流程统计：命中 3 条、未命中 1 条（越界）
        self.assertEqual(res.stats["flow_hits"], 3)
        self.assertEqual(res.stats["flow_misses"], 1)

    def test_process_no_steps_dropped_with_degradation(self):
        """process 无 steps → 丢弃并登记降级，不产出空流程。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["processes"].append({"name": "空流程", "trigger": "无",
                                      "steps": [], "flows": [],
                                      "preconditions": [], "postconditions": [],
                                      "sub_process_ref": [], "evidence": ""})
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(len(res.processes), 2, "无步骤流程应被丢弃")
        self.assertTrue(any(
            d.component == "extract.process" and "无步骤" in d.reason
            for d in res.degradations
        ), "应登记无步骤降级")

    def test_sub_process_ref_passthrough(self):
        """sub_process_ref：无引用 → 无待办；有引用 → 透传给宿主（待办事项）。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        p0, p1 = res.processes[0], res.processes[1]
        self.assertEqual(p0.sub_process_ref, [], "开户流程无子流程引用")
        self.assertEqual(p1.sub_process_ref, ["开户流程"], "销户流程引用开户流程")
        self.assertEqual(res.stats.get("sub_process_pending", 0), 1,
                         "有待办子流程应计入待办事项")

    def test_ids_idempotent(self):
        """process_id / step_id 内容寻址幂等。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        r1 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        r2 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual({p.process_id for p in r1.processes},
                         {p.process_id for p in r2.processes})
        self.assertEqual({s.step_id for p in r1.processes for s in p.steps},
                         {s.step_id for p in r2.processes for s in p.steps})


class ProcessBuildKGTest(unittest.TestCase):
    def test_processes_not_in_graph_but_counted(self):
        """流程不进实体-边图；build_kg 在 metadata 登记 process_count。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ex = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        graph = BuildKGStep().transform(ex, RunContext(llm=stub))
        self.assertNotIn("开户流程", graph.stats.by_predicate,
                         "流程名不应进入实体-边图谱")
        self.assertEqual(graph.metadata.get("process_count"), 2)


class ProcessFullPipelineTest(unittest.TestCase):
    def test_full_pipeline_with_processes(self):
        """全链路：流程作为独立产出保留，不破坏原有链路。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = make_context([_SAMPLE])
        pipeline = build_pipeline([_SAMPLE], llm=stub)
        state, results = pipeline.run(ctx=ctx)
        self.assertEqual(len(results), 3)  # extract-only：ingest → extract → build_kg
        self.assertEqual(len(state.extractions[0].processes), 2)
        self.assertEqual(state.graph.metadata.get("process_count"), 2)


class ProcessValidateTest(unittest.TestCase):
    def test_validate_extract_with_processes(self):
        """validate extract：含流程的 04-extraction.json 通过校验。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_process_validate"
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

    def test_render_viewer_process_tabs(self):
        """render_viewer 对含流程的 04-extraction.json 渲染流程相关 Tab。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_process_render"
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
        for tab in ("业务流程", "流程清单", "状态机"):
            self.assertIn(tab, html, f"HTML 应含 {tab} Tab")
        self.assertIn("开户流程", html, "HTML 应含开户流程")
        self.assertIn("销户流程", html, "HTML 应含销户流程")
        self.assertIn("提交开户申请", html, "HTML 应含流程步骤")
        jp.unlink(missing_ok=True)
        hp.unlink(missing_ok=True)
        out_dir.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
