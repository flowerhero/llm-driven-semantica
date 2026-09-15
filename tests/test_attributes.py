"""属性抽取（v2 · DatatypeProperty）测试。

契约即规则架构下，宿主交卷三件套：entities + relations + attributes。
本测试证明属性通道正确、兜底、可消费：

- attributes → 属性三元组（extractor="llm.attr.v1"、object_literal=True、
  value_type 独立枚举透传、object_type=None）。
- value_type 非法 → OTHER 兜底（不自创类型）。
- attribute.entity 匹配不到 entities[] → 合成 mention（llm.attr.synth.v1），
  属性不丢（允许跨实体属性，归属判断交给 LLM，冗余由 QA 兜底）。
- build_kg 属性双落位：字面量边为权威事实 + Entity.properties 聚合视图。
- 全链路跑通、幂等、validate 通过。
"""

from __future__ import annotations

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
    AttributeValueType,
    EntityType,
    NormalizedDocument,
    SourceRef,
    SourceType,
)

_SAMPLE = (
    "特斯拉公司成立于2003年，总部位于美国加利福尼亚州，拥有12万名员工，已上市。"
    "公司注册资本为100亿美元，风险等级为高。马斯克是特斯拉公司的CEO。"
)

# 宿主交卷：entities + relations + attributes 三件套
_LLM_EXTRACT = {
    "entities": [
        {"surface": "特斯拉公司", "type": "ORGANIZATION", "canonical": "特斯拉公司"},
        {"surface": "2003年", "type": "DATE", "canonical": "2003年"},
        {"surface": "马斯克", "type": "PERSON", "canonical": "马斯克"},
    ],
    "relations": [
        {"subject": "特斯拉公司", "predicate": "成立于", "object": "2003年",
         "object_type": "DATE", "evidence": "特斯拉公司成立于2003年"},
        {"subject": "马斯克", "predicate": "CEO", "object": "特斯拉公司",
         "object_type": "ORGANIZATION", "evidence": "马斯克是特斯拉公司的CEO"},
    ],
    "attributes": [
        {"entity": "特斯拉公司", "name": "员工数", "value": "12万名员工",
         "value_type": "QUANTITY", "evidence": "拥有12万名员工"},
        {"entity": "特斯拉公司", "name": "上市", "value": "已上市",
         "value_type": "BOOLEAN", "evidence": "已上市"},
        {"entity": "特斯拉公司", "name": "注册资本", "value": "100亿美元",
         "value_type": "MONEY", "evidence": "公司注册资本为100亿美元"},
        {"entity": "特斯拉公司", "name": "风险等级", "value": "高",
         "value_type": "ENUM", "evidence": "风险等级为高"},
    ],
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/attr-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class AttributeExtractPathTest(unittest.TestCase):
    def test_attributes_map_to_literal_triplets(self):
        """属性 → 属性三元组：extractor、object_literal、value_type 透传。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        self.assertEqual(res[0].stats["mode"], "llm")
        self.assertEqual(res[0].stats["attributes"], 4, "应统计 4 条属性")

        attr_trips = [t for t in res[0].triplets if t.value_type is not None]
        self.assertEqual(len(attr_trips), 4, "属性三元组应全部映射")

        # 属性三元组：extractor 标记 + object_literal + value_type + object_type=None
        by_name = {t.predicate: t for t in attr_trips}
        emp = by_name["员工数"]
        self.assertEqual(emp.extractor, "llm.attr.v1")
        self.assertTrue(emp.object_literal, "属性宾语是字面量，object_literal 应为 True")
        self.assertIsNone(emp.object_type, "属性宾语不是实体，object_type 应为 None")
        self.assertEqual(emp.value_type, AttributeValueType.QUANTITY)
        self.assertEqual(emp.subject, "特斯拉公司")
        self.assertEqual(emp.object, "12万名员工")

        # value_type 独立枚举透传（不被降为实体枚举）
        self.assertEqual(by_name["上市"].value_type, AttributeValueType.BOOLEAN)
        self.assertEqual(by_name["注册资本"].value_type, AttributeValueType.MONEY)
        self.assertEqual(by_name["风险等级"].value_type, AttributeValueType.ENUM)

        # 属性三元组也带 evidence 溯源（relation 侧）
        rel_evidence = {r.predicate: r.evidence_text for r in res[0].relations
                        if r.extractor == "llm.attr.v1"}
        self.assertEqual(rel_evidence["注册资本"], "公司注册资本为100亿美元")

    def test_value_type_invalid_falls_back_to_other(self):
        """value_type 非法 → OTHER 兜底，不自创类型。"""
        extract = dict(_LLM_EXTRACT)
        attrs = [dict(a) for a in extract["attributes"]]
        attrs.append({"entity": "特斯拉公司", "name": "奇怪类型", "value": "x",
                      "value_type": "NOT_A_TYPE", "evidence": "x"})
        extract["attributes"] = attrs
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        trips = [t for t in res[0].triplets if t.value_type is not None]
        weird = [t for t in trips if t.predicate == "奇怪类型"]
        self.assertEqual(len(weird), 1)
        self.assertEqual(weird[0].value_type, AttributeValueType.OTHER, "非法类型应兜底 OTHER")

    def test_attribute_entity_mismatch_synthesizes_mention(self):
        """attribute.entity 匹配不到 entities[] → 合成 mention，属性不丢。"""
        extract = dict(_LLM_EXTRACT)
        attrs = [dict(a) for a in extract["attributes"]]
        attrs.append({"entity": "加州工厂", "name": "产能", "value": "年产50万辆",
                      "value_type": "QUANTITY", "evidence": "加州工厂年产50万辆"})
        extract["attributes"] = attrs
        stub = _make_stub(_SAMPLE, extract)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        trips = [t for t in res[0].triplets if t.value_type is not None]
        factory = [t for t in trips if t.predicate == "产能"]
        self.assertEqual(len(factory), 1, "跨实体属性不应因归属失配而丢")
        self.assertEqual(factory[0].subject, "加州工厂")
        self.assertEqual(factory[0].subject_type, EntityType.OTHER, "失配实体兜底 OTHER")
        self.assertEqual(factory[0].value_type, AttributeValueType.QUANTITY)

    def test_attribute_triplet_id_is_content_addressed(self):
        """属性三元组 ID 内容寻址，两次构建一致（幂等）。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ids1 = {t.triplet_id for t in ExtractStep()
                .transform([_nd(_SAMPLE)], RunContext(llm=stub))[0].triplets}
        ids2 = {t.triplet_id for t in ExtractStep()
                .transform([_nd(_SAMPLE)], RunContext(llm=stub))[0].triplets}
        self.assertEqual(ids1, ids2, "属性三元组 ID 应内容寻址幂等")


class AttributeBuildKGTest(unittest.TestCase):
    def test_properties_dual_placement(self):
        """build_kg 双落位：字面量边为权威 + Entity.properties 聚合视图。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ex = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        graph = BuildKGStep().transform(ex, RunContext(llm=stub))

        # 特斯拉公司实体存在
        tesla = next(e for e in graph.entities.values()
                     if e.canonical_name == "特斯拉公司")
        # 聚合视图：属性已落 Entity.properties
        self.assertEqual(tesla.properties["员工数"], "12万名员工")
        self.assertEqual(tesla.properties["注册资本"], "100亿美元")
        self.assertEqual(tesla.properties["风险等级"], "高")
        # 字面量边（权威事实）存在：subject=特斯拉、predicate=属性名、object_literal=值
        attr_edges = [e for versions in graph.edges.values() for e in versions
                      if e.object_literal is not None and e.subject_id == tesla.entity_id]
        self.assertGreaterEqual(len(attr_edges), 4, "属性应产出字面量边")
        lit_vals = {e.object_literal for e in attr_edges}
        for want in ("12万名员工", "100亿美元", "高"):
            self.assertIn(want, lit_vals, f"字面量边应含属性值 {want}")


class AttributeFullPipelineTest(unittest.TestCase):
    def test_full_pipeline_with_attributes(self):
        """全链路：属性进入图谱并统计。"""
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = make_context([_SAMPLE], query="特斯拉公司")
        pipeline = build_pipeline([_SAMPLE], llm=stub, query="特斯拉公司")
        state, results = pipeline.run(ctx=ctx)
        self.assertEqual(len(results), 8)
        # 属性作为字面量边进入图谱
        tesla = next(e for e in state.graph.entities.values()
                     if e.canonical_name == "特斯拉公司")
        self.assertEqual(tesla.properties["注册资本"], "100亿美元")
        self.assertIn("注册资本", state.graph.stats.by_predicate,
                      "属性名应进入谓词统计")


if __name__ == "__main__":
    unittest.main(verbosity=2)
