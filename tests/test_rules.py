"""业务规则抽取（v3 · Rule-by-Contract）测试。

契约即规则架构下，宿主交卷四件套：entities + relations + attributes + rules。
本测试证明规则通道正确、兜底、可消费：

- rules → Rule（extractor="llm.rule.v1"、modality 独立 5 类枚举透传、
  rule_id 由 (subject, condition, action, modality) 内容寻址）。
- modality 非法 → OTHER 兜底（不自创类型）。
- action 缺失 → 跳过（契约要求 action 必有）。
- 规则不入实体-边图：build_kg 不产生规则边，仅在 metadata 登记 rule_count。
- 规则与关系/属性分界：含道义动词的句子进 rules，纯事实陈述保持
  relations/attributes（四件套并行不互斥）。
- 全链路跑通、幂等、validate 通过。
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
    SourceRef,
    SourceType,
)

_SAMPLE = (
    "证券经营机构向投资者销售金融产品时，应当履行适当性义务。"
    "不得向风险承受能力等级不匹配的投资者销售产品。"
    "普通投资者申请转化成为专业投资者的，可以按照相关规定申请转化。"
    "产品或服务风险等级由低至高至少划分为五级。"
    "该指引由中国证券业协会负责解释。"
)

# 宿主交卷：四件套
_LLM_EXTRACT = {
    "entities": [
        {"surface": "证券经营机构", "type": "ORGANIZATION", "canonical": "证券经营机构"},
        {"surface": "投资者", "type": "CONCEPT", "canonical": "投资者"},
        {"surface": "普通投资者", "type": "CONCEPT", "canonical": "普通投资者"},
        {"surface": "专业投资者", "type": "CONCEPT", "canonical": "专业投资者"},
        {"surface": "中国证券业协会", "type": "ORGANIZATION", "canonical": "中国证券业协会"},
    ],
    "relations": [
        {"subject": "中国证券业协会", "predicate": "解释", "object": "该指引",
         "object_type": "CONCEPT", "evidence": "该指引由中国证券业协会负责解释"},
    ],
    "attributes": [
        {"entity": "普通投资者", "name": "风险承受能力等级划分", "value": "五级",
         "value_type": "ENUM", "evidence": "产品或服务风险等级由低至高至少划分为五级"},
    ],
    "rules": [
        {"subject": "证券经营机构", "condition": "向投资者销售金融产品",
         "action": "应当履行适当性义务", "modality": "OBLIGATION",
         "evidence": "证券经营机构向投资者销售金融产品时，应当履行适当性义务"},
        {"subject": "证券经营机构", "condition": "",
         "action": "不得向风险承受能力等级不匹配的投资者销售产品",
         "modality": "PROHIBITION",
         "evidence": "不得向风险承受能力等级不匹配的投资者销售产品"},
        {"subject": "普通投资者", "condition": "申请转化成为专业投资者",
         "action": "可以按照相关规定申请转化", "modality": "PERMISSION",
         "evidence": "普通投资者申请转化成为专业投资者的"},
        {"subject": "", "condition": "产品风险等级由低至高",
         "action": "至少划分为五级", "modality": "CONDITIONAL",
         "evidence": "产品或服务风险等级由低至高至少划分为五级"},
    ],
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/rule-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class RuleExtractPathTest(unittest.TestCase):
    def test_rules_map_to_rule_objects(self):
        """rules → Rule：extractor、modality 透传、rule_id 内容寻址。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        self.assertEqual(res[0].stats["mode"], "llm")
        self.assertEqual(res[0].stats["rules"], 4, "应统计 4 条规则")

        rules = res[0].rules
        self.assertEqual(len(rules), 4)
        by_mod = {r.modality: r for r in rules}
        self.assertEqual(by_mod[RuleModality.OBLIGATION].subject, "证券经营机构")
        self.assertEqual(by_mod[RuleModality.OBLIGATION].condition, "向投资者销售金融产品")
        self.assertEqual(by_mod[RuleModality.OBLIGATION].action, "应当履行适当性义务")
        self.assertEqual(by_mod[RuleModality.OBLIGATION].extractor, "llm.rule.v1")
        self.assertEqual(by_mod[RuleModality.PROHIBITION].action,
                         "不得向风险承受能力等级不匹配的投资者销售产品")
        self.assertEqual(by_mod[RuleModality.PERMISSION].subject, "普通投资者")
        self.assertEqual(by_mod[RuleModality.CONDITIONAL].subject, "",
                         "纯派生规则 subject 可空")

        # 无模态词 IF-THEN 推导 → CONDITIONAL（区别于道义三模态）
        self.assertEqual(by_mod[RuleModality.CONDITIONAL].modality, RuleModality.CONDITIONAL)

        # 规则带 evidence 溯源
        self.assertIn("应当履行适当性义务", by_mod[RuleModality.OBLIGATION].evidence_text)

    def test_modality_invalid_falls_back_to_other(self):
        """modality 非法 → OTHER 兜底，不自创类型。"""
        extract = dict(_LLM_EXTRACT)
        rules = [dict(r) for r in extract["rules"]]
        rules.append({"subject": "证券经营机构", "condition": "", "action": "x",
                      "modality": "NOT_A_MODALITY", "evidence": "x"})
        extract["rules"] = rules
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        weird = [r for r in res[0].rules if r.action == "x"]
        self.assertEqual(len(weird), 1)
        self.assertEqual(weird[0].modality, RuleModality.OTHER, "非法模态应兜底 OTHER")

    def test_missing_action_skipped(self):
        """action 缺失 → 跳过（契约要求 action 必有）。"""
        extract = dict(_LLM_EXTRACT)
        rules = [dict(r) for r in extract["rules"]]
        rules.append({"subject": "x", "condition": "", "action": "  ",
                      "modality": "OBLIGATION", "evidence": "x"})
        extract["rules"] = rules
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        self.assertEqual(len(res[0].rules), 4, "action 为空的规则应被跳过")

    def test_rule_id_is_content_addressed_and_deduped(self):
        """rule_id 内容寻址 + 同规则去重（幂等）。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res1 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        res2 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        ids1 = {r.rule_id for r in res1.rules}
        ids2 = {r.rule_id for r in res2.rules}
        self.assertEqual(ids1, ids2, "rule_id 应内容寻址幂等")
        self.assertEqual(len(ids1), 4, "同 (subject,condition,action,modality) 应去重")

        # 同规则重复出现 → 合并为一条
        extract = dict(_LLM_EXTRACT)
        rules = [dict(r) for r in extract["rules"]]
        rules.append(dict(rules[0]))
        extract["rules"] = rules
        stub2 = _make_stub(_SAMPLE, extract)
        res3 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub2))[0]
        self.assertEqual(len(res3.rules), 4, "重复规则应按 rule_id 合并")

    def test_rules_are_separate_from_relations_attributes(self):
        """规则与关系/属性分界：规则独立产出，不影响关系/属性通道。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        # 纯事实陈述（中国证券业协会 解释 该指引）→ relations，不进 rules
        rel_rels = [r for r in res.relations if r.extractor == "llm.rel.v1"]
        self.assertEqual(len(rel_rels), 1)
        self.assertEqual(rel_rels[0].predicate, "解释")
        # 字面量事实 → attributes，不进 rules
        attr_trips = [t for t in res.triplets if t.value_type is not None]
        self.assertEqual(len(attr_trips), 1)
        self.assertEqual(attr_trips[0].predicate, "风险承受能力等级划分")
        # 规则单独统计
        self.assertEqual(res.stats["rules"], 4)


class RuleBuildKGTest(unittest.TestCase):
    def test_rules_not_in_graph_but_counted_in_metadata(self):
        """规则不入实体-边图；build_kg 仅在 metadata 登记 rule_count。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ex = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        graph = BuildKGStep().transform(ex, RunContext(llm=stub))

        # 图谱里没有规则谓词（应当/不得/可以 是规则模态，不进边；
        # 属性名「风险承受能力等级划分」作为字面量边进图是正常属性行为）
        rule_preds = {p for p in graph.stats.by_predicate
                      if "应当" in p or "不得" in p or "可以" in p}
        self.assertEqual(rule_preds, set(), "规则模态谓词不应进入实体-边图谱")
        # metadata 登记 rule_count
        self.assertEqual(graph.metadata.get("rule_count"), 4)


class RuleFullPipelineTest(unittest.TestCase):
    def test_full_pipeline_with_rules(self):
        """全链路：规则作为独立产出保留，不破坏原有链路。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = make_context([_SAMPLE], query="适当性义务")
        pipeline = build_pipeline([_SAMPLE], llm=stub, query="适当性义务")
        state, results = pipeline.run(ctx=ctx)
        self.assertEqual(len(results), 8)
        # 规则保留在 extraction 阶段
        self.assertEqual(state.extractions[0].stats["rules"], 4)
        self.assertEqual(len(state.extractions[0].rules), 4)
        # 图谱照常建出（规则不影响实体/边）
        self.assertGreaterEqual(len(state.graph.entities), 5)
        self.assertEqual(state.graph.metadata.get("rule_count"), 4)


class RuleValidateTest(unittest.TestCase):
    def test_validate_extract_with_rules(self):
        """validate extract：含 rules 的 04-extraction.json 通过校验。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        from smini.types import to_dict
        payload = json.dumps(to_dict(res), ensure_ascii=False, default=str)
        out_dir = Path(__file__).resolve().parent / "_tmp_rules_validate"
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
