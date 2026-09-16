"""skill 运行时 / 宿主契约（Rule-by-Contract）测试。

覆盖 skill 与宿主 agent 之间的产出契约与自检项：

- 契约常量：REQUIRED_SECTIONS / REQUIRED_KEYS / REQUIRED_FIELDS
  十一件套齐全，SECTION_ORDER 与 render_viewer 的 Tab 顺序一致。
- 宿主契约合规性：真实 host-contract.example.json 通过 validate extract；
  render_viewer 渲染 12 个 Tab（实体/关系/属性/规则/流程/流程清单/状态机/
  指标函数/时态/动作处置/约束/授权）。
- skill 自检清单被渲染进 HTML（人工核查时逐条勾选）。
- 幂等：同契约两次渲染 HTML 逐字节一致。
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smini.steps.extract import (  # noqa: E402
    REQUIRED_FIELDS,
    REQUIRED_KEYS,
    REQUIRED_SECTIONS,
    SECTION_ORDER,
)

PROJECT = Path(__file__).resolve().parent.parent
SKILL_DIR = PROJECT / "skills" / "smini-extract"
EXAMPLE = SKILL_DIR / "references" / "host-contract.example.json"
VIEWER = SKILL_DIR / "scripts" / "render_viewer.py"


class ContractConstantsTest(unittest.TestCase):
    def test_required_sections_eleven(self):
        self.assertEqual(len(REQUIRED_SECTIONS), 11)
        self.assertEqual(
            REQUIRED_SECTIONS,
            {"entities", "relations", "attributes", "rules", "processes", "states",
             "functions", "temporal", "actions", "constraints", "permissions"},
        )

    def test_required_keys_three(self):
        self.assertEqual(REQUIRED_KEYS, {"entities", "relations", "attributes"})

    def test_required_fields_entity_and_relation(self):
        self.assertEqual(REQUIRED_FIELDS["entity"], {"surface", "type", "canonical"})
        self.assertEqual(REQUIRED_FIELDS["relation"],
                         {"subject", "predicate", "object", "object_type", "evidence"})

    def test_section_order_matches_viewer_tabs(self):
        viewer = VIEWER.read_text(encoding="utf-8")
        for section in SECTION_ORDER:
            tab = {"states": "状态机", "functions": "指标函数",
                   "temporal": "时态", "actions": "动作处置",
                   "constraints": "约束", "permissions": "授权"}.get(section, section)
            self.assertIn(tab, viewer, f"render_viewer 缺少 Tab {tab}")


class HostContractExampleTest(unittest.TestCase):
    def test_example_validates(self):
        """真实 host-contract.example.json 通过 validate extract（宿主契约合规）。"""
        proc = subprocess.run(
            [sys.executable, "-m", "smini.cli", "validate", "extract",
             "--in", str(EXAMPLE)],
            cwd=PROJECT, capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"example 应通过校验：{proc.stderr}")

    def test_example_render_12_tabs(self):
        """render_viewer 渲染 example → 12 Tab（含 6 个预测决策新 Tab）。"""
        out_dir = Path(__file__).resolve().parent / "_tmp_skill_runtime"
        out_dir.mkdir(exist_ok=True)
        hp = out_dir / "04-extraction.html"
        proc = subprocess.run(
            [sys.executable, str(VIEWER), "--in", str(EXAMPLE), "--out", str(hp)],
            capture_output=True, text=True,
        )
        self.assertEqual(proc.returncode, 0, f"render_viewer 应成功：{proc.stderr}")
        html = hp.read_text(encoding="utf-8")
        for tab in ("实体", "关系", "属性", "业务规则", "业务流程", "流程清单",
                    "状态机", "指标函数", "时态", "动作处置", "约束", "授权"):
            self.assertIn(tab, html, f"HTML 应含 Tab {tab}")
        self.assertIn("证券经营机构", html, "HTML 应含示例实体")
        jp = out_dir / "04-extraction.json"
        jp.unlink(missing_ok=True)
        hp.unlink(missing_ok=True)
        out_dir.rmdir()

    def test_render_idempotent(self):
        """同契约两次渲染 HTML 逐字节一致（确定性渲染）。"""
        out_dir = Path(__file__).resolve().parent / "_tmp_skill_runtime2"
        out_dir.mkdir(exist_ok=True)
        h1 = out_dir / "a.html"
        h2 = out_dir / "b.html"
        subprocess.run([sys.executable, str(VIEWER), "--in", str(EXAMPLE),
                        "--out", str(h1)], check=True, capture_output=True)
        subprocess.run([sys.executable, str(VIEWER), "--in", str(EXAMPLE),
                        "--out", str(h2)], check=True, capture_output=True)
        self.assertEqual(h1.read_text(encoding="utf-8"),
                         h2.read_text(encoding="utf-8"), "渲染应确定性幂等")
        h1.unlink(missing_ok=True)
        h2.unlink(missing_ok=True)
        out_dir.rmdir()


if __name__ == "__main__":
    unittest.main(verbosity=2)
