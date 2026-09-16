"""预测决策本体抽取（v6 · Rule-by-Contract）测试。

契约即规则架构下，宿主交卷十一件套：entities + relations + attributes + rules
+ processes + states + functions + temporal + actions + constraints + permissions。
本测试证明预测决策六件套通道正确、兜底、可消费：

- states → StateMachine（extractor="llm.sm.v1"、sm_id/state_id/transition_id
  内容寻址；transitions 下标越界 → 丢弃；initial 标记透传）。
- functions → Function（extractor="llm.fn.v1"、output_type 独立枚举、非法 → OTHER）。
- temporal → Temporal（extractor="llm.tp.v1"、kind 独立枚举、非法 → VALIDITY）。
- actions → Action（extractor="llm.ac.v1"、level 独立枚举、非法 → OBSERVE）。
- constraints → Constraint（extractor="llm.cn.v1"、type 独立枚举、非法 → CONSISTENCY）。
- permissions → Permission（extractor="llm.pm.v1"、effect 独立枚举、非法 → DENY）。
- relations 的 prop_kind/strength 透传（独立 4 类枚举，非法 → 空=普通事实）。
- 六件套均不入实体-边图：build_kg 仅在 metadata 登记各自 count。
- 全链路跑通、幂等、validate 通过、renderer 渲染 6 个新 Tab。
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
    ActionLevel,
    ConstraintType,
    FunctionOutputType,
    NormalizedDocument,
    PermissionEffect,
    SourceRef,
    SourceType,
    TemporalKind,
)

_SAMPLE = (
    "证券经营机构应当对投资者履行适当性管理义务，不得向风险承受能力等级不匹配的"
    "投资者销售产品。"
    "普通投资者可以申请转化成为专业投资者，专业投资者可以购买所有风险等级的产品或服务。"
    "风险承受能力等级由低至高至少划分为五级，自2017年7月1日起实施。"
    "证券经营机构应当每年对投资者风险承受能力进行一次评估。"
    "当投资者风险承受能力等级发生变化时，证券经营机构应当书面风险警示并重新匹配产品或服务。"
    "压力函数 = 风险承受能力 ÷ 产品或服务风险等级。"
)

# 宿主交卷：十一件套
_LLM_EXTRACT = {
    "entities": [
        {"surface": "证券经营机构", "type": "ORGANIZATION", "canonical": "证券经营机构"},
        {"surface": "投资者", "type": "CONCEPT", "canonical": "投资者"},
        {"surface": "普通投资者", "type": "CONCEPT", "canonical": "普通投资者"},
        {"surface": "专业投资者", "type": "CONCEPT", "canonical": "专业投资者"},
        {"surface": "风险承受能力等级", "type": "FINANCIAL_INDICATOR", "canonical": "风险承受能力等级"},
        {"surface": "产品或服务风险等级", "type": "FINANCIAL_INDICATOR", "canonical": "产品或服务风险等级"},
    ],
    "relations": [
        {"subject": "证券经营机构", "predicate": "销售", "object": "产品或服务风险等级",
         "object_type": "FINANCIAL_INDICATOR", "evidence": "向投资者销售产品",
         "prop_kind": "TRIGGER", "strength": "HIGH"},
    ],
    "attributes": [],
    "rules": [
        {"subject": "证券经营机构", "condition": "向投资者销售金融产品",
         "action": "应当履行适当性义务", "modality": "OBLIGATION",
         "evidence": "应当对投资者履行适当性管理义务"},
    ],
    "processes": [],
    "states": [
        {
            "object": "投资者", "name": "风险承受能力等级状态机",
            "states": [
                {"label": "低风险", "initial": True,
                 "evidence": "风险承受能力等级由低至高至少划分为五级"},
                {"label": "中风险", "initial": False, "evidence": ""},
                {"label": "高风险", "initial": False, "evidence": ""},
            ],
            "transitions": [
                {"from": 0, "to": 1, "event": "评估结果变化", "condition": "得分提高",
                 "action": "调整等级", "evidence": "风险承受能力等级发生变化"},
                {"from": 1, "to": 2, "event": "", "condition": "", "action": "",
                 "evidence": ""},
                {"from": 99, "to": 100, "event": "", "condition": "", "action": "",
                 "evidence": ""},
            ],
        }
    ],
    "functions": [
        {"name": "压力函数", "subject": "投资者",
         "formula": "风险承受能力 ÷ 产品或服务风险等级",
         "inputs": ["风险承受能力", "产品或服务风险等级"], "output_type": "RANK",
         "evidence": "压力函数 = 风险承受能力 ÷ 产品或服务风险等级"},
    ],
    "temporal": [
        {"subject": "投资者适当性管理义务", "kind": "VALIDITY",
         "value": "自2017年7月1日起实施", "evidence": "自2017年7月1日起实施"},
        {"subject": "风险承受能力评估", "kind": "FREQUENCY",
         "value": "每年一次", "evidence": "应当每年对投资者风险承受能力进行一次评估"},
    ],
    "actions": [
        {"name": "书面风险警示", "actor": "证券经营机构", "level": "WARN",
         "target": "投资者", "side_effect": "", "trigger": "风险承受能力等级发生变化",
         "evidence": "应当书面风险警示并重新匹配产品或服务"},
    ],
    "constraints": [
        {"subject": "风险承受能力等级", "type": "ENUM",
         "description": "由低至高至少划分为五级", "evidence": "由低至高至少划分为五级"},
    ],
    "permissions": [
        {"actor": "普通投资者", "action": "申请转化成为专业投资者", "effect": "PERMIT",
         "scope": "符合相关规定", "evidence": "普通投资者可以申请转化成为专业投资者"},
    ],
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/predict-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class PredictExtractPathTest(unittest.TestCase):
    def test_six_sets_map_to_objects(self):
        """六件套映射：extractor、独立枚举透传、ID 内容寻址、stats 计数。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(res.stats["mode"], "llm")
        self.assertEqual(res.stats["states"], 1)
        self.assertEqual(res.stats["functions"], 1)
        self.assertEqual(res.stats["temporal"], 2)
        self.assertEqual(res.stats["actions"], 1)
        self.assertEqual(res.stats["constraints"], 1)
        self.assertEqual(res.stats["permissions"], 1)

        # 状态机
        sm = res.states[0]
        self.assertEqual(sm.object, "投资者")
        self.assertEqual(sm.name, "风险承受能力等级状态机")
        self.assertEqual(sm.extractor, "llm.sm.v1")
        self.assertTrue(sm.sm_id.startswith("sm_"))
        self.assertEqual(len(sm.states), 3)
        self.assertEqual(len(sm.transitions), 2, "越界迁移应被丢弃")
        self.assertTrue(sm.states[0].initial)
        self.assertFalse(sm.states[1].initial)
        self.assertTrue(sm.states[0].state_id.startswith("st_"))
        self.assertTrue(sm.transitions[0].transition_id.startswith("tr_"))
        self.assertEqual(sm.transitions[0].from_index, 0)
        self.assertEqual(sm.transitions[0].to_index, 1)
        self.assertEqual(sm.transitions[0].event, "评估结果变化")
        self.assertEqual(sm.transitions[0].condition, "得分提高")
        self.assertEqual(sm.transitions[0].action, "调整等级")

        # 函数指标
        fn = res.functions[0]
        self.assertEqual(fn.extractor, "llm.fn.v1")
        self.assertEqual(fn.output_type, FunctionOutputType.RANK)
        self.assertEqual(fn.inputs, ["风险承受能力", "产品或服务风险等级"])
        self.assertTrue(fn.function_id.startswith("fn_"))

        # 时态
        tm = res.temporal
        self.assertEqual(tm[0].kind, TemporalKind.VALIDITY)
        self.assertEqual(tm[1].kind, TemporalKind.FREQUENCY)
        self.assertTrue(tm[0].temporal_id.startswith("tp_"))

        # 动作处置
        ac = res.actions[0]
        self.assertEqual(ac.level, ActionLevel.WARN)
        self.assertEqual(ac.actor, "证券经营机构")
        self.assertEqual(ac.trigger, "风险承受能力等级发生变化")
        self.assertTrue(ac.action_id.startswith("ac_"))

        # 约束
        cn = res.constraints[0]
        self.assertEqual(cn.type, ConstraintType.ENUM)
        self.assertTrue(cn.constraint_id.startswith("cn_"))

        # 授权
        pm = res.permissions[0]
        self.assertEqual(pm.effect, PermissionEffect.PERMIT)
        self.assertTrue(pm.permission_id.startswith("pm_"))

    def test_invalid_enums_fall_back(self):
        """六件套枚举非法 → 各自兜底（不自创类型）。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["functions"][0]["output_type"] = "NOT_A_TYPE"
        extract["temporal"][0]["kind"] = "NOT_A_KIND"
        extract["actions"][0]["level"] = "NOT_A_LEVEL"
        extract["constraints"][0]["type"] = "NOT_A_TYPE"
        extract["permissions"][0]["effect"] = "NOT_AN_EFFECT"
        extract["relations"][0]["prop_kind"] = "NOT_A_KIND"
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(res.functions[0].output_type, FunctionOutputType.OTHER)
        self.assertEqual(res.temporal[0].kind, TemporalKind.VALIDITY)
        self.assertEqual(res.actions[0].level, ActionLevel.OBSERVE)
        self.assertEqual(res.constraints[0].type, ConstraintType.CONSISTENCY)
        self.assertEqual(res.permissions[0].effect, PermissionEffect.DENY)
        # 非法 prop_kind → 空串 = 普通事实（FACTUAL 语义）
        self.assertEqual(res.relations[0].prop_kind, "")

    def test_prop_kind_strength_passthrough(self):
        """relations 传导增强透传：prop_kind/strength 到 Relation 与 Triplet。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        rel = [r for r in res.relations if r.predicate == "销售"][0]
        self.assertEqual(rel.prop_kind, "TRIGGER")
        self.assertEqual(rel.strength, "HIGH")
        trip = [t for t in res.triplets if t.predicate == "销售"][0]
        self.assertEqual(trip.prop_kind, "TRIGGER")
        self.assertEqual(trip.strength, "HIGH")

    def test_ids_content_addressed_and_deduped(self):
        """六件套 ID 内容寻址 + 同对象去重（幂等）。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res1 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        res2 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual({s.sm_id for s in res1.states}, {s.sm_id for s in res2.states})
        self.assertEqual({f.function_id for f in res1.functions},
                         {f.function_id for f in res2.functions})
        # 同对象重复出现 → 合并为一条
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["functions"].append(json.loads(json.dumps(extract["functions"][0])))
        stub2 = _make_stub(_SAMPLE, extract)
        res3 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub2))[0]
        self.assertEqual(len(res3.functions), 1, "重复函数应按 function_id 合并")


class PredictBuildKGTest(unittest.TestCase):
    def test_six_sets_not_in_graph_but_counted(self):
        """六件套不入实体-边图；build_kg 在 metadata 登记各自 count。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ex = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        graph = BuildKGStep().transform(ex, RunContext(llm=stub))

        # 图谱里没有状态机/函数名（独立产出，不是实体间事实）
        for key in ("状态机", "压力函数", "书面风险警示"):
            self.assertNotIn(key, graph.stats.by_predicate, f"{key} 不应进入实体-边图谱")
        # metadata 登记六件套 count + 关系传导增强透传到边属性
        self.assertEqual(graph.metadata.get("state_count"), 1)
        self.assertEqual(graph.metadata.get("function_count"), 1)
        self.assertEqual(graph.metadata.get("temporal_count"), 2)
        self.assertEqual(graph.metadata.get("action_count"), 1)
        self.assertEqual(graph.metadata.get("constraint_count"), 1)
        self.assertEqual(graph.metadata.get("permission_count"), 1)
        # prop_kind 已透传到边 metadata（edges 值为版本列表）
        edge_metas = [e.metadata for versions in graph.edges.values()
                      for e in versions
                      if isinstance(e.metadata, dict) and e.metadata.get("prop_kind")]
        self.assertTrue(any(m.get("prop_kind") == "TRIGGER" for m in edge_metas),
                        "传导增强应透传到边属性")


class PredictFullPipelineTest(unittest.TestCase):
    def test_full_pipeline_with_six_sets(self):
        """全链路：六件套作为独立产出保留，不破坏原有链路。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = make_context([_SAMPLE])
        pipeline = build_pipeline([_SAMPLE], llm=stub)
        state, results = pipeline.run(ctx=ctx)
        self.assertEqual(len(results), 3)  # extract-only：ingest → extract → build_kg
        self.assertEqual(state.extractions[0].stats["states"], 1)
        self.assertEqual(state.extractions[0].stats["permissions"], 1)
        self.assertEqual(len(state.extractions[0].states), 1)
        self.assertEqual(state.graph.metadata.get("state_count"), 1)
        self.assertEqual(state.graph.metadata.get("permission_count"), 1)


class PredictValidateTest(unittest.TestCase):
    def test_validate_extract_with_six_sets(self):
        """validate extract：含六件套的 04-extraction.json 通过校验。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_predict_validate"
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

    def test_render_viewer_six_tabs(self):
        """render_viewer 对含六件套的 04-extraction.json 渲染 6 个新 Tab。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_predict_render"
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
        for tab in ("状态机", "指标函数", "时态", "动作处置", "约束", "授权"):
            self.assertIn(tab, html, f"HTML 应含 {tab} Tab")
        self.assertIn("风险承受能力等级状态机", html, "HTML 应含状态机名")
        self.assertIn("压力函数", html, "HTML 应含函数指标名")
        self.assertIn("普通投资者", html, "HTML 应含授权主体")
        jp.unlink(missing_ok=True)
        hp.unlink(missing_ok=True)
        out_dir.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
