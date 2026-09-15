"""smini.llm — 让大模型承担「智能」的接缝实现。

这一层把「抽取 / 解析」从硬代码规则挪进 LLM，但**只让 LLM 提议事实**，
确定性骨架（span 定位、entity_id/edge_id 的 sha256 哈希、双时态、幂等）
仍全部留在 Python 的 step 里。这样重跑同批数据 + 温度=0 → 图谱严格不变。

模型能力的来源（方式 3 · 宿主即 LLM）：
  * ``HostAgentLLMProvider`` —— 生产路径。**宿主 agent（豆包）充当 LLM**：
    宿主读文档后按契约产出 ``{entities, relations}``（或 ``{text, blocks}``），
    通过 ``inject()`` 注入本 provider，Python 薄壳只做确定性映射。
    本实现**不再读取任何 ``SMINI_LLM_*`` 环境变量**，也**不发起任何模型
    HTTP 调用**——模型能力完全来自运行环境的宿主，无需外部 API 凭证。
  * ``StubLLMProvider`` —— 测试桩，直接返回预置 dict，用于契约/端到端测试
    证明「LLM 提议 → Python 归一」这条链路正确且幂等。

agent 编排时：宿主按 SKILL.md 契约产出 dict → ``HostAgentLLMProvider.inject()``
（或 CLI ``--host-contract <file>``）→ step 内部经 ``LLMProvider.extract`` 取到
dict 后走薄壳映射。``LLMProvider.extract`` 仍是唯一接缝。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Mapping

from .protocols import LLMProvider
from .types import (
    ActionLevel,
    AttributeValueType,
    ConstraintType,
    DependencyMissing,
    EntityType,
    FlowType,
    FunctionOutputType,
    PermissionEffect,
    RelationPropKind,
    RuleModality,
    StepKind,
    TemporalKind,
)

# ---------------------------------------------------------------------------
# 抽取 / 解析的 JSON Schema（作为指令，不强制校验）
# ---------------------------------------------------------------------------

_EXTRACT_TYPES = [t.value for t in EntityType]
_ATTR_VALUE_TYPES = [t.value for t in AttributeValueType]
_RULE_MODALITIES = [t.value for t in RuleModality]
_STEP_KINDS = [t.value for t in StepKind]
_FLOW_TYPES = [t.value for t in FlowType]
_FUNC_OUTPUT_TYPES = [t.value for t in FunctionOutputType]
_TEMPORAL_KINDS = [t.value for t in TemporalKind]
_ACTION_LEVELS = [t.value for t in ActionLevel]
_CONSTRAINT_TYPES = [t.value for t in ConstraintType]
_PERMISSION_EFFECTS = [t.value for t in PermissionEffect]
_PROP_KINDS = [t.value for t in RelationPropKind]

EXTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "entities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "surface": {"type": "string"},
                    "type": {"type": "string", "enum": _EXTRACT_TYPES},
                    "canonical": {"type": "string"},
                },
                "required": ["surface", "type"],
            },
        },
        "relations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "object_type": {"type": ["string", "null"]},
                    "evidence": {"type": "string"},
                    "prop_kind": {"type": ["string", "null"], "enum": _PROP_KINDS,
                                  "description": "v6 关系传导类型（可选）："
                                                 "FACTUAL 普通事实 / DEPENDENCY 依赖 / "
                                                 "CAUSAL 因果 / TRIGGER 触发；省略即普通"},
                    "strength": {"type": ["string", "null"],
                                 "description": "v6 关系强度（可选）：HIGH/MEDIUM/LOW "
                                                "或原文程度词；省略即未标注"},
                },
                "required": ["subject", "predicate", "object"],
            },
        },
        "attributes": {
            "type": "array",
            "description": "实体属性（本体论 DatatypeProperty）：宾语是字面量时用此通道。"
                           "entity 须与 entities[].canonical 一致；name 是归一化属性名；"
                           "value 逐字取自原文；value_type 是属性值类型独立枚举。",
            "items": {
                "type": "object",
                "properties": {
                    "entity": {"type": "string"},
                    "name": {"type": "string"},
                    "value": {"type": "string"},
                    "value_type": {"type": "string", "enum": _ATTR_VALUE_TYPES},
                    "evidence": {"type": "string"},
                },
                "required": ["entity", "name", "value"],
            },
        },
        "rules": {
            "type": "array",
            "description": "业务规则（规范性/条件性知识）：含道义动词或条件-动作结构。"
                           "subject 主体（可空）；condition 触发条件（可空）；"
                           "action 动作/结论（必有）；modality 是规则模态独立枚举；"
                           "evidence 逐字取自原文。",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "condition": {"type": "string"},
                    "action": {"type": "string"},
                    "modality": {"type": "string", "enum": _RULE_MODALITIES},
                    "evidence": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        "processes": {
            "type": "array",
            "description": "业务流程（有序活动 + 控制流）：文本含顺序/阶段/流程性描述时"
                           "抽取。name 流程名；steps 步骤（label 做什么/kind 角色/actor 谁做"
                           "/evidence）；flows 控制流（from/to 整数下标引用 steps、type 独立"
                           "枚举、condition 仅分支用）。",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "kind": {"type": "string", "enum": _STEP_KINDS},
                                "actor": {"type": "string"},
                                "evidence": {"type": "string"},
                            },
                            "required": ["label"],
                        },
                    },
                    "flows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "integer"},
                                "to": {"type": "integer"},
                                "type": {"type": "string", "enum": _FLOW_TYPES},
                                "condition": {"type": "string"},
                                "evidence": {"type": "string"},
                            },
                            "required": ["from", "to", "type"],
                        },
                    },
                },
                "required": ["name", "steps"],
            },
        },
        "states": {
            "type": "array",
            "description": "状态机（v6）：对象生命周期状态迁移，预测引擎的骨架。"
                           "object 状态机归属对象（用实体 canonical）；name 状态机名；"
                           "states 状态列表（label 状态名/initial 是否初始，可空）；"
                           "transitions 迁移列表（from/to 整数下标引用 states、"
                           "event 触发事件/condition 守卫条件/action 迁移动作均可空）。",
            "items": {
                "type": "object",
                "properties": {
                    "object": {"type": "string"},
                    "name": {"type": "string"},
                    "states": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "initial": {"type": "boolean"},
                                "evidence": {"type": "string"},
                            },
                            "required": ["label"],
                        },
                    },
                    "transitions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "from": {"type": "integer"},
                                "to": {"type": "integer"},
                                "event": {"type": "string"},
                                "condition": {"type": "string"},
                                "action": {"type": "string"},
                                "evidence": {"type": "string"},
                            },
                            "required": ["from", "to"],
                        },
                    },
                },
                "required": ["object", "name", "states"],
            },
        },
        "functions": {
            "type": "array",
            "description": "函数指标（v6）：把业务经验写成可计算定义，是推演的输入。"
                           "name 指标名；subject 作用主体（用实体 canonical，可空）；"
                           "formula 计算定义（自然语言/运算符描述，不必可执行）；"
                           "inputs 依赖因子（实体 canonical 或字面量名）；"
                           "output_type 输出类型独立枚举。",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "subject": {"type": "string"},
                    "formula": {"type": "string"},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "output_type": {"type": "string", "enum": _FUNC_OUTPUT_TYPES},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "formula"],
            },
        },
        "temporal": {
            "type": "array",
            "description": "时序时态（v6）：把时间限定绑定到某事实/规则/流程/状态。"
                           "subject 主体（事实/规则/流程名或实体 canonical）；"
                           "kind 时态类型独立枚举；value 时间描述（逐字取自原文）；"
                           "anchor 时间锚点（可空）。",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "kind": {"type": "string", "enum": _TEMPORAL_KINDS},
                    "value": {"type": "string"},
                    "anchor": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["subject", "kind", "value"],
            },
        },
        "actions": {
            "type": "array",
            "description": "动作处置（v6）：处置动作元信息，供决策执行。"
                           "name 动作名；actor 执行者（用实体 canonical，可空）；"
                           "level 处置级别独立枚举；target 作用对象（可空）；"
                           "side_effect 副作用/代价（可空）；trigger 触发情形（可空）。",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "actor": {"type": "string"},
                    "level": {"type": "string", "enum": _ACTION_LEVELS},
                    "target": {"type": "string"},
                    "side_effect": {"type": "string"},
                    "trigger": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["name", "level"],
            },
        },
        "constraints": {
            "type": "array",
            "description": "结构约束（v6）：结构合法性约束（SHACL 式），保证预测与"
                           "决策不基于非法状态。subject 约束对象（实体 canonical）；"
                           "type 约束类型独立枚举；description 约束内容描述。",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "type": {"type": "string", "enum": _CONSTRAINT_TYPES},
                    "description": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["subject", "type", "description"],
            },
        },
        "permissions": {
            "type": "array",
            "description": "主体-动作授权（v6）：RBAC 授权边界，决策自动化治理层。"
                           "actor 主体（实体 canonical）；action 动作（对应 actions 名）；"
                           "effect 授权效果独立枚举；scope 授权范围（可空）。",
            "items": {
                "type": "object",
                "properties": {
                    "actor": {"type": "string"},
                    "action": {"type": "string"},
                    "effect": {"type": "string", "enum": _PERMISSION_EFFECTS},
                    "scope": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["actor", "action", "effect"],
            },
        },
    },
    "required": ["entities", "relations"],
}

_PARSE_KINDS = [
    "paragraph", "heading", "table", "code", "list_item", "quote",
    "caption", "image_ref", "footnote", "metadata",
]

PARSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "blocks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": _PARSE_KINDS},
                    "text": {"type": "string"},
                    "level": {"type": "integer"},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "required": ["kind", "text"],
            },
        },
    },
    "required": ["text", "blocks"],
}


def build_extract_prompt(text: str, doc_hint: str = "") -> str:
    """构造抽取 user prompt。system 指令由 provider 注入。

    契约即规则：LLM 全权负责语义，但必须按约定格式交卷——
      * entities：surface（原文表面形式）+ canonical（规范名，唯一身份锚，
        同一实体跨文档必须用同一 canonical，如「特斯拉」「Tesla」都归「特斯拉」）
        + type（19 类枚举之一——12 通用 + 7 金融扩展，判不出用 OTHER）。
      * relations：subject/predicate/object 用实体的 surface 或 canonical；
        object_type 从 19 类枚举选——宾语是日期/金额/百分比/数量时必须是
        DATE/MONEY/PERCENT/QUANTITY（字面量由类型派生，无需你判 literal）；
        宾语是另一个实体时给该实体类型；给不出就省略。evidence 是支撑这条
        关系的**原文短引用**（用于溯源，必须逐字取自原文）。
      * attributes（属性抽取 v2）：宾语是**字面量**时进这个通道。
        entity 用实体的 canonical（须与 entities 一致）；name 是归一化属性名
        （如「注册资本」「风险等级」）；value 逐字取自原文；value_type 是
        属性值类型独立枚举。**唯一硬分界**：宾语是独立实体 → relations；
        宾语是字面量（不是另一个实体）→ attributes。
    注意：不给字符偏移、不给 ID、不给 object_literal——这些由消费层确定。
    """
    hint = f"（文档类型提示：{doc_hint}）" if doc_hint else ""
    return (
        "请从下面的文档中抽取知识图谱要素。\n"
        f"{hint}\n"
        "要求：\n"
        "1. entities：命名实体，给出原文 surface、类型 type（从 "
        f"{'/'.join(_EXTRACT_TYPES)} 中选，判不出用 OTHER）、归一化 canonical 名。"
        "**canonical 是实体唯一身份**：同一实体在不同地方出现必须给同一 canonical"
        "（如「特斯拉」「Tesla」都归「特斯拉」），不得每处自由发挥。\n"
        "2. relations：实体间**类型化关系**，subject/object 用实体的 surface 或 canonical"
        "（须与 entities 中某条一致）。object_type 从枚举中选：宾语若是日期/金额/百分比"
        "/数量（如 2003年、967亿美元、30%）必须标 DATE/MONEY/PERCENT/QUANTITY；"
        "宾语是另一个实体就标它的类型；给不出可省略。evidence 给支撑本关系的"
        "**原文短引用**（逐字取自原文，用于溯源）。可选：prop_kind 标注关系传导类型（从 "
        f"{'/'.join(_PROP_KINDS)} 中选：FACTUAL 普通事实/DEPENDENCY 依赖/CAUSAL 因果/"
        "TRIGGER 触发，省略即普通）；strength 标注影响强度（HIGH/MEDIUM/LOW 或原文程度词"
        "如「重大」「显著」，省略即未标注）。\n"
        f"3. attributes：**实体属性**（本体论数据属性）。**唯一硬分界**：宾语是独立实体"
        "→ 放 relations；宾语是字面量（日期、金额、数字、百分比、程度词、真/假等，"
        "不是一个实体）→ 放 attributes。每条给 entity（用实体的 canonical，须与 "
        "entities 一致）、name（归一化属性名，如「成立年份」「注册资本」「风险等级」）、"
        "value（**逐字取自原文**）、value_type（从 "
        f"{'/'.join(_ATTR_VALUE_TYPES)} 中选，判不出用 OTHER）、evidence（原文短引用）。\n"
        "4. rules：**业务规则**（规范性/条件性知识）。判定线索：含**道义动词**（应当/"
        "必须/应/须/不得/禁止/严禁/可以/有权/允许）或**条件-动作结构**（如果/当…时…"
        "则…）的句子 → 抽为规则；纯事实陈述（宾语是实体 → relations；宾语是字面量 → "
        "attributes）**不**进 rules。每条给 subject（规则主体，用实体的 canonical，可空）、"
        "condition（触发条件/前件 IF，可空）、action（动作/结论/后件 THEN，**必有**）、"
        "modality（从 "
        f"{'/'.join(_RULE_MODALITIES)} 中选：应当/必须/应/须 → OBLIGATION，不得/禁止/"
        "严禁 → PROHIBITION，可以/有权/允许 → PERMISSION，无模态词的 IF-THEN 推导 → "
        "CONDITIONAL，判不出用 OTHER）、evidence（原文短引用）。**不抽**定义、标题、"
        "序言、交叉引用表等非规则段落（防规则集膨胀）。\n"
        "5. processes：**业务流程**（有序活动 + 控制流）。判定线索：文本含**顺序/阶段/"
        "流程性描述**（顺序词、阶段词、「流程」「步骤」「先…再…最后」、生命周期）→ "
        "抽为流程。每条给 name（流程名）、steps（按执行顺序，label 步骤内容、kind 从 "
        f"{'/'.join(_STEP_KINDS)} 中选：TASK 活动/GATEWAY 决策点/EVENT 事件，判不出用 "
        "TASK、actor 执行者用实体的 canonical 可空、evidence 原文短引用）、flows（控制流，"
        "from/to 用**整数下标**引用 steps 数组（0-based）、type 从 "
        f"{'/'.join(_FLOW_TYPES)} 中选：先…再…最后 → SEQUENCE，同时/同步 → PARALLEL，"
        "如果…则…否则 → CONDITIONAL（此时给 condition 分支条件），重复/直到 → LOOP，"
        "判不出用 SEQUENCE、condition 仅 CONDITIONAL 用、evidence 原文短引用）。"
        "纯事实（relations/attributes）与纯规范（rules）不因流程抽取而重复——"
        "五件套并行不互斥，同一句可同时是关系与流程步骤。\n"
        "6. states：**对象状态机**（预测决策 v6）。判定线索：文本描述对象/业务的"
        "**状态演化**（如「风险承受能力等级分为五级：低风险→中低风险→中风险→中高"
        "风险→高风险」「普通投资者满足条件可转为专业投资者」）。每条给 object（状态机"
        "归属对象，用实体的 canonical）、name（状态机名）、states（状态列表：label 状态名、"
        "initial 是否为初始状态可空、evidence 原文短引用）、transitions（迁移列表：from/to "
        "用**整数下标**引用 states 数组（0-based）、event 触发事件可空、condition 守卫条件"
        "可空、action 迁移动作可空、evidence 原文短引用）。与流程分界：processes 以**活动**"
        "为中心，states 以**对象状态**为中心；与规则分界：rules 是条件-动作**规范**，"
        "transitions 表达**状态拓扑**。\n"
        "7. functions：**函数指标**（预测决策 v6）。判定线索：文本定义**可计算的指标/"
        "函数**（如「压力函数=需求强度÷供给能力」「风险承受能力综合评估得分」）。"
        "每条给 name（指标名）、subject（作用主体用实体的 canonical，可空）、formula"
        "（计算定义，自然语言/运算符描述即可，不必可执行）、inputs（依赖因子，实体的 "
        "canonical 或字面量名，可空数组）、output_type（从 "
        f"{'/'.join(_FUNC_OUTPUT_TYPES)} 中选，判不出用 OTHER）、evidence。与属性分界："
        "attributes 存**字面量值**（当前值），functions 存**计算定义**。\n"
        "8. temporal：**时序时态**（预测决策 v6）。判定线索：文本把**时间限定**绑定到"
        "某事实/规则/流程/状态（如「自2017年7月1日起实施」「每年进行一次评估」"
        "「未来3个月」）。每条给 subject（主体：事实/规则/流程名或实体 canonical）、kind"
        "（从 "
        f"{'/'.join(_TEMPORAL_KINDS)} 中选：生效/有效期→VALIDITY，频率/周期→FREQUENCY，"
        "时间窗/预测窗口→WINDOW，触发时点→TRIGGER，判不出用 VALIDITY）、value（时间描述"
        "**逐字取自原文**）、anchor（时间锚点，可空）、evidence。与 DATE 实体分界：DATE "
        "是**实体**（图中节点），temporal 是**把时间语义绑定到主体**。\n"
        "9. actions：**动作处置**（预测决策 v6）。判定线索：文本描述**处置动作及其级别/"
        "副作用/触发情形**（如「书面风险警示」「暂停销售」「强制平仓」「上报监管部门」）。"
        "每条给 name（动作名）、actor（执行者用实体的 canonical，可空）、level（从 "
        f"{'/'.join(_ACTION_LEVELS)} 中选：观察/记录→OBSERVE，警示/提示→WARN，干预/处置→"
        "INTERVENE，判不出用 OBSERVE）、target（作用对象，可空）、side_effect（副作用/"
        "代价，可空）、trigger（触发情形，可空）、evidence。与规则分界：rules 是条件-动作"
        "**规范**（该不该做），actions 是处置动作的**元信息**（怎么做/什么级别）。\n"
        "10. constraints：**结构约束**（预测决策 v6）。判定线索：文本给出**结构合法性**"
        "约束（SHACL 式）：基数（至少/至多一个）、值域（在…至…之间/不得低于）、枚举"
        "（只能为以下之一/分为…五级）、不相交（不得同时是）、必填（应当具备/必须提供）、"
        "一致性（不得矛盾）。每条给 subject（约束对象，用实体的 canonical）、type（从 "
        f"{'/'.join(_CONSTRAINT_TYPES)} 中选：基数→CARDINALITY，值域/区间→VALUE_RANGE，"
        "枚举取值→ENUM，类不相交→DISJOINT，必填/必备→REQUIRED，一致性→CONSISTENCY，"
        "判不出用 CONSISTENCY）、description（约束内容，可含原文）、evidence。与规则分界："
        "rules 是「该做什么」的**规范**（道义模态），constraints 是「什么合法」的**结构**"
        "约束（SHACL 式）。\n"
        "11. permissions：**主体-动作授权**（预测决策 v6）。判定线索：文本定义**谁能"
        "（不能）执行什么动作**（如「普通投资者可以申请转化成为专业投资者」「不得向"
        "风险承受能力等级不匹配的投资者销售产品」）。每条给 actor（主体，用实体的 "
        "canonical）、action（动作，尽量对应 actions 的动作名）、effect（从 "
        f"{'/'.join(_PERMISSION_EFFECTS)} 中选：允许→PERMIT，禁止→DENY，判不出用 DENY）、"
        "scope（授权范围，可空）、evidence。与规则分界：rules 的 PROHIBITION 是**业务**"
        "禁令（不得销售不匹配产品），permissions 是**主体-动作授权边界**（仅特定角色可"
        "执行）；同一句可同时产出 rules 与 permissions。\n"
        "12. 不要输出字符偏移、不要输出 ID、不要输出 object_literal 字段。\n"
        "只输出 JSON，不要任何解释。\n\n"
        "==== 文档开始 ====\n"
        f"{text}\n"
        "==== 文档结束 ===="
    )


def build_parse_prompt(text: str, fmt_label: str) -> str:
    return (
        f"请把下面的{fmt_label}原始文档解析成「统一结构化文本 + 版式区块」。\n"
        "要求：\n"
        "1. text：文档的完整纯文本（**逐字保留原文，不要增删改**，长度须与原文一致）。\n"
        "2. blocks：按阅读顺序的版式区块，每个含 kind（"
        f"{'/'.join(_PARSE_KINDS)}）、text（该块纯文本）、"
        "标题可带 level、表格带 rows（二维字符串数组）。\n"
        "只输出 JSON，不要任何解释。\n\n"
        "==== 原始文档开始 ====\n"
        f"{text}\n"
        "==== 原始文档结束 ===="
    )


# ---------------------------------------------------------------------------
# 生产实现：宿主 agent（豆包）充当 LLM
# ---------------------------------------------------------------------------

_SYSTEM_EXTRACT = (
    "你是知识图谱抽取引擎。严格按用户给出的 JSON Schema 输出，"
    "只返回 JSON 对象，不要 markdown 代码块、不要解释。"
)
_SYSTEM_PARSE = (
    "你是文档版式解析引擎。严格按用户给出的 JSON Schema 输出，"
    "text 必须与原文逐字一致。只返回 JSON 对象，不要解释。"
)


class HostAgentLLMProvider(LLMProvider):
    """宿主 agent（豆包）充当 LLM 的 provider（方式 3 · 宿主即 LLM）。

    语义：抽取 / 解析的「智能」由**宿主**承担——宿主读文档后按契约产出
    ``{entities:[...], relations:[...]}``（抽取）或 ``{text, blocks}``（解析），
    通过 ``inject()`` 注入本 provider；Python 薄壳只做确定性映射。

    本实现**不读取任何 ``SMINI_LLM_*`` 环境变量**，**不发起任何模型 HTTP
    调用**——模型能力完全来自运行环境的宿主，无需外部 API 凭证。

    用法：::

        llm = HostAgentLLMProvider()
        llm.inject({"entities": [...], "relations": [...]})   # 宿主交卷
        res = ExtractStep().transform([nd], RunContext(llm=llm))

    宿主也可经 CLI 交卷：``python -m smini.cli build <src> --host-contract <file>``
    （CLI 读契约 JSON 后自动注入）。
    """

    def __init__(
        self,
        contract: dict[str, Any] | None = None,
        *,
        model: str = "host-agent",
    ) -> None:
        self._contract = contract
        self.model_name = model

    def inject(self, contract: dict[str, Any]) -> None:
        """宿主按契约交卷：注入抽取 / 解析结果。"""
        self._contract = contract

    def is_available(self) -> bool:
        return self._contract is not None

    def extract(
        self, prompt: str, schema: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._contract is None:
            raise DependencyMissing(
                "宿主 agent 尚未充当 LLM（未 inject 契约 / 未传 --host-contract）："
                "零规则架构无确定性兜底，抽取将为空"
            )
        return dict(self._contract)


class StubLLMProvider(LLMProvider):
    """测试桩：返回预置 dict（或按 prompt+schema 计算的 dict）。

    用于契约/端到端测试，证明「LLM 提议 → Python 归一」链路正确且幂等，
    不依赖任何网络或真实模型。
    """

    def __init__(
        self,
        response: dict[str, Any] | Callable[[str, Mapping[str, Any] | None], dict[str, Any]],
        *,
        model: str = "stub",
    ) -> None:
        self._response = response
        self.model_name = model

    def is_available(self) -> bool:
        return True

    def extract(
        self, prompt: str, schema: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if callable(self._response):
            return self._response(prompt, schema)
        return dict(self._response)


# 兼容别名：宿主即 LLM 是生产路径的默认实现（无外部凭证依赖）
default_llm = HostAgentLLMProvider
