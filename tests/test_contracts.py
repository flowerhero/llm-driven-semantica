"""契约自验证测试。

目的不是测算法（实现还没写），而是验证**契约本身兑现了它的承诺**。
每条测试都对应文档里的一条声明；如果哪天有人改坏了契约，这里会先炸。

运行：
    python -m unittest discover -s tests -v
或：
    python tests/test_contracts.py

刻意只用标准库 unittest —— 契约层不应依赖 pytest。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smini import (  # noqa: E402
    Chunk,
    Conflict,
    ConflictKind,
    ContractViolation,
    Degradation,
    DegradationKind,
    DimensionMismatch,
    DocumentFormat,
    DuplicateCluster,
    Entity,
    EntityMention,
    EntityType,
    ExtractionResult,
    KGEdge,
    KnowledgeGraph,
    MergeStrategy,
    NormalizedDocument,
    ParsedDocument,
    Pipeline,
    PipelineState,
    PipelineStep,
    Provenance,
    QAResult,
    RawDocument,
    Resolution,
    ResolutionStrategy,
    RunContext,
    SearchHit,
    SourceRef,
    SourceType,
    SpanPatch,
    StoreReceipt,
    TimeInterval,
    BiTemporal,
    VectorRecord,
    VectorStore,
    Embedder,
    LLMProvider,
    map_span_back,
    stable_id,
    to_dict,
    validate_patches,
)
from smini.ids import chunk_id, doc_id, edge_id, entity_id, mention_id  # noqa: E402

PY = sys.executable


class TestDeterministicIds(unittest.TestCase):
    """契约承诺 1：ID 由内容决定，跨进程、跨 PYTHONHASHSEED 严格一致。"""

    def test_stable_id_same_input_same_output(self):
        a = stable_id("PERSON", "Ada Lovelace", prefix="e_")
        b = stable_id("PERSON", "Ada Lovelace", prefix="e_")
        self.assertEqual(a, b)

    def test_stable_id_differs_on_any_field(self):
        self.assertNotEqual(stable_id("PERSON", "Ada"), stable_id("ORG", "Ada"))
        self.assertNotEqual(stable_id("PERSON", "Ada"), stable_id("PERSON", "Ada B"))

    def test_separator_prevents_concat_collision(self):
        """('a:b','c') 与 ('a','b:c') 必须不同 —— 分隔符不是业务字符。"""
        self.assertNotEqual(stable_id("a:b", "c"), stable_id("a", "b:c"))

    def test_field_order_matters(self):
        self.assertNotEqual(stable_id("x", "y"), stable_id("y", "x"))

    def test_none_vs_empty_string_distinct(self):
        self.assertNotEqual(stable_id(None), stable_id(""))

    def test_cross_process_stability(self):
        """核心验证：不同 PYTHONHASHSEED 的子进程必须算出同一个 ID。

        这是原版 `str(hash(...))` 做不到的 —— 那条路在这里必然失败。
        """
        code = (
            "import sys; sys.path.insert(0, %r); "
            "from smini.ids import entity_id; "
            "print(entity_id('PERSON', 'Ada Lovelace'))" % str(
                Path(__file__).resolve().parent.parent
            )
        )
        outs = set()
        for seed in ("0", "1", "12345", "random"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            r = subprocess.run(
                [PY, "-c", code], capture_output=True, text=True, env=env, check=True
            )
            outs.add(r.stdout.strip())
        self.assertEqual(len(outs), 1, f"跨进程 ID 不一致：{outs}")

    def test_hash_builtin_is_indeed_unstable(self):
        """反证：确认内置 hash() 确实不稳定，证明上面的修复不是多余的。

        故意用 'random' seed 跑多轮。若某轮恰好相同，测试跳过而非失败
        —— 我们证明的是"hash 可能不稳定"，不是"一定不稳定"。
        """
        code = "print(hash('Ada Lovelace'))"
        vals = set()
        for _ in range(6):
            env = {**os.environ, "PYTHONHASHSEED": "random"}
            r = subprocess.run(
                [PY, "-c", code], capture_output=True, text=True, env=env, check=True
            )
            vals.add(r.stdout.strip())
        if len(vals) > 1:
            self.assertTrue(True, "已复现内置 hash 的跨进程不稳定性")
        else:
            self.skipTest("本轮 hash 恰好全部相同（随机种子撞了），不具反证力")

    # -- 语义化构造器 -------------------------------------------------------
    def test_entity_id_ignores_case_and_whitespace(self):
        self.assertEqual(
            entity_id("PERSON", "  Ada Lovelace "),
            entity_id("PERSON", "ada lovelace"),
        )

    def test_doc_id_is_content_addressed(self):
        """同一份内容换文件名，ID 不变 —— 能识别重复来源。"""
        self.assertEqual(
            doc_id("file:///a.pdf", b"hello"),
            doc_id("file:///b.pdf", b"hello"),
        )
        self.assertNotEqual(
            doc_id("file:///a.pdf", b"hello"),
            doc_id("file:///a.pdf", b"world"),
        )

    def test_edge_id_requires_object(self):
        with self.assertRaises(ValueError):
            edge_id("s1", "knows")


class TestProvenance(unittest.TestCase):
    """契约承诺 2：溯源必填且不可变，不变量被破坏立刻报错。"""

    def setUp(self):
        self.src = SourceRef.of("file:///a.pdf", SourceType.FILE)

    def test_confidence_range_enforced(self):
        Provenance(confidence=0.5)
        Provenance(confidence=0.0)
        Provenance(confidence=1.0)
        with self.assertRaises(ContractViolation):
            Provenance(confidence=1.5)
        with self.assertRaises(ContractViolation):
            Provenance(confidence=-0.1)

    def test_char_span_enforced(self):
        Provenance(char_span=(0, 5))
        with self.assertRaises(ContractViolation):
            Provenance(char_span=(5, 2))
        with self.assertRaises(ContractViolation):
            Provenance(char_span=(-1, 5))

    def test_merge_takes_max_confidence_and_union_sources(self):
        other = SourceRef.of("https://x.com", SourceType.WEB)
        p1 = Provenance(source_refs=(self.src,), confidence=0.6, extractor="a")
        p2 = Provenance(source_refs=(other,), confidence=0.9, extractor="b")
        m = p1.merge(p2)
        self.assertEqual(len(m.source_refs), 2)
        self.assertEqual(m.confidence, 0.9)

    def test_merge_dedups_same_source(self):
        p = Provenance(source_refs=(self.src,))
        self.assertEqual(len(p.merge(Provenance(source_refs=(self.src,))).source_refs), 1)

    def test_source_ref_requires_uri(self):
        with self.assertRaises(ContractViolation):
            SourceRef(source_id="s_1", uri="", source_type=SourceType.FILE)


class TestSpanMapping(unittest.TestCase):
    """契约承诺 3：归一化造成的坐标漂移可逆，溯源链能闭合到原文。"""

    def test_no_patches_is_identity(self):
        self.assertEqual(map_span_back((3, 10), []), (3, 10))

    def test_single_replacement_shorter(self):
        """原文 "2026年3月5日" (10字) → 新文 "2026-03-05" (10字)，等长。"""
        patches = [SpanPatch(0, 10, 0, 10, "date", "2026年3月5日", "2026-03-05")]
        self.assertEqual(map_span_back((0, 10), patches), (0, 10))

    def test_replacement_grows_text(self):
        """原文变长，其后内容在 orig 坐标系中要往右推。

        orig: "cost 1K USD"    "1K" 在 [5:7]，"USD" 在 [8:11]
        new:  "cost 1000 USD"  "1000" 在 [5:9]，"USD" 在 [10:13]
        累积漂移 +2
        """
        patches = [SpanPatch(5, 7, 5, 9, "number", "1K", "1000")]
        validate_patches(patches)          # 先自证测试数据自洽
        self.assertEqual(map_span_back((10, 13), patches), (8, 11))

    def test_replacement_shrinks_text(self):
        """原文变短，其后内容往左移。

        orig: "Ada   Lovelace"  "Lovelace" 在 [6:14]
        new:  "Ada Lovelace"    "Lovelace" 在 [4:12]
        累积漂移 -2
        """
        patches = [SpanPatch(3, 6, 3, 4, "whitespace", "   ", " ")]
        validate_patches(patches)
        self.assertEqual(map_span_back((4, 12), patches), (6, 14))

    def test_multiple_patches_accumulate(self):
        """两个 patch 的漂移必须累积，不能只算最后一个。

        orig: "abXYZcdefghijkl" (len 15)
          patch1  orig[2:5]="XYZ" → "W"，new[2:3]        漂移 -2
          patch2  orig[8:11]="fgh" → "Q"，new[6:7]       漂移 -2（累计 -4）
        new:  "abWcdeQijkl" (len 11)
        new 中 "ijkl" 在 [7:11]  ⇒  orig 中 "ijkl" 在 [11:15]
        """
        patches = [
            SpanPatch(2, 5, 2, 3, "alias", "XYZ", "W"),
            SpanPatch(8, 11, 6, 7, "alias", "fgh", "Q"),
        ]
        validate_patches(patches)
        self.assertEqual(map_span_back((7, 11), patches), (11, 15))

    def test_span_inside_replacement_is_conservative(self):
        """落在替换区内部的偏移无法精确还原 —— 必须保守包含整个替换区。"""
        patches = [SpanPatch(0, 20, 0, 5, "alias", "International Business Machines", "IBM")]
        validate_patches(patches)
        self.assertEqual(map_span_back((1, 4), patches), (0, 20))

    def test_roundtrip_via_document(self):
        """端到端：norm 区间 → 映射回原文 → 取到正确的原文片段。"""
        parsed = ParsedDocument(
            doc_id="d_1",
            format=DocumentFormat.TEXT,
            source=SourceRef.of("memory://x", SourceType.TEXT),
            text="cost 1K USD",
        )
        norm = NormalizedDocument(
            doc_id="d_1",
            source=parsed.source,
            text="cost 1000 USD",
            patches=[SpanPatch(5, 7, 5, 9, "number", "1K", "1000")],
        )
        validate_patches(norm.patches)
        # norm 文本中 "USD" 在 [10:13]，映射回原文取到 "USD"
        self.assertEqual(norm.original_text(parsed, (10, 13)), "USD")

    def test_validate_patches_catches_inconsistent_coords(self):
        """new 坐标与累积漂移对不上时必须报错。

        这是构造 SpanPatch 数据最容易犯的错：patch1 让文本变长 1，
        patch2 的 new_start 就应该是 11 而不是 12。写错了映射会静默
        偏移几个字符，极难排查 —— 所以必须在入口拦住。
        """
        bad = [
            SpanPatch(5, 8, 5, 9, "number", "1K", "1500"),
            SpanPatch(10, 14, 12, 13, "whitespace", "    ", " "),
        ]
        with self.assertRaises(ContractViolation):
            validate_patches(bad)

        good = [
            SpanPatch(5, 8, 5, 9, "number", "1K", "1500"),
            SpanPatch(10, 14, 11, 12, "whitespace", "    ", " "),
        ]
        validate_patches(good)  # 不抛

    def test_validate_patches_catches_overlap(self):
        bad = [
            SpanPatch(0, 10, 0, 10, "a", "x", "y"),
            SpanPatch(20, 25, 5, 6, "b", "x", "y"),   # new 区间与前一个重叠
        ]
        with self.assertRaises(ContractViolation):
            validate_patches(bad)


class TestKnowledgeGraph(unittest.TestCase):
    """契约承诺 4：建图幂等，合并可重复执行且不丢信息。"""

    def _entity(self, name, **kw):
        return Entity(
            entity_id=entity_id("PERSON", name),
            canonical_name=name,
            entity_type=EntityType.PERSON,
            **kw,
        )

    def test_add_entity_is_idempotent(self):
        g = KnowledgeGraph()
        g.add_entity(self._entity("Ada Lovelace"))
        g.add_entity(self._entity("Ada Lovelace"))
        self.assertEqual(len(g.entities), 1)

    def test_merge_preserves_alias(self):
        """被合并方的名字必须进 aliases —— 否则用旧名字就查不到了。

        semantica 原版的 entity_merger 在合并时会丢掉别名，这是这里
        刻意修掉的行为。

        注意场景：两个实体 ID 不同（表面形式不同），是在 QA 步被判定为
        同一实体后才调 merge_with 合并 —— 不是靠 ID 碰撞。
        """
        canonical = Entity(
            entity_id=entity_id("PERSON", "Ada Lovelace"),
            canonical_name="Ada Lovelace",
            entity_type=EntityType.PERSON,
        )
        variant = Entity(
            entity_id=entity_id("PERSON", "A. Lovelace"),
            canonical_name="A. Lovelace",
            entity_type=EntityType.PERSON,
            aliases=["Ada Byron"],
        )
        self.assertNotEqual(canonical.entity_id, variant.entity_id)

        canonical.merge_with(variant, MergeStrategy.KEEP_MOST_COMPLETE)
        lowered = {a.casefold() for a in canonical.aliases}
        self.assertIn("a. lovelace", lowered, "被合并方的规范名必须保留为别名")
        self.assertIn("ada byron", lowered, "被合并方已有的别名必须继承")

    def test_merge_does_not_duplicate_alias(self):
        e1 = Entity("e_1", "Ada", EntityType.PERSON, aliases=["Ada Byron"])
        e2 = Entity("e_2", "Ada", EntityType.PERSON, aliases=["ada byron", "A. Lovelace"])
        e1.merge_with(e2, MergeStrategy.KEEP_MOST_COMPLETE)
        self.assertEqual(
            len([a for a in e1.aliases if a.casefold() == "ada byron"]), 1
        )

    def test_merge_keeps_most_complete_property(self):
        e1 = Entity("e_1", "X", EntityType.PERSON, properties={"birth": None})
        e2 = Entity("e_1", "X", EntityType.PERSON, properties={"birth": "1815"})
        e1.merge_with(e2, MergeStrategy.KEEP_MOST_COMPLETE)
        self.assertEqual(e1.properties["birth"], "1815")

    def test_edge_requires_object(self):
        with self.assertRaises(ContractViolation):
            KGEdge(edge_id="r_1", subject_id="e_1", predicate="knows")

    def test_add_edge_is_idempotent_and_raises_confidence(self):
        g = KnowledgeGraph()
        base = KGEdge(
            edge_id=edge_id("e_1", "knows", "e_2"),
            subject_id="e_1", predicate="knows", object_id="e_2", confidence=0.5,
        )
        g.add_edge(base)
        g.add_edge(
            KGEdge(
                edge_id=edge_id("e_1", "knows", "e_2"),
                subject_id="e_1", predicate="knows", object_id="e_2", confidence=0.9,
            )
        )
        self.assertEqual(len(g.edges), 1)
        # 多版本下 edges[id] 是版本列表；temporal=None 的两次写入视为
        # 同一版本，应合并而非追加，故当前视图只有 1 条且置信度取最大。
        self.assertEqual(
            g.edges[edge_id("e_1", "knows", "e_2")][0].confidence, 0.9
        )

    def test_stats_count_orphans(self):
        g = KnowledgeGraph()
        g.add_entity(self._entity("Ada Lovelace"))
        g.add_entity(self._entity("Lonely Node"))
        g.add_edge(
            KGEdge(
                edge_id=edge_id("a", "b", object_literal="c"),
                subject_id=entity_id("PERSON", "Ada Lovelace"),
                predicate="born_in",
                object_literal="1815",
            )
        )
        s = g.recompute_stats()
        self.assertEqual(s.entity_count, 2)
        self.assertEqual(s.orphan_entity_count, 1)
        self.assertEqual(s.literal_edge_count, 1)


class TestBiTemporal(unittest.TestCase):
    """双时态契约：事实可以有多个时间版本，且能按时点回看历史。

    这是相对 semantica 原版新增的能力（原版只在 ConflictValue 留了
    valid_from/valid_to，没有真正的双时态查询）。每条测试对应 types.py
    里 `BiTemporal` / `KnowledgeGraph.edges` 多版本 / `at()` 的一条行为。
    """

    def _e(self, subject, predicate, obj, valid):
        return KGEdge(
            edge_id=edge_id(subject, predicate, obj),
            subject_id=subject, predicate=predicate, object_id=obj,
            temporal=BiTemporal(valid=TimeInterval.since(valid), recorded_at=valid),
        )

    def test_time_interval_contains_half_open(self):
        """半开区间 [start, end)：右端本身不属于区间。"""
        iv = TimeInterval(datetime(2020, 1, 1, tzinfo=timezone.utc),
                          datetime(2023, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(iv.contains(datetime(2022, 6, 1, tzinfo=timezone.utc)))
        self.assertFalse(iv.contains(datetime(2023, 1, 1, tzinfo=timezone.utc)))
        self.assertTrue(iv.contains(datetime(2020, 1, 1, tzinfo=timezone.utc)))

    def test_time_interval_open_ended_contains_everything_after(self):
        iv = TimeInterval.since(datetime(2020, 1, 1, tzinfo=timezone.utc))
        self.assertTrue(iv.contains(datetime(2099, 1, 1, tzinfo=timezone.utc)))

    def test_bitemporal_valid_vs_known(self):
        """valid_time 与 transaction_time 是两个独立维度。"""
        bt = BiTemporal(
            valid=TimeInterval.since(datetime(2020, 1, 1, tzinfo=timezone.utc)),
            recorded_at=datetime(2022, 1, 1, tzinfo=timezone.utc),
        )
        # 2021 年：事实在现实中已为真，但系统还没录入 → 不算 known
        self.assertTrue(bt.valid_at(datetime(2021, 6, 1, tzinfo=timezone.utc)))
        self.assertFalse(bt.known_at(datetime(2021, 6, 1, tzinfo=timezone.utc)))
        # 2023 年：已录入，且仍为真
        self.assertTrue(bt.known_at(datetime(2023, 6, 1, tzinfo=timezone.utc)))

    def test_add_edge_appends_new_version_when_temporal_differs(self):
        """不同双时态版本 → 追加而非合并（这是双时态能成立的基础）。"""
        g = KnowledgeGraph()
        t1 = datetime(2020, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2023, 1, 1, tzinfo=timezone.utc)
        g.add_edge(self._e("e_1", "employed_by", "acme", t1))
        g.add_edge(self._e("e_1", "employed_by", "startup", t2))
        versions = g.edges[edge_id("e_1", "employed_by", "acme")]
        # acme 与 startup 是不同 object → 不同 edge_id，各自 1 个版本
        self.assertEqual(len(g.edges), 2)
        # 同 edge_id 但 temporal 不同 → 追加为 2 个版本
        g.add_edge(self._e("e_1", "employed_by", "acme", t2))
        self.assertEqual(len(g.edges[edge_id("e_1", "employed_by", "acme")]), 2)

    def test_add_edge_merges_same_version(self):
        """相同双时态版本 → 合并置信度（取最大），不新增版本。"""
        g = KnowledgeGraph()
        t = datetime(2020, 1, 1, tzinfo=timezone.utc)
        g.add_edge(KGEdge(
            edge_id=edge_id("e_1", "employed_by", "acme"),
            subject_id="e_1", predicate="employed_by", object_id="acme",
            confidence=0.5,
            temporal=BiTemporal(valid=TimeInterval.since(t), recorded_at=t),
        ))
        g.add_edge(KGEdge(
            edge_id=edge_id("e_1", "employed_by", "acme"),
            subject_id="e_1", predicate="employed_by", object_id="acme",
            confidence=0.9,
            temporal=BiTemporal(valid=TimeInterval.since(t), recorded_at=t),
        ))
        versions = g.edges[edge_id("e_1", "employed_by", "acme")]
        self.assertEqual(len(versions), 1)
        self.assertEqual(versions[0].confidence, 0.9)

    def test_at_snapshot_returns_historical_view(self):
        """at(valid_time) 只返回该时点成立的事实。

        成功建模：acme 用闭合区间 [2020,2023)，startup 用 since(2023)。
        若两者都写成 since(...) 会同时"有效"，那本身就是矛盾事实，
        不应压成一条 —— 正确做法是用不重叠的 valid_time 表达"先后任职"。
        """
        g = KnowledgeGraph()
        t1 = datetime(2020, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2023, 1, 1, tzinfo=timezone.utc)
        g.add_edge(KGEdge(
            edge_id=edge_id("e_1", "employed_by", "acme"),
            subject_id="e_1", predicate="employed_by", object_id="acme",
            temporal=BiTemporal(valid=TimeInterval(t1, t2), recorded_at=t1),
        ))
        g.add_edge(self._e("e_1", "employed_by", "startup", t2))
        snap_2021 = g.at(datetime(2021, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(len(snap_2021.current_edges()), 1)
        self.assertEqual(snap_2021.current_edges()[0].object_id, "acme")
        snap_2024 = g.at(datetime(2024, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(len(snap_2024.current_edges()), 1)
        self.assertEqual(snap_2024.current_edges()[0].object_id, "startup")

    def test_supersede_invalidates_old_version(self):
        """作废旧版本后仍能在历史时点查到它。"""
        g = KnowledgeGraph()
        t1 = datetime(2020, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2023, 1, 1, tzinfo=timezone.utc)
        old = self._e("e_1", "title", "CTO", t1)
        g.add_edge(old)
        g.supersede(edge_id("e_1", "title", "CTO"),
                    KGEdge(edge_id=edge_id("e_1", "title", "VP"),
                           subject_id="e_1", predicate="title", object_id="VP",
                           temporal=BiTemporal(valid=TimeInterval.since(t2),
                                               recorded_at=t2)),
                    at=t2)
        self.assertFalse(old.is_current)
        snap = g.at(datetime(2021, 6, 1, tzinfo=timezone.utc))
        self.assertEqual(snap.current_edges()[0].object_id, "CTO")



class TestPipelineContract(unittest.TestCase):
    """契约承诺 5：步骤顺序从 reads/writes 推导，构造期就能发现错误。"""

    def _step(self, name, reads, writes, out_factory):
        class _S(PipelineStep):
            def select(self, state):
                return [getattr(state, f) for f in self.reads]

            def transform(self, inputs, ctx):
                return out_factory()

            def commit(self, state, output):
                for f in self.writes:
                    setattr(state, f, output)

        # 类属性赋值不会清除 ABCMeta 的抽象标记，必须手动置空
        _S.name = name
        _S.reads = reads
        _S.writes = writes
        _S.__abstractmethods__ = frozenset()
        return _S()

    def test_valid_order_passes(self):
        p = Pipeline(steps=[
            self._step("a", (), ("raw",), lambda: [1]),
            self._step("b", ("raw",), ("parsed",), lambda: [2]),
        ])
        self.assertEqual(len(p.steps), 2)

    def test_missing_producer_is_rejected(self):
        """读了没人生产的字段 —— 必须报错。"""
        with self.assertRaises(Exception) as cm:
            Pipeline(steps=[self._step("b", ("raw",), ("parsed",), lambda: [2])])
        self.assertIn("raw", str(cm.exception))

    def test_duplicate_writer_is_rejected(self):
        with self.assertRaises(Exception) as cm:
            Pipeline(steps=[
                self._step("a", (), ("raw",), lambda: [1]),
                self._step("a2", (), ("raw",), lambda: [1]),
            ])
        self.assertIn("重复写入", str(cm.exception))

    def test_unnamed_step_rejected(self):
        with self.assertRaises(Exception):
            Pipeline(steps=[self._step("", (), ("raw",), lambda: [1])])

    def test_empty_upstream_aborts(self):
        """上游产出 0 条时立刻失败，不让空数据静默流向下游。"""
        class Zero(PipelineStep):
            name = "zero"
            reads = ()
            writes = ("raw",)
            def select(self, state): return None
            def transform(self, inputs, ctx): return []
            def commit(self, state, output): state.raw = output

        class Next(PipelineStep):
            name = "next"
            reads = ("raw",)
            writes = ("parsed",)
            def select(self, state): return state.raw
            def transform(self, inputs, ctx): return [1]
            def commit(self, state, output): state.parsed = output

        p = Pipeline(steps=[Zero(), Next()])
        with self.assertRaises(Exception) as cm:
            p.run()
        self.assertIn("0 条", str(cm.exception))

    def test_disabled_step_skipped(self):
        class Off(PipelineStep):
            name = "off"
            reads = ()
            writes = ("raw",)
            def select(self, state): return None
            def transform(self, inputs, ctx): return [1]
            def commit(self, state, output): state.raw = output
            def enabled(self, ctx): return False

        p = Pipeline(steps=[Off()])
        state, results = p.run()
        self.assertEqual(results[0].output, None)

    def test_run_returns_state_and_results(self):
        class A(PipelineStep):
            name = "a"
            reads = ()
            writes = ("raw",)
            def select(self, state): return None
            def transform(self, inputs, ctx): return [1, 2, 3]
            def commit(self, state, output): state.raw = output

        state, results = Pipeline(steps=[A()]).run()
        self.assertEqual(len(state.raw), 3)
        self.assertEqual(results[0].items_out, 3)

    def test_injected_clock_used(self):
        """时间由 ctx 注入，不取系统时钟 —— 保证可复现。"""
        fixed = datetime(2020, 1, 1, tzinfo=timezone.utc)

        class A(PipelineStep):
            name = "a"
            reads = ()
            writes = ("raw",)
            def select(self, state): return None
            def transform(self, inputs, ctx):
                self.seen = ctx.now()
                return [1]
            def commit(self, state, output): state.raw = output

        s = A()
        ctx = RunContext(now=lambda: fixed)
        Pipeline(steps=[s]).run(ctx=ctx)
        self.assertEqual(s.seen, fixed)


class TestStoreContract(unittest.TestCase):
    """契约承诺 6：向量维度硬校验，绝不让脏数据进索引。"""

    class MemVec(VectorStore):
        def __init__(self, dimension):
            super().__init__(dimension)
            self.data: dict[str, VectorRecord] = {}

        def upsert(self, records):
            self._check_dim(records)
            for r in records:
                self.data[r.vector_id] = r
            return len(records)

        def search(self, query_vector, top_k=10, filters=None):
            self._check_dim([VectorRecord("q", tuple(query_vector), "")])
            return [
                SearchHit(vector_id=r.vector_id, score=1.0, text=r.text,
                          payload=r.payload, rank=i)
                for i, r in enumerate(list(self.data.values())[:top_k])
            ]

    def test_dimension_mismatch_raises(self):
        store = self.MemVec(dimension=384)
        bad = VectorRecord("v1", tuple([0.1] * 768), "text")
        with self.assertRaises(DimensionMismatch):
            store.upsert([bad])

    def test_matching_dimension_ok(self):
        store = self.MemVec(dimension=384)
        ok = VectorRecord("v1", tuple([0.1] * 384), "text")
        self.assertEqual(store.upsert([ok]), 1)

    def test_upsert_is_idempotent(self):
        store = self.MemVec(dimension=4)
        r = VectorRecord("v1", (1.0, 0, 0, 0), "hello")
        store.upsert([r])
        store.upsert([r])
        self.assertEqual(len(store.data), 1)

    def test_receipt_declares_idempotency(self):
        r = StoreReceipt(backend="memory", entities_written=3, idempotent=True)
        self.assertTrue(r.idempotent)


class TestDegradationAndQA(unittest.TestCase):
    """契约承诺 7：降级必须出声；QA 未通过不得入库。"""

    def test_degradation_recorded(self):
        r = ExtractionResult(
            doc_id="d_1",
            degradations=[
                Degradation(
                    component="ner.spacy",
                    kind=DegradationKind.MODEL_MISSING,
                    reason="未安装 en_core_web_sm",
                    fallback="regex_only",
                    impact="仅能识别日期/金额/邮箱等规则实体，人名机构名大量漏召",
                )
            ],
        )
        self.assertTrue(r.is_degraded)

    def test_clean_result_not_degraded(self):
        self.assertFalse(ExtractionResult(doc_id="d_1").is_degraded)

    def test_qa_gate(self):
        g = KnowledgeGraph()
        unresolved = Conflict(
            conflict_id="x_1",
            kind=ConflictKind.VALUE,
            subject_id="e_1",
            predicate="born_in",
            values=[],
        )
        qa = QAResult(graph=g, conflicts=[unresolved])
        from smini.types import QualityMetrics
        qa.metrics = QualityMetrics(unresolved_count=1)
        self.assertFalse(qa.passed, "存在未裁决冲突时 Store 必须拒绝入库")

        resolved = Conflict(
            conflict_id="x_2",
            kind=ConflictKind.VALUE,
            subject_id="e_1",
            predicate="born_in",
            resolution=Resolution(
                strategy=ResolutionStrategy.MOST_RECENT,
                chosen="1815",
                rationale="来源更新时间最新",
            ),
        )
        qa2 = QAResult(graph=g, conflicts=[resolved])
        self.assertTrue(qa2.passed)


class TestSerialization(unittest.TestCase):
    """契约承诺 8：所有结构可 JSON 化，便于落盘与跨进程传递。"""

    def test_to_dict_and_json(self):
        doc = RawDocument(
            doc_id="d_1",
            source=SourceRef.of("file:///a.pdf", SourceType.FILE),
            content=b"hello",
        )
        d = to_dict(doc)
        s = json.dumps(d, ensure_ascii=False)
        self.assertIn("d_1", s)
        self.assertEqual(d["source"]["source_type"], "file")
        # bytes 走 base64，不污染 JSON
        self.assertIn("__bytes__", d["content"])

    def test_enum_serializes_to_value(self):
        self.assertEqual(to_dict(EntityType.PERSON), "PERSON")

    def test_datetime_to_iso(self):
        dt = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(to_dict(dt), "2026-03-05T12:00:00+00:00")

    def test_tuple_key_flattened(self):
        d = to_dict({("a", "b"): 1})
        self.assertEqual(d["a|b"], 1)


class TestCapabilityContracts(unittest.TestCase):
    """能力提供方契约：缺失时抛 DependencyMissing，绝不返回垃圾结果。

    这是相对原版（静默降级）的核心修正。即便 v1 用确定性兜底实现，
    这两个 ABC 的接口与异常语义必须在契约层就位。
    """

    def test_embedder_dimension_must_be_positive(self):
        with self.assertRaises(ValueError):
            class E(Embedder):
                def embed(self, texts): return []
            E(0)

    def test_embedder_rejects_dimension_mismatch(self):
        class E(Embedder):
            def embed(self, texts):
                return [(0.0,) * self.dimension for _ in texts]
        e = E(4)
        with self.assertRaises(DimensionMismatch):
            e._check_dim([(0.0, 1.0)])  # 2 维 ≠ 4 维
        e._check_dim([(0.0,) * 4])  # 同维通过

    def test_llm_abstract_methods_present(self):
        """LLMProvider 的两个抽象方法必须存在且未实现。"""
        with self.assertRaises(TypeError):
            LLMProvider()  # 含抽象方法，不能直接实例化
        self.assertTrue(hasattr(LLMProvider, "is_available"))
        self.assertTrue(hasattr(LLMProvider, "extract"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
