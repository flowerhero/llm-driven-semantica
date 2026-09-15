"""LLM / 大模型驱动路径测试（契约即规则版）。

证明「LLM 全权提议 → Python 薄壳映射 + 惰性锚点」这条链路正确、幂等：

- Extract 走 LLM：LLM 只交 entities[{surface,canonical,type}] +
  relations[{subject,predicate,object,object_type,evidence}]，Python 薄壳
  映射出 mentions/relations/triplets，ID 全部由 sha256 内容寻址补算（幂等）。
- object_literal 由 object_type 派生：DATE/MONEY 宾语 → literal=true，
  且宾语类型透传（不降为 OTHER）。
- Extract 无可用 LLM 时：零规则架构无确定性兜底 → 空结果 + Degradation
  （不静默、不造假）。
- Parse 走 LLM：结构化文本须与原文逐字等长（保真校验），否则 StepError。
- 宿主 agent 充当 LLM（方式 3）：``HostAgentLLMProvider`` 未注入时不可用，
  注入契约后驱动薄壳；生产默认即 ``HostAgentLLMProvider``，无需任何凭证。
- 全链路注入 ``StubLLMProvider``：8 步跑通，图谱带 ORG/PERSON/LOC 类型，
  两次构建实体 ID 集合一致（幂等）。

只依赖标准库 ``unittest``，用 ``StubLLMProvider``（测试桩）替代真实模型。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smini import build_pipeline, make_context  # noqa: E402
from smini.llm import (  # noqa: E402
    EXTRACT_SCHEMA,
    PARSE_SCHEMA,
    HostAgentLLMProvider,
    StubLLMProvider,
    default_llm,
)
from smini.protocols import RunContext  # noqa: E402
from smini.steps.extract import ExtractStep  # noqa: E402
from smini.steps.ingest import resolve_source  # noqa: E402
from smini.steps.parse import ParseStep  # noqa: E402
from smini.types import (  # noqa: E402
    DegradationKind,
    DependencyMissing,
    DocumentFormat,
    EntityType,
    NormalizedDocument,
    SourceRef,
    SourceType,
    StepError,
)

_SAMPLE = (
    "特斯拉公司成立于2003年，总部位于美国加利福尼亚州。马斯克是特斯拉公司的CEO。"
    "该公司2023年营收约967亿美元。OpenAI 是一家人工智能公司，总部位于美国旧金山。"
    "山姆·奥特曼是 OpenAI 的 CEO。"
)

# LLM「按契约交卷」的抽取结果（契约即规则）：
# 无偏移、无 ID、无 object_literal —— 由薄壳 + 惰性锚点补算
_LLM_EXTRACT = {
    "entities": [
        {"surface": "特斯拉公司", "type": "ORGANIZATION", "canonical": "特斯拉公司"},
        {"surface": "2003年", "type": "DATE", "canonical": "2003年"},
        {"surface": "美国加利福尼亚州", "type": "LOCATION", "canonical": "美国加利福尼亚州"},
        {"surface": "马斯克", "type": "PERSON", "canonical": "马斯克"},
        {"surface": "2023年", "type": "DATE", "canonical": "2023年"},
        {"surface": "967亿美元", "type": "MONEY", "canonical": "967亿美元"},
        {"surface": "OpenAI", "type": "ORGANIZATION", "canonical": "OpenAI"},
        {"surface": "旧金山", "type": "LOCATION", "canonical": "旧金山"},
        {"surface": "山姆·奥特曼", "type": "PERSON", "canonical": "山姆·奥特曼"},
    ],
    "relations": [
        {"subject": "特斯拉公司", "predicate": "成立于", "object": "2003年",
         "object_type": "DATE", "evidence": "特斯拉公司成立于2003年"},
        {"subject": "特斯拉公司", "predicate": "总部位于", "object": "美国加利福尼亚州",
         "object_type": "LOCATION", "evidence": "总部位于美国加利福尼亚州"},
        {"subject": "马斯克", "predicate": "CEO", "object": "特斯拉公司",
         "object_type": "ORGANIZATION", "evidence": "马斯克是特斯拉公司的CEO"},
        {"subject": "特斯拉公司", "predicate": "营收", "object": "967亿美元",
         "object_type": "MONEY", "evidence": "2023年营收约967亿美元"},
        {"subject": "OpenAI", "predicate": "总部位于", "object": "旧金山",
         "object_type": "LOCATION", "evidence": "总部位于美国旧金山"},
        {"subject": "山姆·奥特曼", "predicate": "CEO", "object": "OpenAI",
         "object_type": "ORGANIZATION", "evidence": "山姆·奥特曼是 OpenAI 的 CEO"},
    ],
}


def _make_stub(sample_text: str, extract: dict, *, fail: bool = False) -> StubLLMProvider:
    """返回一个按 schema 分流的测试桩。

    - 解析 schema → 返回与原文等长的结构化文本（保真）
    - 抽取 schema → 返回预置抽取结果；``fail=True`` 时抛 DependencyMissing
    """
    def respond(prompt: str, schema):
        if schema is PARSE_SCHEMA:
            return {"text": sample_text, "blocks": [{"kind": "paragraph", "text": sample_text}]}
        if fail:
            raise DependencyMissing("stub: 模拟 LLM 不可用")
        return dict(extract)

    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class LLMExtractPathTest(unittest.TestCase):
    def test_llm_path_typed_and_idempotent(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = RunContext(llm=stub)
        res = ExtractStep().transform([_nd(_SAMPLE)], ctx)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0].stats["mode"], "llm", "应走 LLM 抽取路径")

        # 实体类型透传：不应被降为 OTHER（含字面量类型 DATE/MONEY）
        types = {m.entity_type for m in res[0].mentions}
        for want in (EntityType.ORGANIZATION, EntityType.PERSON,
                     EntityType.LOCATION, EntityType.DATE, EntityType.MONEY):
            self.assertIn(want, types, f"LLM 图谱缺少实体类型 {want}")

        # 关系边的主语/宾语类型透传（核心修复点）
        typed = [
            (t.subject, t.subject_type.value, t.object, t.object_type.value)
            for t in res[0].triplets
            if not t.object_literal
        ]
        self.assertTrue(typed, "应产出类型化关系边")
        for subj, st, obj, ot in typed:
            self.assertNotEqual(st, "OTHER", f"边 {subj}→{obj} 主语类型被降为 OTHER")
            self.assertNotEqual(ot, "OTHER", f"边 {subj}→{obj} 宾语类型被降为 OTHER")

        # 字面量宾语：object_type 是数值类 → literal=true 且类型不丢
        lits = [(t.object, t.object_type.value, t.object_literal)
                for t in res[0].triplets if t.object_literal]
        self.assertTrue(lits, "应产出字面量边（成立于→2003年 / 营收→967亿美元）")
        for obj, ot, lit in lits:
            self.assertTrue(lit, f"字面量边 {obj} 应 literal=true")
            self.assertIn(ot, ("DATE", "MONEY", "PERCENT", "QUANTITY"),
                          f"字面量边 {obj} 类型应为数值类，实为 {ot}")

        # 幂等：两次构建的 ID 集合一致（Python 算 sha256，与模型无关）
        mids1 = {m.mention_id for m in res[0].mentions}
        tids1 = {t.triplet_id for t in res[0].triplets}
        res2 = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))
        mids2 = {m.mention_id for m in res2[0].mentions}
        tids2 = {t.triplet_id for t in res2[0].triplets}
        self.assertEqual(mids1, mids2, "mention_id 应内容寻址一致")
        self.assertEqual(tids1, tids2, "triplet_id 应内容寻址一致")

    def test_llm_unavailable_returns_empty_and_degrades(self):
        # 零规则架构：LLM 不可用 → 空结果 + Degradation，无确定性兜底
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT, fail=True)
        ctx = RunContext(llm=stub)
        res = ExtractStep().transform([_nd(_SAMPLE)], ctx)
        self.assertEqual(res[0].stats["mode"], "empty", "LLM 失败应为空结果")
        self.assertEqual(res[0].mentions, [], "空结果不应有实体")
        self.assertTrue(res[0].degradations, "应登记降级而非静默")
        deg = res[0].degradations[0]
        self.assertEqual(deg.component, "extract.llm")
        self.assertEqual(deg.kind, DegradationKind.UNAVAILABLE)

    def test_no_llm_returns_empty_with_no_credential(self):
        # 未注入 LLM 同样降级为空（no_credential）
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=None))
        self.assertEqual(res[0].stats["mode"], "empty")
        self.assertEqual(res[0].degradations[0].kind, DegradationKind.NO_CREDENTIAL)


class LLMParsePathTest(unittest.TestCase):
    def test_llm_parse_fidelity_guard_raises(self):
        # LLM 返回远短于原文的「结构化文本」→ 保真校验须拒绝
        stub = StubLLMProvider({"text": "截断的", "blocks": []})
        ctx = RunContext(llm=stub)
        rd = resolve_source(_SAMPLE)
        with self.assertRaises(StepError):
            ParseStep()._parse_with_llm(rd.content.decode("utf-8"), rd, DocumentFormat.TEXT, ctx)

    def test_llm_parse_runs_with_full_fidelity(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        ctx = RunContext(llm=stub)
        rd = resolve_source(_SAMPLE)
        pd = ParseStep()._parse_with_llm(rd.content.decode("utf-8"), rd, DocumentFormat.TEXT, ctx)
        self.assertEqual(pd.parser, "llm.v1", "应标记为 LLM 解析")
        self.assertEqual(pd.text, _SAMPLE, "解析文本须与原文逐字一致")
        self.assertTrue(pd.blocks, "应产出版式区块")


class HostAgentLLMProviderTest(unittest.TestCase):
    """宿主 agent（豆包）充当 LLM：inject 契约后可用，未注入则降级为空。"""

    def test_uninjected_is_unavailable(self):
        llm = HostAgentLLMProvider()
        self.assertFalse(llm.is_available(), "宿主未交卷时 is_available 应为 False")

    def test_injected_returns_contract(self):
        llm = HostAgentLLMProvider()
        llm.inject(dict(_LLM_EXTRACT))
        self.assertTrue(llm.is_available(), "宿主交卷后应可用")
        got = llm.extract("prompt", EXTRACT_SCHEMA)
        self.assertIn("entities", got)
        self.assertIn("relations", got)
        self.assertEqual(got["entities"][0]["surface"], "特斯拉公司")

    def test_injected_drives_extract_shell(self):
        # 宿主充当 LLM 的完整链路：inject → ExtractStep 薄壳
        llm = HostAgentLLMProvider(contract=dict(_LLM_EXTRACT))
        ctx = RunContext(llm=llm)
        res = ExtractStep().transform([_nd(_SAMPLE)], ctx)
        self.assertEqual(res[0].stats["mode"], "llm")
        self.assertGreaterEqual(res[0].stats["mentions"], 5)

    def test_default_llm_is_host_agent(self):
        # 生产默认实现即宿主：无需任何外部凭证，天然可用（空契约）
        self.assertIs(default_llm, HostAgentLLMProvider)


class LLMFullPipelineTest(unittest.TestCase):
    def _run(self, stub, query="特斯拉公司"):
        ctx = make_context([_SAMPLE], query)
        pipeline = build_pipeline([_SAMPLE], llm=stub, query=query)
        return pipeline.run(ctx=ctx)

    def test_full_pipeline_with_llm_builds_typed_graph(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        state, results = self._run(stub)
        self.assertEqual(len(results), 8)
        kinds = set(state.graph.stats.by_type)
        for want in ("ORGANIZATION", "PERSON", "LOCATION", "DATE", "MONEY"):
            self.assertIn(want, kinds, f"LLM 图谱缺少实体类型 {want}")

    def test_full_pipeline_with_llm_idempotent(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        _, r1 = self._run(stub)
        _, r2 = self._run(stub)
        self.assertEqual(
            set(r1[4].output.entities), set(r2[4].output.entities),
            "注入 LLM 后两次构建实体 ID 仍应一致（幂等）",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
