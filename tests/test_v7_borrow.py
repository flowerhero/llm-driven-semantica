"""v7 借鉴增量（借鉴 ontology-driven-dev 七模型，保持契约即规则机制）测试。

A 组字段级增强：
- rules：rule_type（用途，与 modality 正交）/ output_type（复用函数输出枚举）/
  reused_by（引用名数组）/ certainty（行业通用-企业专属分级）。
- processes：preconditions/postconditions（流程级前后置）；steps 增
  sub_process_ref（子流程引用）；StepKind 增 START/END/SYSTEM_TASK。
- actions：precondition/postcondition。
- permissions：role（角色中间层）+ actor_type（HUMAN/SYSTEM）。
- attributes：required（必录性）+ value_type 增 DICT_REF/ENTITY_REF。

B 组结构增强：
- 输出顶层 document_meta {domain, source, version, doc_type}（source 强制取
  输入文档 URI）。
- cmd_validate 软引用检查：reused_by/sub_process_ref/actor/role 找不到引用
  只出 warning、不阻断（借鉴对方一致性门禁但降级为软检查）。

核心机制不变：新字段全部由宿主契约交卷 → 薄壳枚举校验 + 非法兜底 →
evidence 溯源 → validate 可验证；ID 锚点键不变（rule_id 仍由
subject+condition+action+modality 内容寻址，新字段不参与寻址）。
"""

from __future__ import annotations

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
    ActorType,
    AttributeValueType,
    Certainty,
    FunctionOutputType,
    NormalizedDocument,
    RuleType,
    SourceRef,
    SourceType,
    StepKind,
)

_SAMPLE = (
    "证券经营机构应当对投资者履行适当性管理义务。"
    "当投资者风险承受能力等级发生变化时，证券经营机构应当书面风险警示并重新匹配产品或服务。"
    "机构应当按照规定流程开展回访，回访完成后系统自动归档。"
)

_LLM_EXTRACT = {
    "entities": [
        {"surface": "证券经营机构", "type": "ORGANIZATION", "canonical": "证券经营机构"},
        {"surface": "投资者", "type": "CONCEPT", "canonical": "投资者"},
    ],
    "relations": [],
    "attributes": [
        {"entity": "证券经营机构", "name": "注册资本", "value": "10亿元",
         "value_type": "MONEY", "required": True, "evidence": "注册资本"},
        {"entity": "投资者", "name": "风险等级", "value": "高风险",
         "value_type": "ENUM", "required": "是", "evidence": "风险承受能力等级"},
        {"entity": "证券经营机构", "name": "所属市场", "value": "上交所",
         "value_type": "ENTITY_REF", "required": False, "evidence": ""},
        {"entity": "投资者", "name": "报案方式", "value": "下拉选择",
         "value_type": "DICT_REF", "evidence": ""},
    ],
    "rules": [
        {"subject": "证券经营机构", "condition": "投资者风险承受能力等级发生变化",
         "action": "书面风险警示并重新匹配产品或服务", "modality": "OBLIGATION",
         "rule_type": "TRIGGER", "output_type": "STRING",
         "reused_by": ["适当性管理流程"], "certainty": "GENERAL",
         "evidence": "应当书面风险警示并重新匹配产品或服务"},
        {"subject": "证券经营机构", "condition": "",
         "action": "每年开展一次评估", "modality": "OBLIGATION",
         "rule_type": "INVALID_XX", "output_type": "NOPE",
         "reused_by": "字符串引用", "certainty": "ENTERPRISE",
         "evidence": "应当每年"},
    ],
    "processes": [
        {
            "name": "适当性管理流程",
            "description": "评估-匹配-回访",
            "preconditions": ["已完成投资者风险承受能力评估"],
            "postconditions": ["匹配结果已留档"],
            "steps": [
                {"label": "开始", "kind": "START", "actor": "", "evidence": "按规定流程"},
                {"label": "评估风险承受能力", "kind": "TASK", "actor": "证券经营机构",
                 "evidence": ""},
                {"label": "匹配适当产品", "kind": "GATEWAY", "actor": "",
                 "sub_process_ref": "产品匹配子流程", "evidence": ""},
                {"label": "结束", "kind": "END", "actor": "", "evidence": ""},
            ],
            "flows": [
                {"from": 0, "to": 1, "type": "SEQUENCE", "condition": "", "evidence": ""},
                {"from": 2, "to": 3, "type": "CONDITIONAL", "condition": "匹配成功",
                 "evidence": ""},
            ],
        }
    ],
    "actions": [
        {"name": "书面风险警示", "actor": "证券经营机构", "level": "WARN",
         "target": "投资者", "side_effect": "", "trigger": "等级发生变化",
         "precondition": "已识别等级变化", "postcondition": "投资者已收到警示",
         "evidence": "应当书面风险警示"},
    ],
    "permissions": [
        {"actor": "客户经理", "action": "发起风险评估", "effect": "PERMIT",
         "scope": "权限范围内", "role": "适当性管理岗", "actor_type": "HUMAN",
         "evidence": "有权"},
        {"actor": "收付费系统", "action": "自动归档", "effect": "PERMIT",
         "scope": "", "role": "", "actor_type": "SYSTEM",
         "evidence": "系统自动归档"},
    ],
    "document_meta": {"domain": "金融/适当性", "version": "V1", "doc_type": "监管指引"},
}


def _make_stub(sample_text: str, extract: dict) -> StubLLMProvider:
    def respond(prompt: str, schema):
        return dict(extract)
    return StubLLMProvider(respond)


def _nd(sample_text: str) -> NormalizedDocument:
    src = SourceRef.of("memory://inline/v7-test", SourceType.TEXT, checksum="x")
    return NormalizedDocument(doc_id="d1", source=src, text=sample_text)


class V7RulesTest(unittest.TestCase):
    """A 组 · rules 新字段：透传、非法兜底、ID 寻址不变。"""

    def test_rule_fields_passthrough_and_fallback(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(len(res.rules), 2)

        r0 = res.rules[0]
        self.assertEqual(r0.rule_type, RuleType.TRIGGER)
        self.assertEqual(r0.output_type, "STRING")
        self.assertEqual(r0.reused_by, ["适当性管理流程"])
        self.assertEqual(r0.certainty, Certainty.GENERAL)

        # 非法 rule_type/certainty/output_type → OTHER / OTHER / OTHER 兜底
        r1 = res.rules[1]
        self.assertEqual(r1.rule_type, RuleType.OTHER)
        self.assertEqual(r1.output_type, FunctionOutputType.OTHER.value)
        self.assertEqual(r1.certainty, Certainty.ENTERPRISE)
        # reused_by 单字符串 → 清洗成单元素数组
        self.assertEqual(r1.reused_by, ["字符串引用"])

    def test_rule_id_not_affected_by_new_fields(self):
        """ID 锚点键不变：新字段不影响 rule_id 内容寻址。"""
        a = dict(_LLM_EXTRACT)
        b = json.loads(json.dumps(_LLM_EXTRACT))
        b["rules"][0]["certainty"] = "ENTERPRISE"   # 改新字段
        b["rules"][0]["rule_type"] = "VALIDATION"
        ra = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=_make_stub(_SAMPLE, a)))[0]
        rb = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=_make_stub(_SAMPLE, b)))[0]
        self.assertEqual(ra.rules[0].rule_id, rb.rules[0].rule_id)


class V7ProcessTest(unittest.TestCase):
    """A 组 · processes：前后置条件、子流程引用、StepKind 扩展。"""

    def test_process_fields_passthrough(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(len(res.processes), 1)
        p = res.processes[0]
        self.assertEqual(p.preconditions, ["已完成投资者风险承受能力评估"])
        self.assertEqual(p.postconditions, ["匹配结果已留档"])

        kinds = [s.kind for s in p.steps]
        self.assertEqual(kinds, [StepKind.START, StepKind.TASK,
                                 StepKind.GATEWAY, StepKind.END])
        self.assertEqual(p.steps[2].sub_process_ref, "产品匹配子流程")


class V7ActionPermissionTest(unittest.TestCase):
    """A 组 · actions/permissions：前后置条件、角色与主体类型。"""

    def test_action_pre_post_conditions(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        ac = res.actions[0]
        self.assertEqual(ac.precondition, "已识别等级变化")
        self.assertEqual(ac.postcondition, "投资者已收到警示")

    def test_permission_role_and_actor_type(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(len(res.permissions), 2)
        p0, p1 = res.permissions
        self.assertEqual(p0.role, "适当性管理岗")
        self.assertEqual(p0.actor_type, ActorType.HUMAN)
        self.assertEqual(p1.actor_type, ActorType.SYSTEM)


class V7AttributeTest(unittest.TestCase):
    """A 组 · attributes：required 必录性、DICT_REF/ENTITY_REF 值类型。"""

    def test_required_and_ref_value_types(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        by_name = {t.predicate: t for t in res.triplets if t.value_type is not None}
        self.assertTrue(by_name["注册资本"].required)          # bool True
        self.assertTrue(by_name["风险等级"].required)           # 字符串 "是"
        self.assertFalse(by_name["所属市场"].required)         # False
        self.assertFalse(by_name["报案方式"].required)         # 缺省 → False
        self.assertEqual(by_name["所属市场"].value_type, AttributeValueType.ENTITY_REF)
        self.assertEqual(by_name["报案方式"].value_type, AttributeValueType.DICT_REF)


class V7DocumentMetaTest(unittest.TestCase):
    """B 组 · document_meta：source 强制取输入 URI，其余透传。"""

    def test_document_meta(self):
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ExtractStep().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        self.assertEqual(res.document_meta["source"], "memory://inline/v7-test")
        self.assertEqual(res.document_meta["domain"], "金融/适当性")
        self.assertEqual(res.document_meta["version"], "V1")
        self.assertEqual(res.document_meta["doc_type"], "监管指引")


class V7ValidateSoftRefTest(unittest.TestCase):
    """B 组 · validate：新字段过 schema；软引用找不到只 warning 不阻断。"""

    def _run_extract(self):
        from smini.steps.extract import ExtractStep as ES
        stub = _make_stub(_SAMPLE, _LLM_EXTRACT)
        res = ES().transform([_nd(_SAMPLE)], RunContext(llm=stub))[0]
        return res

    def test_extraction_json_validates_with_warnings(self):
        import io
        import contextlib
        import smini.runtime as R
        from smini.types import to_dict
        res = self._run_extract()
        p = Path(__file__).parent / "_tmp_v7_extract.json"
        p.write_text(json.dumps(to_dict(res), ensure_ascii=False), encoding="utf-8")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stderr(buf):
                rc = R.cmd_validate("extraction", str(p))
            self.assertEqual(rc, 0, buf.getvalue())
            # 软警告应出现（子流程引用/角色引用在契约内未定义实体）
            self.assertIn("软警告", buf.getvalue())
        finally:
            p.unlink(missing_ok=True)


class V7RendererTest(unittest.TestCase):
    """渲染器：v7 新列与 document_meta 头正常渲染。"""

    def test_renderer_includes_v7_columns(self):
        from smini.types import to_dict
        res = ExtractStep().transform(
            [_nd(_SAMPLE)], RunContext(llm=_make_stub(_SAMPLE, _LLM_EXTRACT)))[0]
        data = to_dict(res)
        inp = Path(__file__).parent / "_tmp_v7_contract.json"
        out = inp.with_suffix(".html")
        inp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        try:
            # 渲染脚本直接调用
            script = Path(__file__).resolve().parent.parent / \
                "skills/smini-extract/scripts/render_viewer.py"
            proc = subprocess.run(
                [sys.executable, str(script), "--in", str(inp), "--out", str(out)],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            html = out.read_text(encoding="utf-8")
            self.assertIn("用途类型", html)
            self.assertIn("rule_type_color", html)
            self.assertIn("document_meta", html)
            self.assertIn("领域：金融/适当性", html)
        finally:
            inp.unlink(missing_ok=True)
            out.unlink(missing_ok=True)


def FunctionOutputTypeValue(name: str) -> str:
    from smini.types import FunctionOutputType
    return FunctionOutputType(name).value


if __name__ == "__main__":
    unittest.main()
