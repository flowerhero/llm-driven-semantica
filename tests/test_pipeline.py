"""端到端流水线测试：用内存 ``text://`` 源 + StubLLMProvider 跑通 extract-only 通路。

契约即规则架构下，抽取必须有 LLM（零规则无确定性兜底），因此 E2E 全程
注入 ``StubLLMProvider``（测试桩）模拟 LLM 按契约交卷。

验证点：
- 3 步严格按序执行（ingest → extract → build_kg）、每步产出字段非空
- 图谱实体/边数量合理（LLM 契约固定 → 数量可断言）
- 字面量边（如「成立于 → 2003年」）正确生成（object_literal 由类型派生）
- 同输入两次构建，实体 ID 集合一致（幂等）
- CLI 子进程：``python -m smini.cli build --sample`` 无 --host-contract 时抽取为空但
  管线完整跑通且明确提示（退出码 0）；传契约文件后抽取成功（宿主充当 LLM，无需任何凭证）
- 契约文件缺失/非法 → 明确报错退出码 2（不静默）

只依赖标准库 ``unittest`` —— 与契约测试一致，确保零依赖可跑。
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
        return dict(_LLM_EXTRACT)

    return StubLLMProvider(respond)


class PipelineE2ETest(unittest.TestCase):
    # -- 运行辅助（注入 LLM 桩，零规则抽取必需）---------------------------
    def _run(self, sources):
        ctx = make_context(sources, llm=_stub())
        pipeline = build_pipeline(sources, llm=_stub())
        return pipeline.run(ctx=ctx)

    # -- 1. extract-only 顺序执行 -----------------------------------------
    def test_extract_only_pipeline_runs_3_steps(self):
        state, results = self._run([_SAMPLE])
        self.assertEqual(len(results), 3)
        self.assertEqual([r.step for r in results],
                         ["ingest", "extract", "build_kg"])
        self.assertTrue(state.raw, "raw 应为非空")
        self.assertTrue(state.extractions, "extractions 应为非空")
        self.assertIsNotNone(state.graph)

    # -- 2. 图谱被正确构建 -------------------------------------------------
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

    # -- 3. 幂等：同输入两次实体 ID 集合一致 -------------------------------
    def test_deterministic_entity_ids(self):
        _, r1 = self._run([_SAMPLE])
        _, r2 = self._run([_SAMPLE])
        g1 = r1[2].output  # build_kg 步（索引 2）
        g2 = r2[2].output
        self.assertEqual(
            set(g1.entities), set(g2.entities),
            "同输入应产出内容寻址一致的实体 ID",
        )

    # -- 4. CLI 子进程 -----------------------------------------------------
    def test_cli_subprocess_without_host_contract_runs_empty_extract(self):
        """零规则架构：宿主未交卷（无 --host-contract）时抽取为空，但管线完整跑通并提示。"""
        py = sys.executable
        proc = subprocess.run(
            [py, "-m", "smini.cli", "build", "--sample", "--quiet"],
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
                 "--quiet"],
                capture_output=True, text=True,
                cwd=str(Path(__file__).resolve().parent.parent),
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertIn("实体 /", proc.stdout, "应产出图谱统计")
            self.assertIn("ORGANIZATION", proc.stdout, "实体类型分布应进入输出")
        finally:
            import os
            os.unlink(cpath)

    def test_cli_subprocess_host_contract_bad_file(self):
        """契约文件缺失/非法 → 明确报错退出码 2（不静默）。"""
        py = sys.executable
        proc = subprocess.run(
            [py, "-m", "smini.cli", "build", "--sample", "--host-contract",
             "/nonexistent/contract.json", "--quiet"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr)
        self.assertIn("读取宿主契约失败", proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
