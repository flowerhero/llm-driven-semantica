"""业务规则抽取（v3 · Rule-by-Contract）测试。

契约即规则架构下，宿主交卷十一件套：entities + relations + attributes + rules
+ processes + states + functions + temporal + actions + constraints + permissions。
本测试证明规则通道正确、兜底、可消费：

- rules → Rule（extractor="llm.ru.v1"、rule_id 内容寻址、modality 独立枚举、
  非法 → UNSPECIFIED 兜底）。
- rule_type 四分类透传（OBLIGATION/PROHIBITION/PERMISSION/RECOMMENDATION，
  非法 → GENERIC）；certainty/strength/priority 透传；reused_by 透传（规则复用）。
- 主体失配合成 mention（llm.ru.synth.v1），规则不丢（归属判断交给 LLM）。
- 规则不进实体-边图：build_kg 仅在 metadata 登记 rule_count。
- 全链路跑通、幂等、validate 通过、renderer 渲染规则 Tab。
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
    NormalizedDocument,
    RuleModality,
    RuleType,
    SourceRef,
    SourceType,
)

_SAMPLE = (
    "证券经营机构应当对投资者履行适当性管理义务，不得向风险承受能力等级不匹配的"
    "投资者销售产品。"
    "普通投资者可以申请转化成为专业投资者，专业投资者可以购买所有风险等级的产品或服务。"
    "建议证券公司定期开展投资者适当性自查。"
    "风险承受能力等级由低至高至少划分为五级。"
)

# 宿主交卷：十一件套
_LLM_EXTRACT = {
    "entities": [
        {"surface": "证券经营机构", "type": "ORGANIZATION", "canonical": "证券经营机构"},
        {"surface": "投资者", "type": "CONCEPT", "canonical": "投资者"},
        {"surface": "普通投资者", "type": "CONCEPT", "canonical": "普通投资者"},
        {"surface": "专业投资者", "type": "CONCEPT", "canonical": "专业投资者"},
        {"surface": "风险承受能力等级", "type": "FINANCIAL_INDICATOR", "canonical": "风险承受能力等级"},
    ],
    "relations": [],
    "attributes": [],
    "rules": [
        {"subject": "证券经营机构", "condition": "向投资者销售金融产品",
         "action": "应当履行适当性义务", "modality": "OBLIGATION",
         "rule_type": "OBLIGATION", "certainty": "HIGH", "strength": "STRONG",
         "priority": 1, "evidence": "应当对投资者履行适当性管理义务"},
        {"subject": "证券经营机构", "condition": "向风险承受能力等级不匹配的投资者销售产品",
         "action": "不得销售", "modality": "PROHIBITION",
         "rule_type": "PROHIBITION", "certainty": "HIGH", "strength": "STRONG",
         "priority": 2, "evidence": "不得向风险承受能力等级不匹配的投资者销售产品"},
        {"subject": "普通投资者", "condition": "符合相关规定",
         "action": "可以申请转化成为专业投资者", "modality": "PERMISSION",
         "rule_type": "PERMISSION", "certainty": "MEDIUM", "strength": "MEDIUM",
         "priority": 3, "evidence": "普通投资者可以申请转化成为专业投资者"},
        {"subject": "证券公司", "condition": "定期",
         "action": "开展投资者适当性自查", "modality": "RECOMMENDATION",
         "rule_type": "RECOMMENDATION", "certainty": "LOW", "strength": "WEAK",
         "priority": 4, "evidence": "建议证券公司定期开展投资者适当性自查"},
    ],
    "processes": [],
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
    src = SourceRef.of("memory://inline/rule-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class RuleExtractPathTest(unittest.TestCase):
    def test_rules_map_with_modality_and_type(self):
        """rules → Rule：extractor、rule_id、modality/rule_type/certainty 透传。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(res.stats["mode"], "llm")
        self.assertEqual(res.stats["rules"], 4)

        r0 = res.rules[0]
        self.assertTrue(r0.rule_id.startswith("ru_"))
        self.assertEqual(r0.extractor, "llm.ru.v1")
        self.assertEqual(r0.subject, "证券经营机构")
        self.assertEqual(r0.condition, "向投资者销售金融产品")
        self.assertEqual(r0.action, "应当履行适当性义务")
        self.assertEqual(r0.modality, RuleModality.OBLIGATION)
        self.assertEqual(r0.rule_type, RuleType.OBLIGATION)
        self.assertEqual(r0.certainty, "HIGH")
        self.assertEqual(r0.strength, "STRONG")
        self.assertEqual(r0.priority, 1)
        # 四种模态齐备
        modalities = {r.modality for r in res.rules}
        self.assertEqual(modalities, {RuleModality.OBLIGATION, RuleModality.PROHIBITION,
                                      RuleModality.PERMISSION, RuleModality.RECOMMENDATION})

    def test_invalid_modality_and_type_fall_back(self):
        """modality/rule_type 非法 → 各自兜底（不自创类型）。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["rules"].append({"subject": "证券经营机构", "condition": "x",
                                  "action": "y", "modality": "NOT_A_MODALITY",
                                  "rule_type": "NOT_A_TYPE", "certainty": "",
                                  "strength": "", "priority": 0, "evidence": ""})
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        bad = [r for r in res.rules if r.condition == "x"][0]
        self.assertEqual(bad.modality, RuleModality.UNSPECIFIED)
        self.assertEqual(bad.rule_type, RuleType.GENERIC)

    def test_subject_mismatch_synthesizes_mention(self):
        """规则主体匹配不到 entities[] → 合成 mention（llm.ru.synth.v1），规则不丢。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["rules"].append({"subject": "期货公司", "condition": "经营期货业务",
                                  "action": "应当取得期货业务许可", "modality": "OBLIGATION",
                                  "rule_type": "OBLIGATION", "certainty": "HIGH",
                                  "strength": "STRONG", "priority": 5,
                                  "evidence": "经营期货业务应当取得期货业务许可"})
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        new_rule = [r for r in res.rules if r.subject == "期货公司"][0]
        self.assertEqual(new_rule.extractor, "llm.ru.synth.v1",
                         "主体失配的规则应标记为合成")
        self.assertEqual(new_rule.subject_type, EntityType.OTHER,
                         "失配主体兜底 OTHER")

    def test_reused_by_passthrough(self):
        """reused_by 透传：规则被其他规则引用（v7 借鉴七模型）。"""
        extract = json.loads(json.dumps(_LLM_EXTRACT))
        extract["rules"].append({"subject": "证券经营机构", "condition": "自查发现问题",
                                  "action": "整改并报告", "modality": "OBLIGATION",
                                  "rule_type": "OBLIGATION", "certainty": "HIGH",
                                  "strength": "STRONG", "priority": 6,
                                  "reused_by": ["开展投资者适当性自查"],
                                  "evidence": "自查发现问题应当整改并报告"})
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        r = [r for r in res.rules if r.condition == "自查发现问题"][0]
        self.assertEqual(r.reused_by, ["开展投资者适当性自查"])

    def test_ids_idempotent(self):
        """rule_id 内容寻址幂等。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        r1 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        r2 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual({r.rule_id for r in r1.rules},
                         {r.rule_id for r in r2.rules})


class RuleBuildKGTest(unittest.TestCase):
    def test_rules_not_in_graph_but_counted(self):
        """规则不进实体-边图；build_kg 在 metadata 登记 rule_count。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ex = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        graph = BuildKGStep().transform(ex, RunContext(llm=stub))
        self.assertNotIn("应当履行适当性义务", graph.stats.by_predicate,
                         "规则动作不应进入实体-边图谱")
        self.assertEqual(graph.metadata.get("rule_count"), 4)


class RuleFullPipelineTest(unittest.TestCase):
    def test_full_pipeline_with_rules(self):
        """全链路：规则作为独立产出保留，不破坏原有链路。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = make_context([_SAMPLE])
        pipeline = build_pipeline([_SAMPLE], llm=stub)
        state, results = pipeline.run(ctx=ctx)
        self.assertEqual(len(results), 3)  # extract-only：ingest → extract → build_kg
        self.assertEqual(len(state.extractions[0].rules), 4)
        self.assertEqual(state.graph.metadata.get("rule_count"), 4)


class RuleValidateTest(unittest.TestCase):
    def test_validate_extract_with_rules(self):
        """validate extract：含规则的 04-extraction.json 通过校验。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_rule_validate"
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

    def test_render_viewer_rule_tab(self):
        """render_viewer 对含规则的 04-extraction.json 渲染规则 Tab。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_rule_render"
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
        self.assertIn("业务规则", html, "HTML 应含业务规则 Tab")
        self.assertIn("应当履行适当性义务", html, "HTML 应含规则动作")
        self.assertIn("不得向风险承受能力等级不匹配的投资者销售产品", html,
                      "HTML 应含禁止性规则")
        jp.unlink(missing_ok=True)
        hp.unlink(missing_ok=True)
        out_dir.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
