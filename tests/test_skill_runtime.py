"""tests/test_skill_runtime.py — Skill-First 架构下 Python 运行时契约（零规则版）。

验证「LLM 按契约交卷 → Python 薄壳 + 确定性锚点」这条接缝：

  * 薄壳（ExtractStep + StubLLM）产出 extraction.json，ID 由 sha256 补算，
    跨进程幂等；`ids extract` 对空 ID 产物补算后与薄壳产物逐字节一致
  * Schema 校验能拦住 LLM 的典型错误（漏设 object_literal / span 错位 / 枚举越界）
  * 确定性抽取兜底已删除：`fallback extract` 必须报错（零规则）
  * fallback parse / normalize 保留（格式解析，非抽取语义），降级必须出声
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from smini import runtime as R
from smini.llm import PARSE_SCHEMA, StubLLMProvider
from smini.ids import content_hash, doc_id, mention_id

SAMPLE = (
    "特斯拉公司成立于2003年，总部位于美国加利福尼亚州。马斯克是特斯拉公司的CEO。"
    "该公司2023年营收约967亿美元。OpenAI 是一家人工智能公司，总部位于美国旧金山。"
    "山姆·奥特曼是 OpenAI 的 CEO。"
)

# LLM 按契约交卷的抽取结果（无偏移/ID/literal）
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


def _run_dir() -> Path:
    return Path(tempfile.mkdtemp(prefix="smini-skill-"))


def _stub() -> StubLLMProvider:
    def respond(prompt: str, schema):
        if schema is PARSE_SCHEMA:
            return {"text": SAMPLE, "blocks": [{"kind": "paragraph", "text": SAMPLE}]}
        return dict(_LLM_EXTRACT)

    return StubLLMProvider(respond)


def _extract_via_shell(run: Path, source: str = SAMPLE) -> None:
    """薄壳（ExtractStep + StubLLM）产出 04-extraction.json（ID 已补算）。"""
    from smini.protocols import RunContext
    from smini.steps.extract import ExtractStep
    from smini.types import NormalizedDocument, SourceRef, SourceType, to_dict

    data = R.load_json(str(run / "03-normalized.json"))
    doc = data[0] if isinstance(data, list) else data
    nd = NormalizedDocument(
        doc_id=doc["doc_id"],
        source=SourceRef.of(doc["source"]["uri"], SourceType.TEXT),
        text=doc["text"],
    )
    res = ExtractStep().transform([nd], RunContext(llm=_stub()))[0]
    R.dump_json(str(run / "04-extraction.json"), to_dict(res))


def _pipeline(run: Path, source: str = SAMPLE) -> None:
    """跑零规则全链路，产出 01-05 号文件：
    ingest → fallback parse/normalize（格式解析）→ 薄壳 extract → ids → graph。
    """
    R.cmd_ingest([f"text://{source}"], str(run / "01-raw.json"))
    R.cmd_fallback("parse", str(run / "01-raw.json"), str(run / "02-parsed.json"))
    R.cmd_fallback("normalize", str(run / "02-parsed.json"), str(run / "03-normalized.json"))
    _extract_via_shell(run)
    R.cmd_ids("extract", str(run / "04-extraction.json"))
    R.cmd_graph_build(str(run / "04-extraction.json"), str(run / "05-graph.json"))


class TestIdsExtract(unittest.TestCase):
    """薄壳/LLM 提议 → Python 补算 ID。"""

    def setUp(self) -> None:
        self.run = _run_dir()
        _pipeline(self.run)

    def _as_llm_proposal(self) -> dict:
        """把薄壳产物改造成「LLM 提议态」：所有 ID 清空（模拟 Skill 直接产出）。"""
        data = R.load_json(str(self.run / "04-extraction.json"))
        for m in data.get("mentions", []):
            m["mention_id"] = ""
        for r in data.get("relations", []):
            r["relation_id"] = ""
        for t in data.get("triplets", []):
            t["triplet_id"] = ""
        for c in data.get("chunks", []):
            c["chunk_id"] = ""
        data.pop("degradations", None)
        data.pop("stats", None)
        return data

    def test_ids_extract_fills_all_empty_ids(self) -> None:
        prop = self._as_llm_proposal()
        p = self.run / "04-llm.json"
        R.dump_json(str(p), prop)
        self.assertEqual(R.cmd_ids("extract", str(p)), 0)

        got = R.load_json(str(p))
        ids = [m["mention_id"] for m in got.get("mentions", [])]
        self.assertTrue(ids, "mention_id 应被补算")
        self.assertTrue(all(i for i in ids), f"仍存在空 mention_id：{ids}")
        self.assertTrue(all(t["triplet_id"] for t in got.get("triplets", [])),
                        "triplet_id 应被补算")

    def test_ids_extract_is_deterministic(self) -> None:
        prop = self._as_llm_proposal()
        a, b = self.run / "a.json", self.run / "b.json"
        R.dump_json(str(a), json.loads(json.dumps(prop)))
        R.dump_json(str(b), json.loads(json.dumps(prop)))
        R.cmd_ids("extract", str(a))
        R.cmd_ids("extract", str(b))
        self.assertEqual(R.load_json(str(a)), R.load_json(str(b)),
                         "同一份 LLM 提议补算两次，结果必须一致")

    def test_ids_llm_proposal_matches_shell_ids(self) -> None:
        """★ 核心契约：LLM 提议 + ids 补算 ≡ 薄壳补算的 ID（同一定性锚点）。"""
        prop = self._as_llm_proposal()
        p = self.run / "04-llm.json"
        R.dump_json(str(p), prop)
        R.cmd_ids("extract", str(p))

        shell = R.load_json(str(self.run / "04-extraction.json"))
        llm = R.load_json(str(p))
        self.assertEqual(
            {m["mention_id"] for m in shell["mentions"]},
            {m["mention_id"] for m in llm["mentions"]},
            "ids 补算的 mention_id 必须与薄壳一致",
        )
        self.assertEqual(
            {t["triplet_id"] for t in shell["triplets"]},
            {t["triplet_id"] for t in llm["triplets"]},
            "ids 补算的 triplet_id 必须与薄壳一致",
        )

    def test_ids_handles_unlocated_span(self) -> None:
        """span 未定位（char_start=-1）的 mention，ID 退化为 canonical 稳定生成。"""
        prop = self._as_llm_proposal()
        # 把第一个 mention 改为未定位，模拟 LLM 给的 surface 在原文中找不到
        m0 = prop["mentions"][0]
        m0["char_start"], m0["char_end"] = -1, -1
        p = self.run / "nospan.json"
        R.dump_json(str(p), prop)
        self.assertEqual(R.cmd_ids("extract", str(p)), 0)
        got = R.load_json(str(p))["mentions"][0]["mention_id"]
        self.assertTrue(got.startswith("m_"), "未定位 mention 的 ID 应稳定生成")

    def test_llm_proposal_builds_same_graph_as_shell(self) -> None:
        """LLM 提议 + ids + build 产出的图，与薄壳 + build 一致（锚点收敛）。"""
        prop = self._as_llm_proposal()
        p = self.run / "04-llm.json"
        R.dump_json(str(p), prop)
        R.cmd_ids("extract", str(p))
        R.cmd_graph_build(str(p), str(self.run / "05-llm-graph.json"))

        shell = R.load_json(str(self.run / "05-graph.json"))
        llm = R.load_json(str(self.run / "05-llm-graph.json"))
        self.assertEqual(set(shell["entities"]), set(llm["entities"]),
                         "LLM 提议路径产出的 entity_id 集合必须与薄壳一致")
        self.assertEqual(set(shell["edges"]), set(llm["edges"]),
                         "LLM 提议路径产出的 edge_id 集合必须与薄壳一致")


class TestValidate(unittest.TestCase):
    """Schema + 领域校验要能拦住 LLM 的典型错误。"""

    def setUp(self) -> None:
        self.run = _run_dir()
        _pipeline(self.run)

    def test_valid_artifacts_pass(self) -> None:
        self.assertEqual(R.cmd_validate("raw", str(self.run / "01-raw.json")), 0)
        self.assertEqual(R.cmd_validate("parsed", str(self.run / "02-parsed.json"),
                                        str(self.run / "01-raw.json")), 0)
        self.assertEqual(R.cmd_validate("normalized", str(self.run / "03-normalized.json")), 0)
        self.assertEqual(R.cmd_validate("extraction", str(self.run / "04-extraction.json")), 0)
        self.assertEqual(R.cmd_validate("graph", str(self.run / "05-graph.json")), 0)

    def test_rejects_missing_object_literal(self) -> None:
        """DATE/MONEY 宾语未设 object_literal → 必须报错（消费层虽会修正，校验仍出声）。"""
        p = self.run / "bad.json"
        data = R.load_json(str(self.run / "04-extraction.json"))
        for t in data.get("triplets", []):
            if t.get("object_type") in ("DATE", "MONEY", "PERCENT", "QUANTITY"):
                t["object_literal"] = False
        R.dump_json(str(p), data)
        self.assertEqual(R.cmd_validate("extraction", str(p)), 1)

    def test_rejects_unknown_enum_value(self) -> None:
        p = self.run / "bad2.json"
        data = R.load_json(str(self.run / "04-extraction.json"))
        if data.get("mentions"):
            data["mentions"][0]["entity_type"] = "COMPANY"  # 非法类型（不在 16 类枚举内）
        R.dump_json(str(p), data)
        self.assertEqual(R.cmd_validate("extraction", str(p)), 1)

    def test_accepts_unlocated_span(self) -> None:
        """契约即规则：char_start=-1（未定位）应被 validate 放行。"""
        p = self.run / "ok-span.json"
        data = R.load_json(str(self.run / "04-extraction.json"))
        data["mentions"][0]["char_start"] = -1
        data["mentions"][0]["char_end"] = -1
        R.dump_json(str(p), data)
        self.assertEqual(R.cmd_validate("extraction", str(p)), 0)

    def test_rejects_misaligned_block_span(self) -> None:
        p = self.run / "bad3.json"
        data = R.load_json(str(self.run / "02-parsed.json"))
        if data.get("blocks"):
            data["blocks"][0]["char_start"] += 1  # 错位一个字符
        R.dump_json(str(p), data)
        self.assertEqual(R.cmd_validate("parsed", str(p)), 1)

    def test_alias_parse_maps_to_parsed(self) -> None:
        """Skill 里写 validate parse 也应工作（step 名别名）。"""
        self.assertEqual(R.cmd_validate("parse", str(self.run / "02-parsed.json")), 0)


class TestNormalizeApply(unittest.TestCase):
    """patch 提议的坐标漂移由 Python 算。"""

    def test_apply_patches_computes_new_coords(self) -> None:
        run = _run_dir()
        data = {
            "doc_id": "d_test",
            "source": {"source_id": "s", "uri": "memory://inline/x", "source_type": "text"},
            "text": "特斯拉  公司  成立于2003年",
            "blocks": [
                {"block_id": "", "kind": "paragraph", "text": "特斯拉  公司  成立于2003年",
                 "order": 0, "char_start": 0, "char_end": 17}
            ],
            # "特斯拉  公司  成立于2003年" 索引：0-2 特斯拉 / 3-4 双空格 /
            # 5-6 公司 / 7-8 双空格 / 9-16 成立于2003年（总长 17）
            "patches": [
                {"orig_start": 3, "orig_end": 5, "kind": "whitespace",
                 "original": "  ", "replacement": " "},
                {"orig_start": 7, "orig_end": 9, "kind": "whitespace",
                 "original": "  ", "replacement": " "},
            ],
            "mentions": [],
        }
        p = run / "03.json"
        R.dump_json(str(p), data)
        self.assertEqual(R.cmd_normalize_apply(str(p)), 0)

        got = R.load_json(str(p))
        self.assertEqual(got["text"], "特斯拉 公司 成立于2003年")

        # new_start == orig_start + 前面所有 patch 的 delta 之和
        ps = got["patches"]
        self.assertEqual(ps[0]["new_start"], 3)
        self.assertEqual(ps[1]["new_start"], 6, "第二个 patch 应累积 -1 的漂移（7 + (-1)）")
        self.assertEqual(ps[1]["new_end"], 7)

        # block span 已重映射：原 [0,17) → 新 [0,15)
        b = got["blocks"][0]
        self.assertEqual((b["char_start"], b["char_end"]), (0, 15))
        self.assertEqual(got["text"][b["char_start"]:b["char_end"]], "特斯拉 公司 成立于2003年")

    def test_rejects_overlapping_patches(self) -> None:
        run = _run_dir()
        data = {
            "doc_id": "d", "source": {}, "text": "abcdefghij",
            "blocks": [], "mentions": [],
            "patches": [
                {"orig_start": 0, "orig_end": 4, "kind": "other", "original": "abcd", "replacement": "x"},
                {"orig_start": 2, "orig_end": 6, "kind": "other", "original": "cdef", "replacement": "y"},
            ],
        }
        p = run / "bad.json"
        R.dump_json(str(p), data)
        # 重叠的第二个 patch 应被跳过（而非产出错误的几何）
        R.cmd_normalize_apply(str(p))
        got = R.load_json(str(p))
        self.assertTrue(any("重叠" in w for w in got.get("warnings", [])),
                        "重叠 patch 必须告警，绝不静默")


class TestFallback(unittest.TestCase):
    """降级必须出声；零规则下确定性抽取已删除。"""

    def test_fallback_parse_registers_degradation(self) -> None:
        run = _run_dir()
        R.cmd_ingest([f"text://{SAMPLE}"], str(run / "01-raw.json"))
        R.cmd_fallback("parse", str(run / "01-raw.json"), str(run / "02-parsed.json"))
        got = R.load_json(str(run / "02-parsed.json"))
        degs = got.get("degradations", [])
        self.assertTrue(degs, "fallback 必须登记 Degradation")
        self.assertEqual(degs[0]["component"], "parse.llm")
        self.assertEqual(degs[0]["kind"], "unavailable")
        self.assertTrue(degs[0]["fallback"])

    def test_fallback_extract_is_removed(self) -> None:
        """零规则架构：确定性抽取兜底已删除，fallback extract 必须报错。"""
        run = _run_dir()
        R.cmd_ingest([f"text://{SAMPLE}"], str(run / "01-raw.json"))
        R.cmd_fallback("parse", str(run / "01-raw.json"), str(run / "02-parsed.json"))
        R.cmd_fallback("normalize", str(run / "02-parsed.json"), str(run / "03-normalized.json"))
        self.assertEqual(R.cmd_fallback("extract", str(run / "03-normalized.json"),
                                        str(run / "04-extraction.json")), 2)

    def test_no_llm_extract_returns_empty_and_degrades(self) -> None:
        """薄壳在无 LLM 时不造假：空结果 + Degradation(no_credential)。"""
        from smini.protocols import RunContext
        from smini.steps.extract import ExtractStep
        from smini.types import NormalizedDocument, SourceRef, SourceType

        nd = NormalizedDocument(
            doc_id="d1",
            source=SourceRef.of("memory://inline/x", SourceType.TEXT),
            text=SAMPLE,
        )
        res = ExtractStep().transform([nd], RunContext(llm=None))[0]
        self.assertEqual(res.stats["mode"], "empty")
        self.assertEqual(res.mentions, [])
        self.assertEqual(res.degradations[0].kind.value, "no_credential")


class TestSchemaValidator(unittest.TestCase):
    """轻量 Schema 校验器自身。"""

    def test_ref_resolution_in_nested_schema(self) -> None:
        schema = R.load_schema("raw")
        data = [{
            "doc_id": "d1",
            "source": {"source_id": "s", "uri": "memory://inline/x", "source_type": "text"},
            "checksum": "0" * 64,
        }]
        self.assertEqual(R.validate_against_schema(data, schema), [])

    def test_catches_bad_checksum_pattern(self) -> None:
        schema = R.load_schema("raw")
        data = [{
            "doc_id": "d1",
            "source": {"source_id": "s", "uri": "u", "source_type": "text"},
            "checksum": "not-hex",
        }]
        errs = R.validate_against_schema(data, schema)
        self.assertTrue(errs, "checksum 非 sha256 应被拦下")
        self.assertTrue(any("checksum" in e for e in errs))

    def test_all_contracts_load(self) -> None:
        for name in ("raw", "parsed", "normalized", "extraction", "graph"):
            self.assertIn("title", R.load_schema(name))


class TestRevive(unittest.TestCase):
    """dict → dataclass 反序列化（to_dict 的反向）。"""

    def test_roundtrip_extraction_result(self) -> None:
        run = _run_dir()
        _pipeline(run)

        data = R.load_json(str(run / "04-extraction.json"))
        obj = R.revive(__import__("smini.types", fromlist=["ExtractionResult"]).ExtractionResult, data)
        from smini.types import to_dict
        again = json.loads(json.dumps(to_dict(obj), default=str))
        self.assertEqual(set(again), set(data))
        self.assertEqual(again["mentions"], data["mentions"])
        self.assertEqual(again["relations"], data["relations"])

    def test_revive_handles_enum_datetime_bytes(self) -> None:
        from smini import types as T
        raw = b"hello"
        src = {
            "source_id": "s1",
            "uri": "memory://inline/abc",
            "source_type": "text",
            "fetched_at": "2000-01-01T00:00:00+00:00",
        }
        s = R.revive(T.SourceRef, src)
        self.assertEqual(s.source_type, T.SourceType.TEXT)
        self.assertIsNotNone(s.fetched_at)

        rd = {
            "doc_id": "d1", "source": src, "checksum": "0" * 64,
            "content": base64.b64encode(raw).decode("ascii"), "size_bytes": len(raw),
        }
        d = R.revive(T.RawDocument, rd)
        self.assertEqual(d.content, raw, "base64 content 应还原为 bytes")


class TestIdempotence(unittest.TestCase):
    """★ 验收标准：同输入连跑两次，ID 集合逐字节一致。"""

    def test_two_runs_identical(self) -> None:
        a, b = _run_dir(), _run_dir()
        _pipeline(a)
        _pipeline(b)
        ga = R.load_json(str(a / "05-graph.json"))
        gb = R.load_json(str(b / "05-graph.json"))
        self.assertEqual(set(ga["entities"]), set(gb["entities"]))
        self.assertEqual(set(ga["edges"]), set(gb["edges"]))
        self.assertEqual(
            json.dumps(ga["entities"], sort_keys=True, ensure_ascii=False),
            json.dumps(gb["entities"], sort_keys=True, ensure_ascii=False),
        )

    def test_store_twice_writes_nothing_second_time(self) -> None:
        run = _run_dir()
        _pipeline(run)
        R.cmd_graph_qa(str(run / "05-graph.json"), str(run / "06-qa.json"))
        R.cmd_graph_store(str(run / "06-qa.json"), str(run / "07a.json"), "json")
        R.cmd_graph_store(str(run / "06-qa.json"), str(run / "07b.json"), "json")
        r1 = R.load_json(str(run / "07a.json"))
        r2 = R.load_json(str(run / "07b.json"))
        self.assertTrue(r1["entities_written"] > 0)
        self.assertEqual(r2["entities_written"], 0, "第二次落库不得新增实体")
        self.assertEqual(r2["edges_written"], 0, "第二次落库不得新增边")
        self.assertTrue(r2["idempotent"])

    def test_qa_gate_blocks_store(self) -> None:
        """QA 未决冲突 > 0 → Store 必须拒绝。"""
        run = _run_dir()
        _pipeline(run)
        qa = R.load_json(str(run / "05-graph.json"))
        bad = {"graph": qa, "metrics": {"unresolved_count": 1, "conflict_count": 1}}
        p = run / "06-bad.json"
        R.dump_json(str(p), bad)
        self.assertEqual(R.cmd_graph_store(str(p), str(run / "07-bad.json"), "memory"), 1)


if __name__ == "__main__":
    unittest.main()
