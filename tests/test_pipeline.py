"""端到端流水线测试：用内存 ``text://`` 源 + StubLLMProvider 跑通完整 8 步。

契约即规则架构下，抽取必须有 LLM（零规则无确定性兜底），因此 E2E 全程
注入 ``StubLLMProvider``（测试桩）模拟 LLM 按契约交卷。

验证点：
- 8 步严格按序执行、每步产出字段非空
- 图谱实体/边数量合理（LLM 契约固定 → 数量可断言）
- 字面量边（如「成立于 → 2003年」）正确生成（object_literal 由类型派生）
- QA 结构完整，无冲突、``passed`` 为真
- Store 回执幂等：同一 store 二次 build 不新增实体/边
- Deliver 双向检索：查询作主语与作宾语的实体都能召回对应边
- CLI 子进程：``python -m smini.cli build --sample`` 无 --host-contract 时抽取为空但
  管线完整跑通且明确提示（退出码 0）；传契约文件后抽取成功（宿主充当 LLM，无需任何凭证）
- QA 冲突路径：构造同一 (subject, predicate) 多值时态边，验证检测 + 最近裁决 + 落选作废

只依赖标准库 ``unittest`` —— 与契约测试一致，确保零依赖可跑。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smini import build_pipeline, make_context  # noqa: E402
from smini.ids import edge_id, entity_id  # noqa: E402
from smini.llm import PARSE_SCHEMA, StubLLMProvider  # noqa: E402
from smini.steps import QAStep  # noqa: E402
from smini.stores import MemoryGraphStore  # noqa: E402
from smini.types import (  # noqa: E402
    BiTemporal,
    ConflictKind,
    Entity,
    EntityType,
    KnowledgeGraph,
    KGEdge,
    ResolutionStrategy,
    TimeInterval,
)

_SAMPLE = (
    "特斯拉公司成立于2003年，总部位于美国加利福尼亚州。马斯克是特斯拉公司的CEO。"
    "该公司2023年营收约967亿美元。OpenAI 是一家人工智能公司，总部位于美国旧金山。"
    "山姆·奥特曼是 OpenAI 的 CEO。"
)

# LLM 按契约交卷的抽取结果（无偏移/ID/literal，由薄壳+惰性锚点补算）
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


def _stub() -> StubLLMProvider:
    def respond(prompt: str, schema):
        if schema is PARSE_SCHEMA:
            return {"text": _SAMPLE, "blocks": [{"kind": "paragraph", "text": _SAMPLE}]}
        return dict(_LLM_EXTRACT)

    return StubLLMProvider(respond)


STEP_ORDER = [
    "ingest", "parse", "normalize", "extract",
    "build_kg", "qa", "store", "deliver",
]


def _bit(valid_start: datetime) -> BiTemporal:
    return BiTemporal(valid=TimeInterval(start=valid_start), recorded_at=valid_start)


class PipelineE2ETest(unittest.TestCase):
    # -- 运行辅助（注入 LLM 桩，零规则抽取必需）---------------------------
    def _run(self, sources, query=None):
        ctx = make_context(sources, query, llm=_stub())
        pipeline = build_pipeline(sources, query=query, llm=_stub())
        return pipeline.run(ctx=ctx)

    # -- 1. 8 步顺序执行 --------------------------------------------------
    def test_full_pipeline_runs_8_steps(self):
        state, results = self._run([_SAMPLE], "特斯拉公司")
        self.assertEqual(len(results), 8)
        self.assertEqual([r.step for r in results], STEP_ORDER)
        # run() 在 fail_fast 下若任一步失败会直接抛异常；能走到这里即全步成功

    # -- 2. 中间产物字段非空 ----------------------------------------------
    def test_intermediate_fields_populated(self):
        state, _ = self._run([_SAMPLE], "特斯拉公司")
        self.assertTrue(state.raw, "raw 应为非空")
        self.assertTrue(state.parsed, "parsed 应为非空")
        self.assertTrue(state.normalized, "normalized 应为非空")
        self.assertTrue(state.extractions, "extractions 应为非空")
        self.assertIsNotNone(state.graph)
        self.assertIsNotNone(state.qa)
        self.assertIsNotNone(state.receipt)
        self.assertIsNotNone(state.delivered)

    # -- 3. 图谱被正确构建 -------------------------------------------------
    def test_graph_built(self):
        state, _ = self._run([_SAMPLE])
        g = state.graph
        self.assertGreaterEqual(g.stats.entity_count, 8)
        self.assertGreaterEqual(g.stats.edge_count, 4)
        self.assertGreaterEqual(g.stats.literal_edge_count, 2)
        # 实体类型应覆盖 ORGANIZATION / PERSON / DATE / MONEY / LOCATION
        kinds = set(g.stats.by_type)
        for want in ("ORGANIZATION", "PERSON", "DATE", "MONEY", "LOCATION"):
            self.assertIn(want, kinds, f"图谱缺少实体类型 {want}")

    # -- 4. QA 结构完整 ----------------------------------------------------
    def test_qa_structure(self):
        state, _ = self._run([_SAMPLE])
        qa = state.qa
        self.assertIs(qa.graph, state.graph, "QAResult.graph 应为已修复的图")
        self.assertEqual(qa.metrics.conflict_count, 0)
        self.assertTrue(qa.passed, "默认样例应无未决冲突")
        self.assertEqual(qa.metrics.duplicate_cluster_count, 0)

    # -- 5. Store 幂等 -----------------------------------------------------
    def test_store_idempotent_across_rebuilds(self):
        store = MemoryGraphStore()
        # 第一次构建
        ctx1 = make_context([_SAMPLE], "特斯拉公司", llm=_stub())
        p1 = build_pipeline([_SAMPLE], store=store, query="特斯拉公司", llm=_stub())
        _, res1 = p1.run(ctx=ctx1)
        r1 = res1[6].output  # store 步
        self.assertGreater(r1.entities_written, 0)
        # 第二次构建到同一 store：实体/边应全部命中（updated），written=0
        ctx2 = make_context([_SAMPLE], "特斯拉公司", llm=_stub())
        p2 = build_pipeline([_SAMPLE], store=store, query="特斯拉公司", llm=_stub())
        _, res2 = p2.run(ctx=ctx2)
        r2 = res2[6].output
        self.assertEqual(r2.entities_written, 0, "二次构建不应新增实体")
        self.assertEqual(r2.edges_written, 0, "二次构建不应新增边")
        self.assertTrue(r2.idempotent)

    # -- 6. Deliver 双向检索（宾语视角）-----------------------------------
    def test_deliver_bidirectional_objects(self):
        state, _ = self._run([_SAMPLE], "特斯拉公司")
        pkg = state.delivered
        # 特斯拉公司是「马斯克 —[CEO]→ 特斯拉公司」的宾语，应被双向召回
        ceo_facts = [f for f in pkg.facts if f.predicate == "CEO" and f.object == "特斯拉公司"]
        self.assertTrue(ceo_facts, "查询特斯拉公司应双向召回 CEO 边（特斯拉公司为宾语）")
        self.assertEqual(ceo_facts[0].subject, "马斯克")

    # -- 7. Deliver 主语视角 + 谓词命中 -----------------------------------
    def test_deliver_subject_and_predicate(self):
        state, _ = self._run([_SAMPLE], "特斯拉公司 总部")
        pkg = state.delivered
        located = [f for f in pkg.facts if f.predicate == "总部位于"]
        self.assertTrue(located, "应召回 总部位于 边")
        self.assertEqual(located[0].subject, "特斯拉公司")
        self.assertEqual(located[0].object, "美国加利福尼亚州")

    # -- 8. 幂等：同输入两次实体 ID 集合一致 ---------------------------
    def test_deterministic_entity_ids(self):
        _, r1 = self._run([_SAMPLE])
        _, r2 = self._run([_SAMPLE])
        self.assertEqual(
            set(r1[4].output.entities), set(r2[4].output.entities),
            "同输入应产出内容寻址一致的实体 ID",
        )

    # -- 9. CLI 子进程 -----------------------------------------------------
    def test_cli_subprocess_without_host_contract_runs_empty_extract(self):
        """零规则架构：宿主未交卷（无 --host-contract）时抽取为空，但管线完整跑通并提示。"""
        py = sys.executable
        proc = subprocess.run(
            [py, "-m", "smini.cli", "build", "--sample", "-q", "特斯拉", "--quiet"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("未喂入契约", proc.stderr, "应明确提示抽取为空的原因")

    def test_cli_subprocess_with_host_contract(self):
        """宿主充当 LLM：--host-contract 传入契约 → 抽取成功，不依赖任何凭证。"""
        import tempfile
        contract = json.dumps(_LLM_EXTRACT, ensure_ascii=False)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            f.write(contract)
            cpath = f.name
        try:
            py = sys.executable
            proc = subprocess.run(
                [py, "-m", "smini.cli", "build", "--sample", "--host-contract", cpath,
                 "-q", "特斯拉公司", "--quiet"],
                capture_output=True, text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("实体 /", proc.stdout, "应产出图谱统计")
            self.assertIn("特斯拉公司", proc.stdout, "抽取结果应进入图谱")
        finally:
            import os
            os.unlink(cpath)

    def test_cli_subprocess_host_contract_bad_file(self):
        """契约文件缺失/非法 → 明确报错退出码 2（不静默）。"""
        py = sys.executable
        proc = subprocess.run(
            [py, "-m", "smini.cli", "build", "--sample", "--host-contract",
             "/nonexistent/contract.json", "-q", "特斯拉", "--quiet"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertIn("读取宿主契约失败", proc.stderr)

    # -- 10. QA 冲突路径（构造时态多值）-----------------------------------
    def test_qa_conflict_detection_and_resolution(self):
        subj = Entity(
            entity_id=entity_id("ORGANIZATION", "特斯拉"),
            canonical_name="特斯拉", entity_type=EntityType.ORGANIZATION,
        )
        g = KnowledgeGraph()
        g.add_entity(subj)
        g.add_entity(Entity(entity_id=entity_id("LOCATION", "北京"), canonical_name="北京", entity_type=EntityType.LOCATION))
        g.add_entity(Entity(entity_id=entity_id("LOCATION", "上海"), canonical_name="上海", entity_type=EntityType.LOCATION))
        t1 = datetime(2020, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2021, 1, 1, tzinfo=timezone.utc)
        g.add_edge(KGEdge(
            edge_id=edge_id("特斯拉", "总部", None, "北京"),
            subject_id=subj.entity_id, predicate="总部", object_literal="北京",
            confidence=0.9, temporal=_bit(t1),
        ))
        g.add_edge(KGEdge(
            edge_id=edge_id("特斯拉", "总部", None, "上海"),
            subject_id=subj.entity_id, predicate="总部", object_literal="上海",
            confidence=0.9, temporal=_bit(t2),
        ))
        ctx = make_context(None)
        qa = QAStep().transform(g, ctx)
        # 检测到 1 个 VALUE 冲突
        self.assertEqual(len(qa.conflicts), 1)
        self.assertEqual(qa.conflicts[0].kind, ConflictKind.VALUE)
        # 有时态 → 最近裁决 → 上海 胜出，北京 被作废
        self.assertEqual(qa.conflicts[0].resolution.strategy, ResolutionStrategy.MOST_RECENT)
        self.assertEqual(qa.conflicts[0].resolution.chosen, "上海")
        # 当前视图只剩上海边
        current = {e.object_literal for e in g.current_edges()}
        self.assertEqual(current, {"上海"}, f"落选边应被作废，当前视图：{current}")
        # 历史仍保留（双时态）
        self.assertEqual(len(g.all_edges()), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
