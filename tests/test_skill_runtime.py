"""tests/test_skill_runtime.py — Skill-First 架构下 Python 运行时契约（零规则版）。

验证「LLM 按契约交卷 → Python 薄壳 + 确定性锚点」这条接缝：

  * 薄壳（ExtractStep + StubLLM）产出 extraction.json，ID 由 sha256 补算，
    跨进程幂等；`ids extract` 对空 ID 产物补算后与薄壳产物逐字节一致
  * Schema 校验能拦住 LLM 的典型错误（漏设 object_literal / span 错位 / 枚举越界）
  * 消费层建图（BuildKGStep / `graph build`）把抽取结果收敛为幂等图谱
  * dict → dataclass 反序列化（revive）可还原抽取产物

extract-only 架构：解析、归一化、质检、存储、交付命令已删除，本文件不再
覆盖旧链路（fallback parse/normalize / store / qa 等）。
"""

from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path

from smini import runtime as R
from smini.llm import StubLLMProvider

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
        return dict(_LLM_EXTRACT)

    return StubLLMProvider(respond)


def _pipeline(run: Path, source: str = SAMPLE) -> None:
    """extract-only 链路：构造 NormalizedDocument → 薄壳 extract → ids → graph build。"""
    from smini.protocols import RunContext
    from smini.steps.extract import ExtractStep
    from smini.types import NormalizedDocument, SourceRef, SourceType, to_dict

    nd = NormalizedDocument(
        doc_id="d1",
        source=SourceRef.of("memory://inline/test", SourceType.TEXT),
        text=source,
    )
    res = ExtractStep().transform([nd], RunContext(llm=_stub()))[0]
    R.dump_json(str(run / "03-normalized.json"), to_dict(nd))
    R.dump_json(str(run / "04-extraction.json"), to_dict(res))
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
        self.assertEqual(R.cmd_validate("extraction", str(self.run / "04-extraction.json")), 0)
        self.assertEqual(R.cmd_validate("extract", str(self.run / "04-extraction.json")), 0)

    def test_rejects_missing_object_literal(self) -> None:
        """DATE/MONEY 宾语未设 object_literal → 必须报错（消费层虽会修正，校验仍出声）。"""
        p = self.run / "bad.json"
        data = R.load_json(str(self.run / "04-extraction.json"))
        for t in data.get("triplets", []):
            if t.get("object_type") in ("DATE", "MONEY", "PERCENT", "QUANTITY"):
                t["object_literal"] = False
        R.dump_json(str(p), data)
        self.assertEqual(R.cmd_validate("extraction", str(p)), 1)

    def test_rejects_missing_required_field(self) -> None:
        """mentions 缺 required 字段（normalized）→ Schema 校验必须报错。"""
        p = self.run / "bad2.json"
        data = R.load_json(str(self.run / "04-extraction.json"))
        if data.get("mentions"):
            data["mentions"][0].pop("normalized", None)
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


class TestSchemaValidator(unittest.TestCase):
    """轻量 Schema 校验器自身（extraction 契约）。"""

    def test_extraction_schema_loads(self) -> None:
        self.assertIn("title", R.load_schema("extraction"))


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


if __name__ == "__main__":
    unittest.main()
