"""v9 借鉴增量（sharptoolbox v9 二次比对：dataScope / 规则四层 / approvalOutcomes）测试。

2026-09-20 三个借鉴点（与 v7 相同的契约即规则机制，不引入新通道）：

- permissions.data_scope：数据可见范围（ALL/OWN/DEPT/CUSTOM/OTHER），金融数据隔离；
  不参与 permission_id 寻址。
- processes.flow_type（COLLABORATION/APPROVAL）+ approval_chain（审批链路摘要）；
  steps.lane（泳道）/ approval_outcome（APPROVE/REJECT/RETURN/OTHER）/
  reject_to（驳回目标下标，越界 → None）；flows.on_reject（驳回边条件）。
  均不参与 process_id/step_id/flow_id 寻址。
- 规则四层分级判定（L1 属性内置 / L2 refRules / L3 invariants / L4 业务规则）：
  纯提示词约束，Python 侧无字段变更（不新增测试断言，仅保证既有规则通道不回归）。

核心机制不变：宿主契约交卷 → 薄壳枚举校验 + 非法兜底 → validate 可验证；
ID 锚点键不变（新字段不参与寻址）。
"""

from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smini import make_context  # noqa: E402
from smini.llm import StubLLMProvider  # noqa: E402
from smini.protocols import RunContext  # noqa: E402
from smini.steps.extract import ExtractStep  # noqa: E402
from smini.types import (  # noqa: E402
    ApprovalOutcome,
    DataScope,
    NormalizedDocument,
    ProcessFlowType,
    SourceRef,
    SourceType,
)

_SAMPLE = (
    "证券经营机构应当对投资者履行适当性管理义务。"
    "普通投资者申请转化成为专业投资者，应当经适当性管理岗初审、合规审核岗复核。"
    "审核不通过的退回修改后重新提交。"
)

_LLM_EXTRACT = {
    "entities": [
        {"surface": "证券经营机构", "type": "ORGANIZATION", "canonical": "证券经营机构"},
        {"surface": "普通投资者", "type": "CONCEPT", "canonical": "普通投资者"},
        {"surface": "专业投资者", "type": "CONCEPT", "canonical": "专业投资者"},
    ],
    "relations": [
        {"subject": "普通投资者", "predicate": "可转化为", "object": "专业投资者",
         "object_type": "CONCEPT", "evidence": "申请转化成为专业投资者"},
    ],
    "attributes": [],
    "rules": [],
    "processes": [
        {
            "name": "投资者类别转化审批流程",
            "description": "普通投资者转化专业投资者的审批流程",
            "flow_type": "APPROVAL",
            "approval_chain": ["适当性管理岗", "合规审核岗"],
            "steps": [
                {"label": "流程开始", "kind": "START", "actor": "", "evidence": "",
                 "lane": ""},
                {"label": "提交转化申请", "kind": "TASK", "actor": "普通投资者",
                 "evidence": "申请转化成为专业投资者", "lane": "投资者"},
                {"label": "适当性管理岗初审", "kind": "TASK", "actor": "证券经营机构",
                 "evidence": "审慎评估", "lane": "适当性管理岗",
                 "approval_outcome": "APPROVE", "reject_to": None},
                {"label": "合规审核岗复核", "kind": "TASK", "actor": "证券经营机构",
                 "evidence": "书面告知审查结果和理由", "lane": "合规审核岗",
                 "approval_outcome": "RETURN", "reject_to": 1},
                {"label": "流程结束", "kind": "END", "actor": "", "evidence": "",
                 "lane": ""},
            ],
            "flows": [
                {"from": 0, "to": 1, "type": "SEQUENCE", "condition": "", "evidence": ""},
                {"from": 1, "to": 2, "type": "SEQUENCE", "condition": "", "evidence": ""},
                {"from": 2, "to": 3, "type": "SEQUENCE", "condition": "初审通过",
                 "evidence": ""},
                {"from": 3, "to": 1, "type": "CONDITIONAL", "condition": "复核退回修改",
                 "on_reject": "退回重新提交申请", "evidence": "退回修改后重新提交"},
            ],
            "preconditions": [],
            "postconditions": [],
        },
        {
            "name": "普通协作流程",
            "description": "无审批环节的协作流程",
            "flow_type": "INVALID_XX",
            "steps": [
                {"label": "开始", "kind": "START", "actor": "", "evidence": ""},
                {"label": "填写问卷", "kind": "TASK", "actor": "投资者", "evidence": "",
                 "approval_outcome": "BAD_OUTCOME", "reject_to": 99},
                {"label": "结束", "kind": "END", "actor": "", "evidence": ""},
            ],
            "flows": [
                {"from": 0, "to": 1, "type": "SEQUENCE", "condition": "", "evidence": ""},
                {"from": 1, "to": 2, "type": "SEQUENCE", "condition": "", "evidence": ""},
            ],
        },
    ],
    "permissions": [
        {"actor": "普通投资者", "action": "申请转化", "effect": "PERMIT",
         "scope": "符合规定", "role": "", "actor_type": "HUMAN",
         "data_scope": "OWN", "evidence": "有权申请"},
        {"actor": "专业投资者", "action": "购买产品", "effect": "PERMIT",
         "scope": "全市场", "role": "", "actor_type": "HUMAN",
         "data_scope": "ALL", "evidence": "可以购买"},
        {"actor": "客户经理", "action": "查看客户资料", "effect": "PERMIT",
         "scope": "", "role": "", "actor_type": "HUMAN",
         "data_scope": "NOPE", "evidence": ""},
    ],
    "document_meta": {"domain": "金融/适当性", "version": "V1", "doc_type": "监管指引"},
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/v9-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


def _run():
    return ExtractStep().transform(
        [_nd(_SAMPLE)], RunContext(llm=_make_stub(_SAMPLE, _LLM_EXTRACT)))[0]


class V9DataScopeTest(unittest.TestCase):
    """permissions.data_scope：透传、非法兜底、不参与寻址。"""

    def test_passthrough_and_fallback(self):
        res = _run()
        self.assertEqual(len(res.permissions), 3)
        p0, p1, p2 = res.permissions
        self.assertEqual(p0.data_scope, DataScope.OWN)
        self.assertEqual(p1.data_scope, DataScope.ALL)
        self.assertEqual(p2.data_scope, DataScope.OTHER)   # 非法 → OTHER

    def test_permission_id_not_affected_by_data_scope(self):
        a = dict(_LLM_EXTRACT)
        b = json.loads(json.dumps(_LLM_EXTRACT))
        b["permissions"][0]["data_scope"] = "ALL"   # 改新字段
        ra = _run() if False else ExtractStep().transform(
            [_nd(_SAMPLE)], RunContext(llm=_make_stub(_SAMPLE, a)))[0]
        rb = ExtractStep().transform(
            [_nd(_SAMPLE)], RunContext(llm=_make_stub(_SAMPLE, b)))[0]
        self.assertEqual(ra.permissions[0].permission_id,
                         rb.permissions[0].permission_id)


class V9ProcessApprovalTest(unittest.TestCase):
    """processes 审批流增强：flow_type / approval_chain / lane /
    approval_outcome / reject_to / on_reject。"""

    def test_flow_type_and_approval_chain(self):
        res = _run()
        self.assertEqual(len(res.processes), 2)
        p0, p1 = res.processes
        self.assertEqual(p0.flow_type, ProcessFlowType.APPROVAL)
        self.assertEqual(p0.approval_chain, ["适当性管理岗", "合规审核岗"])
        # 非法 flow_type → COLLABORATION 兜底
        self.assertEqual(p1.flow_type, ProcessFlowType.COLLABORATION)

    def test_step_approval_fields(self):
        res = _run()
        p0 = res.processes[0]
        self.assertEqual(p0.steps[2].lane, "适当性管理岗")
        self.assertEqual(p0.steps[2].approval_outcome, ApprovalOutcome.APPROVE)
        self.assertIsNone(p0.steps[2].reject_to)
        self.assertEqual(p0.steps[3].approval_outcome, ApprovalOutcome.RETURN)
        self.assertEqual(p0.steps[3].reject_to, 1)

    def test_invalid_approval_outcome_and_reject_to_out_of_range(self):
        res = _run()
        p1 = res.processes[1]
        self.assertEqual(p1.steps[1].approval_outcome, ApprovalOutcome.OTHER)
        self.assertIsNone(p1.steps[1].reject_to)   # 越界 99 → None

    def test_on_reject_passthrough(self):
        res = _run()
        flows = res.processes[0].flows
        rj = [f for f in flows if f.on_reject]
        self.assertEqual(len(rj), 1)
        self.assertEqual(rj[0].on_reject, "退回重新提交申请")


class V9ValidateSchemaTest(unittest.TestCase):
    """新字段过 schema：validate extract 通过（含非法兜底后的值）。"""

    def test_extraction_validates_with_approval_fields(self):
        import smini.runtime as R
        from smini.types import to_dict
        res = _run()
        p = Path(__file__).parent / "_tmp_v9_extract.json"
        p.write_text(json.dumps(to_dict(res), ensure_ascii=False), encoding="utf-8")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = R.cmd_validate("extraction", str(p))
            self.assertEqual(rc, 0, buf.getvalue())
        finally:
            p.unlink(missing_ok=True)


class V9RendererTest(unittest.TestCase):
    """渲染器：data_scope / 审批流字段渲染。"""

    def test_renderer_includes_v9_columns(self):
        from smini.types import to_dict
        res = _run()
        data = to_dict(res)
        inp = Path(__file__).parent / "_tmp_v9_contract.json"
        out = inp.with_suffix(".html")
        inp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            script = Path(__file__).resolve().parent.parent / \
                "skills/smini-extract/scripts/render_viewer.py"
            proc = subprocess.run(
                [sys.executable, str(script), "--in", str(inp), "--out", str(out)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = out.read_text(encoding="utf-8")
            self.assertIn("数据范围", html)          # 权限表头新列
            self.assertIn("data_scope_color", html)  # 配色注入
            self.assertIn("审批结果", html)          # 步骤表新列
            self.assertIn("驳回至", html)
            self.assertIn("驳回条件", html)          # 控制流表新列
            self.assertIn("proc_flow_type_color", html)
            self.assertIn("approval_outcome_color", html)
            self.assertIn("APPROVAL", html)          # 审批流徽标
        finally:
            inp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
