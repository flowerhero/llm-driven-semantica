"""smini.types — 全流水线统一数据契约。

设计立场
--------
semantica 原版**没有数据契约**：全仓库零 dataclass，Parse 输出是裸 dict，
KG 节点/边也是裸 dict，跨阶段靠字段名隐式耦合（`semantica-8步流水线
源码解析.md` §结论二）。本模块是精简实现的第一块地基 —— 先把"每一步
吃什么、吐什么"钉死，再谈算法。

贯穿全局的四条不变量
--------------------
1. **ID 确定性**：所有 ID 由内容/语义决定（sha256），绝不用 `hash()`。
   任何时刻重跑同一批数据，ID 必须逐字节相同。
2. **偏移可回溯**：从 Parse 起，所有 span 都锚定到一个明确的坐标系，
   且归一化造成的坐标漂移由 `SpanPatch` 显式记录，可逆向映射回原文。
   原版没有这个，导致 Extract 出的实体无法定位到原文哪一句。
3. **溯源不可省**：`Provenance` 是每个可交付对象的必填字段，不是可选装饰。
4. **降级要出声**：能力缺失（没装模型、没 API key）必须写进
   `Degradation`，而不是静默返回原样数据。原版降级是静默的
   （`semantica/semantic_extract/ner_extractor.py`），出了问题无从排查。

坐标系约定
----------
* `RawDocument.content`  —— bytes 坐标系（原始字节）
* `ParsedDocument.text`  —— orig 坐标系（解码后的字符偏移）
* `NormalizedDocument.text` —— norm 坐标系
* `Provenance.char_span` —— 默认 **norm 坐标系**，可用
  `map_span_back()` 换算回 orig 坐标系

序列化
------
所有类型都可经 `to_dict()` 转 JSON 友好结构（datetime→ISO8601，
Enum→value，bytes→base64）。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Sequence

__all__ = [
    # 枚举
    "SourceType", "DocumentFormat", "BlockKind", "EntityType",
    "ConflictKind", "ResolutionStrategy", "MergeStrategy", "DegradationKind",
    # 双时态
    "TimeInterval", "BiTemporal",
    # 溯源
    "SourceRef", "Provenance",
    # Step 1
    "RawDocument",
    # Step 2
    "ParsedDocument", "Block",
    # Step 3
    "NormalizedDocument", "SpanPatch", "map_offset_back", "map_span_back",
    # Step 4
    "Chunk", "EntityMention", "Relation", "Triplet", "ExtractionResult", "Degradation",
    # Step 4 属性抽取
    "AttributeValueType",
    # Step 4 业务规则抽取
    "RuleModality", "Rule",
    # Step 4 业务流程抽取
    "StepKind", "FlowType", "ProcessStep", "ProcessFlow", "Process",
    # Step 4 预测决策本体抽取（v6）
    "FunctionOutputType", "TemporalKind", "ActionLevel", "ConstraintType",
    "PermissionEffect", "RelationPropKind",
    # 2026-09-20 借鉴（v9）：数据范围 / 流程类型 / 审批结果三态
    "DataScope", "ProcessFlowType", "ApprovalOutcome",
    "StateDef", "TransitionDef", "StateMachine",
    "Function", "Temporal", "Action", "Constraint", "Permission",
    # Step 5
    "Entity", "KGEdge", "KnowledgeGraph", "GraphStats",
    # Step 6
    "ConflictValue", "Conflict", "Resolution", "DuplicateCluster", "QAResult",
    "QualityMetrics",
    # Step 7
    "VectorRecord", "StoreReceipt", "SearchHit",
    # Step 8
    "Citation", "ContextPackage",
    # 错误
    "SminiError", "ContractViolation", "StepError", "DependencyMissing",
    "DimensionMismatch",
    # 工具
    "to_dict", "utcnow",
]


# ===========================================================================
# 0. 基础工具
# ===========================================================================

def utcnow() -> datetime:
    """统一的时间源入口。

    流水线内部**禁止**直接调 `datetime.now()` —— 时间必须走
    `RunContext.now()` 注入，否则无法复现历史构建。这里只作为
    构造默认值时的兜底。
    """
    return datetime.now(timezone.utc)


class _StrEnum(str, Enum):
    """可 JSON 直出的 Enum 基类。

    用 `str, Enum` 双继承，使得 `json.dumps(EntityType.PERSON)` 直接得到
    `"PERSON"`，不必在序列化层写特殊分支。
    """

    def __str__(self) -> str:  # pragma: no cover
        return self.value

    @classmethod
    def from_str(cls, s: str) -> "_StrEnum":
        """宽松解析：大小写不敏感，失败给明确错误而不是 None。"""
        try:
            return cls(s)
        except ValueError:
            try:
                return cls[s.strip().upper()]
            except KeyError:
                valid = ", ".join(m.value for m in cls)
                raise ValueError(f"{cls.__name__} 非法值 {s!r}，可选：{valid}") from None


# ===========================================================================
# 1. 枚举定义
# ===========================================================================

class SourceType(_StrEnum):
    FILE = "file"
    WEB = "web"
    DB = "db"
    STREAM = "stream"
    TEXT = "text"          # 内存字符串，便于测试与 API 直投


class DocumentFormat(_StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    HTML = "html"
    MARKDOWN = "markdown"
    TEXT = "text"
    CODE = "code"
    JSON = "json"
    CSV = "csv"
    XML = "xml"
    EPUB = "epub"
    IMAGE = "image"        # 需 OCR 路径
    AUDIO = "audio"        # 需 ASR 路径
    UNKNOWN = "unknown"


class BlockKind(_StrEnum):
    PARAGRAPH = "paragraph"
    HEADING = "heading"
    TABLE = "table"
    CODE = "code"
    LIST_ITEM = "list_item"
    QUOTE = "quote"
    CAPTION = "caption"
    IMAGE_REF = "image_ref"
    FOOTNOTE = "footnote"
    METADATA = "metadata"


class EntityType(_StrEnum):
    """精简实体类型表。

    原版用的是自由字符串（各抽取器各写各的），导致跨抽取器无法对齐。
    这里收敛为封闭枚举 + `OTHER` 逃生口。

    来源：前 7 类与 MUC-6/7 经典 NER（PERSON ORGANIZATION LOCATION DATE
    TIME MONEY PERCENT）逐字一致；PRODUCT/EVENT/QUANTITY 取自 OntoNotes 5.0；
    CONCEPT 为知识图谱/本体抽取场景自定义。v5 新增 7 个金融领域扩展类
    （FINANCIAL_INSTRUMENT REGULATION FINANCIAL_INDICATOR MARKET RISK
    TRANS SECURITY_CODE），依据金融 NER taxonomy（FIN_INST/LEGAL/KPI/
    RISK/TRANS 等）与金融领域惯例。
    """
    PERSON = "PERSON"
    ORGANIZATION = "ORGANIZATION"
    LOCATION = "LOCATION"
    DATE = "DATE"
    TIME = "TIME"
    MONEY = "MONEY"
    PERCENT = "PERCENT"
    QUANTITY = "QUANTITY"
    PRODUCT = "PRODUCT"
    EVENT = "EVENT"
    CONCEPT = "CONCEPT"
    # --- 金融领域扩展类（v5，仅金融文本建议启用，通用文本可继续用 CONCEPT/OTHER）---
    FINANCIAL_INSTRUMENT = "FINANCIAL_INSTRUMENT"
    REGULATION = "REGULATION"
    FINANCIAL_INDICATOR = "FINANCIAL_INDICATOR"
    MARKET = "MARKET"
    RISK = "RISK"
    TRANS = "TRANS"
    SECURITY_CODE = "SECURITY_CODE"
    OTHER = "OTHER"


class AttributeValueType(_StrEnum):
    """属性值类型（独立于实体类型，只描述**字面量**属性值的取值形态）。

    与 ``EntityType`` 刻意分开：属性值是字面量（数据属性 DatatypeProperty），
    不是实体。判不出用 ``OTHER``，不自创类型。
    """
    DATE = "DATE"
    TIME = "TIME"
    MONEY = "MONEY"
    PERCENT = "PERCENT"
    QUANTITY = "QUANTITY"
    NUMBER = "NUMBER"          # 纯数字（无单位），如 3家、共5名
    BOOLEAN = "BOOLEAN"        # 是/否、有/无、真/假
    ENUM = "ENUM"              # 枚举值：高/中/低、合格/不合格、已实施/未实施
    STRING = "STRING"          # 普通字符串（如地址、名称片段）
    # v7 借鉴（M1 对象模型）：字典引用 / 实体引用——属性值不是纯字面量，
    # 而是指向某字典（如"报案方式=下拉选择"）或另一实体（如"所属部门=部门"）。
    # 判不出仍用 STRING/OTHER，不自创类型。
    DICT_REF = "DICT_REF"      # 字典引用：值来自某字典/码表（含下拉选项语义）
    ENTITY_REF = "ENTITY_REF"  # 实体引用：值指向另一实体（AggregateRootRef 语义）
    OTHER = "OTHER"


class RuleModality(_StrEnum):
    """业务规则模态（独立于实体类型 / 属性值类型，只描述**规则**的规范性）。

    法律/监管文本的道义（Deontic）语义四模态（义务/禁止/许可/豁免）收敛为
    本枚举的前三类 + ``CONDITIONAL``（无模态词的纯 IF-THEN 派生，如"由低到高
    至少划分为五级"这类结构性推导）。判不出用 ``OTHER``，不自创类型。
    """
    OBLIGATION = "OBLIGATION"      # 义务：应当 / 必须 / 应 / 须
    PROHIBITION = "PROHIBITION"    # 禁止：不得 / 禁止 / 严禁 / 不可
    PERMISSION = "PERMISSION"      # 许可：可以 / 有权 / 允许 / 可
    CONDITIONAL = "CONDITIONAL"    # 条件派生（非道义）：如果/当…时…则…，无模态词
    OTHER = "OTHER"                # 逃生口：判不出就用它


class RuleType(_StrEnum):
    """业务规则用途类型（v7 借鉴 M3 规则模型 ruleType，与 modality 正交并存）。

    modality 回答「该不该做」（道义模态），rule_type 回答「规则是什么用途」
    （校验/推导/触发）。二者独立：一条"应当…"规则可以是校验型也可以是
    触发型。判不出用 ``OTHER``，不自创类型。
    """
    VALIDATION = "VALIDATION"    # 校验型：前置条件校验/资格判定（M3 VALIDATION）
    DERIVATION = "DERIVATION"    # 推导型：由输入推导出结论/计算（M3 INFERENCE）
    TRIGGER = "TRIGGER"          # 触发型：满足条件时触发某动作/流转
    OTHER = "OTHER"              # 逃生口：判不出就用它


class Certainty(_StrEnum):
    """业务内容确定性分级（v7 借鉴对方需求探索 A/B 类分级思想）。

    读端不硬暂停人工确认，但把「写死的企业专属参数」打标，供消费方知道
    哪些值需要人工复核：GENERAL=行业通用（监管常设），ENTERPRISE=企业专属
    （如"≤3000元自动核赔""14天内自动立案"这类阈值/时限）。
    判不出用 ``OTHER``，不自创类型。
    """
    GENERAL = "GENERAL"          # 行业通用：监管/行业惯例，跨企业稳定
    ENTERPRISE = "ENTERPRISE"    # 企业专属：本企业写死的参数（阈值/时限/费率）
    OTHER = "OTHER"              # 逃生口：判不出就用它


class ActorType(_StrEnum):
    """主体类型（v7 借鉴 M5 主体模型 actorType：HUMAN/SYSTEM）。

    用于 permissions/actions 的主体：人工角色 vs 系统自动主体（如
    "收付费系统自动触发""系统自动立案"）。判不出用 ``OTHER``，不自创类型。
    """
    HUMAN = "HUMAN"              # 人工：业务人员/审核人员/客户
    SYSTEM = "SYSTEM"            # 系统：自动任务/定时触发/外部系统联动
    OTHER = "OTHER"              # 逃生口：判不出就用它


class StepKind(_StrEnum):
    """流程步骤类型（独立于实体/属性/规则枚举，只描述**流程步骤**的角色）。

    对齐 BPMN 元模型的 Activity/Event/Gateway，但收敛为 3 类足够覆盖业务
    文档流程：活动（做什么）、决策点（分叉/匹配）、事件（触发/结束）。
    判不出用 ``TASK``，不自创类型。
    """
    TASK = "TASK"                  # 活动/任务：填表、评估、签署、回访
    GATEWAY = "GATEWAY"            # 决策/分支点：判定、选择、匹配、若…则分两路
    EVENT = "EVENT"                # 事件：开始/结束/触发（判不出不抽）
    # v7 借鉴（M6 流程模型）：步骤角色细分——START/END 对齐 BPMN Start/End
    # Event，SYSTEM_TASK 对齐 System Task（系统自动步骤，无需人工操作）。
    START = "START"                # 开始事件：流程起点
    END = "END"                    # 结束事件：流程终点
    SYSTEM_TASK = "SYSTEM_TASK"    # 系统自动任务：由系统执行，无人工参与


class FlowType(_StrEnum):
    """控制流类型（独立枚举，只描述**流程步骤之间的连接**）。

    对齐 Workflow Patterns 基本控制模式（van der Aalst）：Sequence / Parallel
    Split(AND) / Exclusive Choice(XOR) / Loop。合并（join/同步）不显式建模——
    分支汇合到下一公共步骤由 LLM 隐式表达。判不出用 ``SEQUENCE``，不自创类型。
    """
    SEQUENCE = "SEQUENCE"          # 顺序：先…再…然后…最后；依次
    PARALLEL = "PARALLEL"          # 并行（AND-split）：同时；同步；一并
    CONDITIONAL = "CONDITIONAL"    # 排他分支（XOR-split）：如果…则…否则…；当…时
    LOOP = "LOOP"                  # 循环：重复；直到；每次…都


class FunctionOutputType(_StrEnum):
    """函数指标输出类型（独立枚举，只描述**函数指标的取值形态**）。

    预测决策本体 v6 新增：函数指标（functions）把业务经验写成可计算定义，
    输出类型独立于实体/属性枚举。判不出用 ``OTHER``，不自创类型。
    """
    NUMBER = "NUMBER"              # 数值（压力函数、风险强度函数）
    PERCENT = "PERCENT"            # 百分比（履约率、完成率）
    RANK = "RANK"                  # 分级/排序（高中低、等级）
    BOOLEAN = "BOOLEAN"            # 真/假（是否达标）
    ENUM = "ENUM"                  # 枚举（如状态标签）
    STRING = "STRING"              # 普通字符串
    OTHER = "OTHER"


class TemporalKind(_StrEnum):
    """时态标注类型（独立枚举，只描述**时间语义如何绑定到主体**）。

    预测决策本体 v6 新增：temporal 把时间限定（何时生效/多频繁/什么窗口/
    何时触发）绑定到某事实/规则/流程/状态。判不出用 ``VALIDITY``。
    """
    VALIDITY = "VALIDITY"          # 生效/失效区间：自…起实施/施行；有效期
    FREQUENCY = "FREQUENCY"        # 频率/周期：每年/每月/定期
    WINDOW = "WINDOW"              # 时间窗/预测窗口：未来2小时/近期
    TRIGGER = "TRIGGER"            # 触发时点：收到…时/当…时触发


class ActionLevel(_StrEnum):
    """动作处置级别（独立 3 类枚举，对齐文章预测输出五项之「建议动作级别」）。

    预测决策本体 v6 新增：actions 的处置动作分级——观察/预警/干预。
    判不出用 ``OBSERVE``，不自创类型。
    """
    OBSERVE = "OBSERVE"            # 观察/记录：记录、监测、留痕、保存
    WARN = "WARN"                  # 警示/提示：书面风险警示、提示告知、签署确认
    INTERVENE = "INTERVENE"        # 干预/处置：暂停销售、强制平仓、拒绝交易、上报


class ConstraintType(_StrEnum):
    """约束类型（独立 6 类枚举，对齐 SHACL 约束组件）。

    预测决策本体 v6 新增：constraints 是结构合法性约束（SHACL 式），与 rules
    的业务规范（应当/不得）不同。判不出用 ``CONSISTENCY``，不自创类型。
    """
    CARDINALITY = "CARDINALITY"    # 基数：至少/至多/必须有一个（sh:minCount/maxCount）
    VALUE_RANGE = "VALUE_RANGE"    # 值域/区间：在…至…之间；不得低于/高于（sh:minInclusive）
    ENUM = "ENUM"                  # 枚举取值：只能为以下之一；分为…五级（sh:in）
    DISJOINT = "DISJOINT"          # 类不相交：不得同时是…；互斥（owl:disjointWith）
    REQUIRED = "REQUIRED"          # 必填/必备：应当具备/必须提供（sh:minCount≥1）
    CONSISTENCY = "CONSISTENCY"    # 一致性/完整性：前后一致；不得矛盾


class PermissionEffect(_StrEnum):
    """权限效果（独立 2 类枚举，RBAC 授权语义）。

    预测决策本体 v6 新增：permissions 定义主体-动作授权边界。判不出用
    ``DENY``（默认禁止，最小权限原则）。
    """
    PERMIT = "PERMIT"              # 允许执行
    DENY = "DENY"                  # 禁止执行


class DataScope(_StrEnum):
    """数据可见范围（2026-09-20 借鉴 sharptoolbox v9 M5 Permission.dataScope）。

    权限除「谁能做什么」外，还表达「能看到哪一层数据」——金融场景的
    本人/本部门/全机构数据隔离。判不出用 ``OTHER``，不自创类型。
    """
    ALL = "ALL"                    # 全机构可见
    OWN = "OWN"                    # 仅本人/本人名下数据
    DEPT = "DEPT"                  # 本部门/本单位范围
    CUSTOM = "CUSTOM"              # 自定义范围（配合 scope 描述）
    OTHER = "OTHER"                # 逃生口：判不出就用它


class ProcessFlowType(_StrEnum):
    """流程类型（2026-09-20 借鉴 sharptoolbox v9 M6 flowType）。

    流程整体的类别：端到端协同流（COLLABORATION）或审批流（APPROVAL）。
    与 FlowType（步骤间控制流 SEQUENCE/PARALLEL/CONDITIONAL/LOOP）正交：
    流程级类型描述「这是什么流」，控制流描述「步骤怎么连」。
    判不出用 ``COLLABORATION``，不自创类型。
    """
    COLLABORATION = "COLLABORATION"  # 端到端业务协同流（默认）
    APPROVAL = "APPROVAL"            # 审批流：审批/审核/复核/批准/会签


class ApprovalOutcome(_StrEnum):
    """审批结果三态（2026-09-20 借鉴 sharptoolbox v9 M6 approvalOutcomes）。

    审批步骤的处理结果：通过 / 否决（流程终止）/ 退回（修改后重报）。
    与 ``reject_to`` 配合：RETURN 必有 reject_to（回到修改步骤），REJECT
    通常无 reject_to（流程终止）。判不出用 ``OTHER``，不自创类型。
    """
    APPROVE = "APPROVE"            # 通过：同意/批准/审核通过
    REJECT = "REJECT"              # 否决：不予批准/否决/驳回申请（流程终止）
    RETURN = "RETURN"              # 退回：退回修改/退回重报/补正材料（配合 reject_to）
    OTHER = "OTHER"                # 逃生口：判不出就用它


class RelationPropKind(_StrEnum):
    """关系传导类型（独立 4 类枚举，增强 relations 的传导语义）。

    预测决策本体 v6 新增：prop_kind 标注该关系是否承载影响/依赖/因果/触发
    语义，供下游图传播/风险传导计算用。普通事实关系省略即默认。
    """
    FACTUAL = "FACTUAL"            # 普通事实关系（默认）
    DEPENDENCY = "DEPENDENCY"      # 依赖：依赖/制约/取决于
    CAUSAL = "CAUSAL"              # 因果：导致/引发/造成
    TRIGGER = "TRIGGER"            # 触发：触发/激活/启动


class ConflictKind(_StrEnum):
    """原版 `conflicts/conflict_detector.py:60-95` 的 5 类，保留并显式化。"""
    VALUE = "value"        # 同一 (s,p) 多个不同值
    TYPE = "type"          # 同一实体被赋予互斥类型
    TEMPORAL = "temporal"  # 时间区间矛盾（如任期重叠）
    MUTEX = "mutex"        # 违反互斥约束（如一人不能同时是两个公司的 CEO）
    LOGIC = "logic"        # 逻辑矛盾（如出生晚于死亡）


class ResolutionStrategy(_StrEnum):
    MOST_RECENT = "most_recent"
    HIGHEST_CONFIDENCE = "highest_confidence"
    MAJORITY_VOTE = "majority_vote"
    SOURCE_PRIORITY = "source_priority"
    KEEP_ALL = "keep_all"      # 多值时全部保留（时序场景下常用）
    MANUAL = "manual"          # 挂起，进人工审核队列


class MergeStrategy(_StrEnum):
    KEEP_MOST_COMPLETE = "keep_most_complete"
    KEEP_FIRST = "keep_first"
    KEEP_LONGEST = "keep_longest"
    UNION_PROPERTIES = "union_properties"


class DegradationKind(_StrEnum):
    """能力降级的原因分类。用于显式告知下游"这次跑得不全"。"""
    MODEL_MISSING = "model_missing"        # 未安装 spaCy 模型等
    UNAVAILABLE = "unavailable"            # 能力提供方（LLM 等）不可用 / 调用失败
    NO_CREDENTIAL = "no_credential"        # 缺 API key
    RATE_LIMITED = "rate_limited"
    PARSE_FAILED = "parse_failed"
    UNSUPPORTED_FORMAT = "unsupported_format"
    TRUNCATED = "truncated"                # 超长被截断


# ===========================================================================
# 2. 溯源（贯穿全链路）
# ===========================================================================

@dataclass(frozen=True)
class SourceRef:
    """指向一个数据来源的不可变引用。

    URI 统一用 scheme 前缀，便于下游按协议分流：
      * `file:///abs/path/to.pdf`
      * `https://example.com/page`
      * `postgres://host/db/table#row=42`
      * `memory://inline/<sha256>`（TEXT 源）
    """
    source_id: str
    uri: str
    source_type: SourceType
    checksum: str | None = None      # 内容 sha256；流式源可为 None
    fetched_at: datetime | None = None
    locator: str | None = None       # 源内定位：页码 / 行号 / 主键 / anchor
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri:
            raise ContractViolation("SourceRef.uri 不可为空")

    @classmethod
    def of(cls, uri: str, source_type: SourceType, **kw: Any) -> "SourceRef":
        from .ids import stable_id
        return cls(
            source_id=kw.pop("source_id", stable_id("src", uri, prefix="s_")),
            uri=uri,
            source_type=source_type,
            **kw,
        )


@dataclass(frozen=True)
class Provenance:
    """挂在实体/关系/事实上的溯源信息。

    与 semantica 的 PROV-O 完整模型（`semantica/provenance/`）不同，
    这里只保留**工程上真正会被查询**的四个维度：出处、抽取器、置信度、
    原文位置。W3C PROV-O 的 RDF 映射留到 Export 步按需生成 —— 不该让
    本体建模的复杂度污染主链路。
    """
    source_refs: tuple[SourceRef, ...] = ()
    extractor: str = ""                       # "regex.date.v1" / "spacy.en_core_web_sm" / "llm.gpt-4o"
    extracted_at: datetime | None = None
    confidence: float = 1.0
    char_span: tuple[int, int] | None = None  # norm 坐标系，见模块 docstring
    note: str = ""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ContractViolation(f"confidence 必须在 [0,1]，收到 {self.confidence}")
        if self.char_span is not None:
            s, e = self.char_span
            if s < 0 or e < s:
                raise ContractViolation(f"char_span 非法：{(s, e)}")

    @property
    def primary_source(self) -> SourceRef | None:
        return self.source_refs[0] if self.source_refs else None

    def merge(self, other: "Provenance") -> "Provenance":
        """合并两个溯源（同一事实被多个来源印证时）。

        来源取并集，置信度取最大值（多源印证只会增强而非削弱可信度）。
        """
        seen = {r.source_id for r in self.source_refs}
        refs = list(self.source_refs) + [
            r for r in other.source_refs if r.source_id not in seen
        ]
        return Provenance(
            source_refs=tuple(refs),
            extractor=self.extractor or other.extractor,
            extracted_at=self.extracted_at or other.extracted_at,
            confidence=max(self.confidence, other.confidence),
            char_span=self.char_span or other.char_span,
            note=self.note or other.note,
        )


# ===========================================================================
# 2.5 双时态（valid_time + transaction_time）
# ===========================================================================

@dataclass(frozen=True)
class TimeInterval:
    """一个时间区间，用于表达 valid_time。

    端点为 `None` 表示**无界**：
      * `start=None` —— 从无穷远过去开始
      * `end=None` —— 直到无穷远未来（即"至今仍有效"）

    **半开区间 `[start, end)`**：`end` 本身不属于区间。这是时间区间运算的
    通行约定，避免了"结束时刻到底算不算"的歧义，也让相邻区间可以无缝衔接
    而不重叠。
    """
    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ContractViolation(
                f"TimeInterval 端点倒置：start={self.start} > end={self.end}"
            )

    def contains(self, t: datetime) -> bool:
        """时刻 t 是否落在区间内（半开 [start, end)）。"""
        if self.start is not None and t < self.start:
            return False
        if self.end is not None and t >= self.end:
            return False
        return True

    def overlaps(self, other: "TimeInterval") -> bool:
        """两区间是否重叠。无界端按 ±∞ 处理。"""
        if self.end is not None and other.start is not None and self.end <= other.start:
            return False
        if other.end is not None and self.start is not None and other.end <= self.start:
            return False
        return True

    @property
    def is_open_ended(self) -> bool:
        """是否至今仍有效。"""
        return self.end is None

    @classmethod
    def since(cls, start: datetime) -> "TimeInterval":
        return cls(start=start, end=None)

    @classmethod
    def until(cls, end: datetime) -> "TimeInterval":
        return cls(start=None, end=end)


@dataclass(frozen=True)
class BiTemporal:
    """双时态标记：事实**何时为真** + **何时被录入系统**。

    两个维度必须分开，因为它们回答的是不同的问题：

    * **valid_time（有效时间）** —— 这个事实在现实世界中哪段时间是真的。
      例：「张三是 A 公司 CEO」的 valid_time 是 2020-01 至 2023-06。
    * **transaction_time（事务时间）** —— 这个事实在我们的系统里哪段时间
      被当作真的。例：我们 2020-03 才知道他上任，2023-09 才发现他已离任
      （数据滞后修正）。

    分开的意义：**可以修正历史而不抹掉修正的痕迹**。
    单时态模型下，"后来发现之前记错了"只能直接覆盖，丢失了
    "我们在 3 月时以为的是什么"这一信息 —— 而审计场景恰恰需要它。

    注意：原版 semantica 的 `temporal_model.py` 做了完整的 BiTemporalFact
    建模。本版把它**扁平化**为可挂在任意对象上的标记，不引入独立的 fact
    实体 —— 少一层间接，对精简实现够用。
    """
    #: 现实世界中为真的区间
    valid: TimeInterval = field(default_factory=TimeInterval)
    #: 录入系统的时刻（transaction_time 起点）
    recorded_at: datetime | None = None
    #: 作废时刻（transaction_time 终点）。None = 当前版本，仍被采信
    invalidated_at: datetime | None = None

    def __post_init__(self) -> None:
        if (
            self.recorded_at is not None
            and self.invalidated_at is not None
            and self.invalidated_at < self.recorded_at
        ):
            raise ContractViolation(
                f"invalidated_at({self.invalidated_at}) 早于 "
                f"recorded_at({self.recorded_at})"
            )

    @property
    def is_current(self) -> bool:
        """是否为当前版本（尚未被作废）。"""
        return self.invalidated_at is None

    def valid_at(self, t: datetime) -> bool:
        """在有效时间的 t 时刻，这个事实是否为真。"""
        return self.valid.contains(t)

    def known_at(self, t: datetime) -> bool:
        """在事务时间的 t 时刻，这条记录是否已被系统采信。"""
        if self.recorded_at is not None and t < self.recorded_at:
            return False
        if self.invalidated_at is not None and t >= self.invalidated_at:
            return False
        return True

    def invalidate(self, at: datetime) -> "BiTemporal":
        """作废当前版本，返回新对象（frozen，不改自身）。"""
        return BiTemporal(
            valid=self.valid,
            recorded_at=self.recorded_at,
            invalidated_at=at,
        )


def _same_temporal(a: BiTemporal | None, b: BiTemporal | None) -> bool:
    """两个双时态标记是否指向同一个版本。

    用于判断"新来的事实"是已有版本的重复印证，还是一个**新版本**。
    这是双时态下 add_edge 能否幂等的关键：
      * 相同 → 合并置信度与来源（不是新版本）
      * 不同 → 追加为新版本（保留历史）
    """
    return a == b


# ===========================================================================
# 3. Step 1 · Ingest
# ===========================================================================

@dataclass
class RawDocument:
    """Ingest 的唯一产物：**原样搬运，不做任何解读**。

    关键决策：`content` 是 `bytes` 而不是 `str`。

    原版在 ingest 阶段就把内容解码成 str，一旦猜错编码，后续全部失真且
    无从恢复。这里把编码推断推迟到 Parse 步 —— 那是第一个真正需要理解
    内容的环节，也只有那里才有格式线索（HTML 的 charset、PDF 的嵌入字体）。

    Ingest 只负责三件事：拿到字节、算校验和、记下出处。
    """
    doc_id: str
    source: SourceRef
    media_type: str = "application/octet-stream"
    content: bytes = b""
    size_bytes: int = 0
    checksum: str = ""
    fetched_at: datetime = field(default_factory=utcnow)
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.content and not self.checksum:
            from .ids import content_hash
            self.checksum = content_hash(self.content)
        if self.content and not self.size_bytes:
            self.size_bytes = len(self.content)


# ===========================================================================
# 4. Step 2 · Parse
# ===========================================================================

@dataclass(frozen=True)
class Block:
    """文档内的一个结构化区块。

    `char_start`/`char_end` **锚定在 `ParsedDocument.text` 上**，这是整条
    链路偏移回溯的第一根桩。原版的 parse 输出只有文本没有偏移，
    导致后面所有"这句话出自原文哪里"的问题都答不上来。
    """
    block_id: str
    kind: BlockKind
    text: str
    order: int                       # 文档内全局序号，跨 kind 单调
    char_start: int
    char_end: int
    level: int | None = None         # heading 层级 / list 嵌套深度
    rows: list[list[str]] | None = None   # TABLE 的结构化内容
    language: str | None = None      # CODE 块的语言
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.char_end < self.char_start:
            raise ContractViolation(
                f"Block.char_end({self.char_end}) < char_start({self.char_start})"
            )


@dataclass
class ParsedDocument:
    """Parse 的产物：把一种格式变成**统一的 文本 + 区块序列**。

    不变量：
      * `blocks` 按 `order` 升序
      * 所有 block 的 span 都落在 `[0, len(text)]` 内
      * `text` 与 blocks 描述的字符区间一致（block 文本是 text 的切片）
    """
    doc_id: str
    format: DocumentFormat
    source: SourceRef
    text: str = ""
    blocks: list[Block] = field(default_factory=list)
    parser: str = ""                 # 实现标识，如 "pdfplumber"
    parser_version: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def char_len(self) -> int:
        return len(self.text)

    def blocks_of(self, kind: BlockKind) -> list[Block]:
        return [b for b in self.blocks if b.kind == kind]


# ===========================================================================
# 5. Step 3 · Normalize
# ===========================================================================

@dataclass(frozen=True)
class SpanPatch:
    """记录一次归一化替换造成的坐标漂移。

    归一化是有损变换（`NFC 组合`、`K/M/B → 数量级展开`、`2026年3月 → 2026-03`），
    前后文本长度会变。如果不记录映射，Extract 给出的 char_span 就只在新
    文本里有效，永远回不到原文 —— 溯源在第一步就断了。

    这是本实现相对原版**新增**的机制，原版没有任何等价物。
    """
    orig_start: int
    orig_end: int
    new_start: int
    new_end: int
    kind: str                 # "unicode_nfc" | "whitespace" | "casefold" | "date" | "number" | "alias"
    original: str = ""
    replacement: str = ""

    @property
    def delta(self) -> int:
        """长度变化量：正表示新文本变长。"""
        return (self.new_end - self.new_start) - (self.orig_end - self.orig_start)


def map_offset_back(
    offset: int,
    patches: Sequence[SpanPatch],
    *,
    at_end: bool = False,
) -> int:
    """把 norm 坐标系的偏移映射回 orig 坐标系。

    Args:
        offset: norm 坐标系中的字符偏移。
        patches: 按 `new_start` 升序排列的 SpanPatch 序列。
        at_end: True 表示此偏移是一个区间的**右端**，
                落在替换区内部时映射到 orig 区间右端（保守包含整个替换区）。

    落在被替换区间内部时无法精确还原，取保守策略：左端映射到区间起点、
    右端映射到区间终点。这保证映射回来的原文区间**一定覆盖**真实位置，
    宁可多引一个字，不能漏掉关键证据。
    """
    if not patches:
        return offset

    result = offset
    for p in patches:
        if p.new_end <= offset:
            # 该 patch 完全在 offset 之前：用它的尾部差值换算
            result = offset + (p.orig_end - p.new_end)
        elif p.new_start <= offset < p.new_end:
            result = p.orig_end if at_end else p.orig_start
            break
        else:
            break
    return max(result, 0)


def validate_patches(patches: Sequence[SpanPatch]) -> None:
    """校验 SpanPatch 序列自洽。

    构造 SpanPatch 极易出错：new 坐标必须与累积长度漂移吻合，否则映射
    结果会静默偏移几个字符 —— 这种 bug 极难排查，因为差得不明显。
    （写本文件的测试时就踩过两次。）

    这里强制检查两条几何约束：
      1. 在 new 坐标系下按 `new_start` 升序
      2. 在 new 坐标系下区间不重叠

    另外检查每个 patch 与累积漂移的自洽性（误差必须是常数偏移），
    不一致说明调用方算错了 new 坐标。

    Raises:
        ContractViolation: 任一约束被破坏。
    """
    prev_new_end = -1
    for i, p in enumerate(patches):
        if p.new_start < prev_new_end:
            raise ContractViolation(
                f"SpanPatch[{i}] new_start={p.new_start} 落在前一个 patch 的 "
                f"new 区间内（prev_new_end={prev_new_end}）—— 区间重叠或未按序排列"
            )
        if p.new_end < p.new_start:
            raise ContractViolation(
                f"SpanPatch[{i}] new_end({p.new_end}) < new_start({p.new_start})"
            )
        prev_new_end = p.new_end

    # 累积漂移自洽性：patch[i].new_start 应等于 orig_start + (前 i 个 patch 的 delta 之和)
    cum = 0
    for i, p in enumerate(patches):
        expected = p.orig_start + cum
        if expected != p.new_start:
            raise ContractViolation(
                f"SpanPatch[{i}] 坐标不自洽：orig_start={p.orig_start} 加上前面累积漂移 "
                f"{cum} 应得 new_start={expected}，实际为 {p.new_start}。"
                f"差值 {p.new_start - expected} 说明有替换未被记录，或有 patch 被重复计入"
            )
        cum += p.delta


def map_span_back(
    span: tuple[int, int],
    patches: Sequence[SpanPatch],
) -> tuple[int, int]:
    """把 norm 坐标系的区间映射回 orig 坐标系。"""
    s, e = span
    return (
        map_offset_back(s, patches, at_end=False),
        map_offset_back(e, patches, at_end=True),
    )


@dataclass
class NormalizedDocument:
    """Normalize 的产物。

    与 `ParsedDocument` 的关键差别：
      * `text` 是清洗后的文本（**norm 坐标系**）
      * `blocks` 的 span 已重新映射到新坐标系
      * `patches` 记录全部替换，供逆向映射使用
      * `mentions` 是归一化过程中**顺手**识别的规则实体（日期、金额、
        百分比等）。这些用正则就能高精度拿到，不必等 Extract 步的 NER，
        而且它们是分块时 entity-aware 的依据。
    """
    doc_id: str
    source: SourceRef
    text: str = ""
    blocks: list[Block] = field(default_factory=list)
    patches: list[SpanPatch] = field(default_factory=list)
    mentions: list["EntityMention"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_original_span(self, span: tuple[int, int]) -> tuple[int, int]:
        """norm → orig 坐标换算。溯源链靠这个方法闭合。"""
        return map_span_back(span, self.patches)

    def original_text(self, parsed: ParsedDocument, span: tuple[int, int]) -> str:
        """取出某个 norm 区间在原文中对应的文本。"""
        o_s, o_e = self.to_original_span(span)
        return parsed.text[o_s:o_e]


# ===========================================================================
# 6. Step 4 · Extract（含 Split）
# ===========================================================================

@dataclass(frozen=True)
class Chunk:
    """切分后的文本块。

    `char_start`/`char_end` 锚定在 **norm 坐标系**。

    `mention_ids` 是 entity-aware 切分的产出：记录落在本 chunk 内的实体
    提及。切分器据此保证"绝不从实体中间下刀"，也据此计算实体密度
    （用于决定是否需要回溯切小）。
    """
    chunk_id: str
    doc_id: str
    text: str
    char_start: int
    char_end: int
    index: int = 0
    block_ids: tuple[str, ...] = ()
    mention_ids: tuple[str, ...] = ()
    token_estimate: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.char_end < self.char_start:
            raise ContractViolation(f"Chunk 区间非法：{self.char_start}..{self.char_end}")


@dataclass
class EntityMention:
    """实体在一次**具体出现**中的记录（尚未消歧）。

    注意与 `Entity`（Step 5）的区别：
      * Mention = 文本里的一处出现，`mention_id` 由 (doc, span, 表面形式) 决定
      * Entity  = 图谱里的一个节点，`entity_id` 由 (类型, 规范名) 决定
      * 一个 Entity 对应零到多个 Mention

    这个区分原版是含混的，导致去重逻辑和抽取逻辑纠缠在一起。
    """
    mention_id: str
    doc_id: str
    text: str                      # 表面形式
    normalized: str                # 规范化形式，消歧的比对键
    entity_type: EntityType
    char_start: int
    char_end: int
    chunk_id: str | None = None
    sentence_id: str | None = None
    confidence: float = 1.0
    extractor: str = ""
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Relation:
    """两个 mention 之间的语义关系。

    `evidence_text`（契约即规则新增）：LLM 提供的原文短引用，用于溯源。
    `evidence_span` 是 Python 尽力定位出的字符区间，找不到时为 None。

    `prop_kind` / `strength`（预测决策本体 v6 增强）：关系是否承载影响/依赖/
    因果/触发语义 + 影响强度（HIGH/MEDIUM/LOW 或原文程度词），供下游图传播/
    风险传导计算用。空串表示普通事实关系（prop_kind 缺省为 FACTUAL）。
    """
    relation_id: str
    doc_id: str
    subject_ref: str               # mention_id
    object_ref: str                # mention_id
    predicate: str
    confidence: float = 1.0
    chunk_id: str | None = None
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prop_kind: str = ""            # v6：FACTUAL/DEPENDENCY/CAUSAL/TRIGGER；空=普通
    strength: str = ""             # v6：HIGH/MEDIUM/LOW 或原文程度词；空=未标注


@dataclass
class Triplet:
    """(主语, 谓词, 宾语) —— 图谱的原子事实单元。

    `object_literal=True` 时，`object` 是字面量（日期、金额、数字），
    图谱里表现为**节点属性**而非指向另一个节点。这个区分必须在这一步
    就定下来，否则建图时无法决定是加边还是加属性。

    `value_type`（属性抽取 v2 新增）：当本三元组是**实体属性**（本体论
    DatatypeProperty）时，记录属性值类型（`AttributeValueType`）。仅属性
    三元组非空；普通关系三元组为 None。LLM 不输出它之外的判别逻辑——
    由宿主在 `attributes[]` 契约中给出 `value_type`，薄壳校验后透传。
    """
    triplet_id: str
    subject: str
    subject_type: EntityType
    predicate: str
    object: str
    object_type: EntityType | None = None
    object_literal: bool = False
    value_type: "AttributeValueType | None" = None
    # v7 借鉴（M1 对象模型 required）：属性必录性（仅属性三元组使用）
    required: bool = False
    # v6：关系传导增强（透传到边属性，供图传播/风险传导）
    prop_kind: str = ""            # FACTUAL/DEPENDENCY/CAUSAL/TRIGGER；空=普通
    strength: str = ""             # HIGH/MEDIUM/LOW 或原文程度词；空=未标注
    confidence: float = 1.0
    source_mentions: tuple[str, ...] = ()
    chunk_id: str | None = None
    extractor: str = ""
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """业务规则：条件-动作/结论（规范/派生知识），契约即规则第四件套。

    与实体关系（ObjectProperty）和实体属性（DatatypeProperty）的分界：
    **含道义动词（应当/必须/不得/禁止/可以/有权）或条件-动作结构
    （如果/当…时…则…）→ 规则**；纯事实陈述 → relations/attributes。
    四件套并行不互斥（同一句可同时产出关系与规则）。

    字段语义对齐 SWRL（Body/IF → Head/THEN）骨架：
      * ``subject``  —— 规则主体（谁承担义务/被约束者），可空（纯派生/全局规则）
      * ``condition`` —— 触发条件（前件 IF），可空（无条件义务：直接"应当…"）
      * ``action``   —— 动作/结论（后件 THEN），必有
      * ``modality`` —— 规则模态（RuleModality 独立 5 类枚举）
      * ``evidence`` —— 支撑本规则的原文短引用（逐字，用于溯源）

    ``rule_id`` 由 (subject, condition, action, modality) 内容寻址（sha256），
    Python 补算、跨进程幂等，LLM 不输出。规则不进入实体-边图谱（规则不是
    实体间事实，KGEdge 的三元形态装不下 condition+modality），作为
    ``ExtractionResult.rules`` 独立产出保留。
    """
    rule_id: str
    doc_id: str
    subject: str
    condition: str
    action: str
    modality: RuleModality
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # v7 借鉴（M3 规则模型 + 对方 A/B 分级）：
    # rule_type=规则用途（与 modality 正交）；output_type=输出取值形态；
    # reused_by=被哪些流程/函数引用；certainty=行业通用/企业专属分级。
    # 均不参与 rule_id 内容寻址（幂等键不变）。
    rule_type: "RuleType" = RuleType.OTHER
    output_type: str = ""
    reused_by: list[str] = field(default_factory=list)
    certainty: "Certainty" = Certainty.OTHER


@dataclass
class ProcessStep:
    """流程步骤（第五件套 processes 的元素）。

    一个执行单元：做什么（label）+ 谁做（actor）+ 什么角色（kind）。
    ``step_id`` 由 (process_id, index, label) 内容寻址，Python 补算。
    ``index`` 是 steps 数组下标（0-based），flows 的 from/to 用它引用。
    """
    step_id: str
    process_id: str
    index: int
    label: str
    kind: StepKind
    actor: str = ""
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # v7 借鉴（M6 SUB_FLOW_CALL）：本步骤是对另一流程的子流程调用
    # （引用被调流程的 name；薄壳仅软校验存在性，不展开执行）。
    sub_process_ref: str = ""
    # 2026-09-20 借鉴（v9 M6 审批流增强）：泳道（角色/部门名，软引用）+
    # 驳回目标（整数下标引用 steps，越界丢弃）+ 审批结果三态（可空）。
    lane: str = ""
    reject_to: int | None = None
    approval_outcome: "ApprovalOutcome" = ApprovalOutcome.OTHER


@dataclass
class ProcessFlow:
    """流程控制流（第五件套 processes 的元素）。

    连接两个步骤：``from_index`` / ``to_index`` 引用 steps 数组下标（LLM 输出
    整数下标，Python 校验越界丢弃）。``type`` 是控制流类型（FlowType 独立
    枚举），``condition`` 仅 CONDITIONAL 分支携带。
    ``flow_id`` 由 (process_id, from, to, type, condition) 内容寻址，Python 补算。
    """
    flow_id: str
    process_id: str
    from_index: int
    to_index: int
    type: FlowType
    condition: str = ""
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 2026-09-20 借鉴（v9 M6 审批流增强）：驳回边条件（自然语言补充；
    # 与步骤级 reject_to 并存时以步骤级为权威）。
    on_reject: str = ""


@dataclass
class Process:
    """业务流程：有序活动 + 控制流（第五件套）。

    本体论依据：流程是 DOLCE 的 **perdurant**（持续体，在时间中延展），
    区别于实体（endurant）与属性（DatatypeProperty）。PSL 用 Activity 作为
    最小执行单元，BPMN 用 Process/Activity/Gateway/Actor/Artifact 建模，
    Workflow Patterns 给出顺序/并行/排他/循环基本控制模式。

    与规则（rules）的分界：规则是「条件→动作」的**规范**（该不该做），
    流程是「谁→按什么顺序→做什么」的**执行结构**（怎么走）。二者正交，
    同一文本可同时产出 rules 与 processes。

    ``process_id`` 由 name 内容寻址；**流程不进入实体-边图谱**（是持续体
    而非实体间事实），作为 ExtractionResult.processes 独立产出保留，
    build_kg 仅在 metadata 登记 ``process_count``。
    """
    process_id: str
    doc_id: str
    name: str
    steps: list[ProcessStep] = field(default_factory=list)
    flows: list[ProcessFlow] = field(default_factory=list)
    description: str = ""
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # v7 借鉴（M6 流程模型）：流程级前后置条件（进入流程前必须满足 /
     # 流程结束后必然成立的业务状态），自然语言表达，可空。
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)
    # 2026-09-20 借鉴（v9 M6 审批流增强）：流程类型（协同流/审批流）+
    # 审批链路摘要（角色/岗位名数组，冗余摘要；多级审批以 steps 顺序为准）。
    flow_type: "ProcessFlowType" = ProcessFlowType.COLLABORATION
    approval_chain: list[str] = field(default_factory=list)


# ===========================================================================
# 6.5 Step 4 · 预测决策本体抽取（v6：states/functions/temporal/actions/
#      constraints/permissions + relations 传导增强）
# ===========================================================================

@dataclass
class StateDef:
    """状态机的一个状态（第六件套 states 的元素）。

    ``state_id`` 由 (sm_id, index, label) 内容寻址，Python 补算。
    ``initial`` 标记初始状态（可空，默认 False）。
    """
    state_id: str
    sm_id: str
    index: int
    label: str
    initial: bool = False
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TransitionDef:
    """状态机的一条状态迁移（第六件套 states 的元素）。

    ``from_index`` / ``to_index`` 引用 states 数组下标（LLM 输出整数下标，
    Python 校验越界丢弃）。``event`` 触发事件、``condition`` 守卫条件、
    ``action`` 迁移动作——全部可空。``transition_id`` 由 (sm_id, from, to,
    event, condition) 内容寻址，Python 补算。
    """
    transition_id: str
    sm_id: str
    from_index: int
    to_index: int
    event: str = ""
    condition: str = ""
    action: str = ""
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StateMachine:
    """对象状态机（第六件套 states）。

    预测决策本体 v6：**预测 = 某对象在某个时间窗内从状态 A 演化到状态 B**
    （动态本体），states 表达对象生命周期状态迁移，是预测引擎的骨架。

    与流程（processes）分界：processes 以**活动**为中心（谁按什么顺序做
    什么）；states 以**对象状态**为中心（对象如何演化）。与规则（rules）
    分界：rules 是条件-动作**规范**，states 的 transition 表达**状态拓扑**
    （transition 的 condition 只是守卫，不是规范义务）。

    ``sm_id`` 由 (object, name) 内容寻址；**不入实体-边图**，作为
    ExtractionResult.states 独立产出保留，build_kg 登记 ``state_count``。
    """
    sm_id: str
    doc_id: str
    object: str
    name: str
    states: list[StateDef] = field(default_factory=list)
    transitions: list[TransitionDef] = field(default_factory=list)
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Function:
    """函数指标（第六件套 functions）。

    预测决策本体 v6：把业务经验写成**可计算定义**（如「压力函数 = 需求强度
    ÷ 供给能力」），是**推演**（如果调整会怎样）的输入与可解释性来源。

    与属性（attributes）分界：attributes 存**字面量值**（当前值），functions
    存**计算定义**（定义式 + 依赖因子）。同一指标可二者兼有。

    ``function_id`` 由 (name, subject, formula) 内容寻址；**不入实体-边图**，
    作为 ExtractionResult.functions 独立产出保留，build_kg 登记 function_count。
    """
    function_id: str
    doc_id: str
    name: str
    formula: str
    subject: str = ""
    inputs: list[str] = field(default_factory=list)
    output_type: "FunctionOutputType" = FunctionOutputType.OTHER
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Temporal:
    """时序时态标注（第六件套 temporal）。

    预测决策本体 v6：把时间限定（何时生效/多频繁/什么窗口/何时触发）绑定到
    某事实/规则/流程/状态。与 DATE 实体分界：DATE 是**实体**（图中节点），
    temporal 是**把时间语义绑定到主体**。

    ``temporal_id`` 由 (subject, kind, value) 内容寻址；**不入实体-边图**，
    作为 ExtractionResult.temporal 独立产出保留，build_kg 登记 temporal_count。
    """
    temporal_id: str
    doc_id: str
    subject: str
    kind: "TemporalKind"
    value: str
    anchor: str = ""
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Action:
    """动作处置（第六件套 actions）。

    预测决策本体 v6：处置动作的**元信息**——动作本身 + 级别 + 副作用 + 触发
    情形（对齐 Palantir ActionType 的 side-effects）。

    与规则（rules）分界：rules 是条件-动作**规范**（该不该做，道义模态）；
    actions 是处置动作的元信息（怎么做/什么级别/什么代价）。二者互补不迁移。

    ``action_id`` 由 (name, actor, level, trigger) 内容寻址；**不入实体-边图**，
    作为 ExtractionResult.actions 独立产出保留，build_kg 登记 action_count。
    """
    action_id: str
    doc_id: str
    name: str
    level: "ActionLevel"
    actor: str = ""
    target: str = ""
    side_effect: str = ""
    trigger: str = ""
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # v7 借鉴（M2 行为模型）：动作执行前后置条件（做什么之前必须成立 /
     # 执行后必然改变什么），自然语言，可空。
    precondition: str = ""
    postcondition: str = ""


@dataclass
class Constraint:
    """结构约束（第六件套 constraints）。

    预测决策本体 v6：结构合法性约束（SHACL 式：基数/值域/枚举/不相交/必填/
    一致性），保证预测与决策不基于非法状态。

    与规则（rules）分界：rules 是「该做什么」的**规范**（道义模态）；
    constraints 是「什么合法」的**结构约束**（SHACL 式，供数据校验）。

    ``constraint_id`` 由 (subject, type, description) 内容寻址；**不入图**，
    作为 ExtractionResult.constraints 独立产出保留，build_kg 登记 constraint_count。
    """
    constraint_id: str
    doc_id: str
    subject: str
    type: "ConstraintType"
    description: str
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Permission:
    """主体-动作授权（第六件套 permissions）。

    预测决策本体 v6：RBAC 授权边界——谁能（不能）执行某动作，是决策自动化
    的治理层（对齐行业四层闭环 data/logic/action/security 的 security）。

    与规则（rules）分界：rules 的 PROHIBITION 是**业务禁令**（不得销售不
    匹配产品），permissions 是**主体-动作授权边界**（仅特定角色可执行）。

    ``permission_id`` 由 (actor, action, effect) 内容寻址；**不入图**，作为
    ExtractionResult.permissions 独立产出保留，build_kg 登记 permission_count。
    """
    permission_id: str
    doc_id: str
    actor: str
    action: str
    effect: "PermissionEffect"
    scope: str = ""
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    confidence: float = 1.0
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # v7 借鉴（M5 主体模型）：角色中间层（权限经角色授予，RBAC 双层）
    # 与主体类型（人工/系统自动）。
    role: str = ""
    actor_type: "ActorType" = ActorType.OTHER
    # 2026-09-20 借鉴（v9 M5 Permission.dataScope）：数据可见范围
    # （全机构/本人/本部门/自定义）——金融数据隔离场景。不参与寻址。
    data_scope: "DataScope" = DataScope.OTHER


@dataclass(frozen=True)
class Degradation:
    """一次显式的能力降级声明。

    原版的做法是：spaCy 模型没装 → 静默退回正则 → 用户以为正常跑了，
    图谱里却少了一半实体。这里强制把降级写进结果，且要求写清 `impact`，
    让调用方能判断"这次的结果能不能用"。
    """
    component: str                # "ner.spacy" / "relation.llm" / "parse.pdf"
    kind: DegradationKind
    reason: str
    fallback: str
    impact: str                   # 人话描述对结果的影响
    recoverable: bool = True      # 装依赖/配 key 后能否恢复


@dataclass
class ExtractionResult:
    """Extract 的产物：一份文档抽出的全部结构化知识。

    ``rules``（业务规则 v3 新增）：条件-动作/结论的规范性知识，独立产出，
    不进入实体-边图谱（build_kg 仅在 metadata 登记 ``rule_count``）。
    ``processes``（业务流程 v4 新增）：有序活动 + 控制流，独立产出，不入
    实体-边图谱（build_kg 仅在 metadata 登记 ``process_count``）。
    ``states/functions/temporal/actions/constraints/permissions``（预测决策
    本体 v6 新增）：状态机 / 函数指标 / 时序时态 / 动作处置 / 约束 / 授权，
    六件套均为独立产出，不进入实体-边图谱（build_kg 在 metadata 登记各自
    count）。relations 新增 ``prop_kind``/``strength`` 传导增强（透传到边属性）。
    """
    doc_id: str
    chunks: list[Chunk] = field(default_factory=list)
    mentions: list[EntityMention] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    triplets: list[Triplet] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    # v6 预测决策本体：六件套独立产出（不入实体-边图）
    states: list[StateMachine] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    temporal: list[Temporal] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    permissions: list[Permission] = field(default_factory=list)
    degradations: list[Degradation] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    # v7 借鉴（manifest / 模型头）：文档级元数据头 {domain, source, version, doc_type}
    document_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_degraded(self) -> bool:
        return bool(self.degradations)


# ===========================================================================
# 7. Step 5 · Build KG
# ===========================================================================

@dataclass
class Entity:
    """图谱节点。

    `entity_id` 由 `(type, canonical_name)` 确定性生成（`ids.entity_id`），
    带来三个直接好处：
      1. 同批数据重复 build → ID 不变 → 天然幂等
      2. 不同文档抽到同一实体 → 自动收敛为同一节点
      3. 消歧/合并 = 让若干实体共享一个 canonical_name，然后按 ID 归并
    """
    entity_id: str
    canonical_name: str
    entity_type: EntityType
    aliases: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    mention_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    provenance: Provenance | None = None
    #: 实体的存在区间（如某公司 2020-2023 存续）。
    #: `None` = 无时间限制，视为恒真。
    #: 注意：实体**身份**不随时间变化，`entity_id` 不含时间维度；
    #: 这里描述的是实体本身的存在期，属性变化请用 properties 自行承载。
    temporal: BiTemporal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def merge_with(self, other: "Entity", strategy: MergeStrategy) -> "Entity":
        """合并两个被判定为同一实体的节点。

        注意：**被合并方的 canonical_name 进 aliases**，绝不丢弃。
        原版的 `entity_merger` 在合并时会丢掉别名，导致"用旧名字查不到"。
        """
        seen_aliases = {self.canonical_name.casefold(), *{a.casefold() for a in self.aliases}}
        for name in (other.canonical_name, *other.aliases):
            if name.casefold() not in seen_aliases:
                self.aliases.append(name)
                seen_aliases.add(name.casefold())

        self.mention_ids.extend(m for m in other.mention_ids if m not in self.mention_ids)

        # 属性合并：按策略裁决冲突
        for k, v in other.properties.items():
            if k not in self.properties or self.properties[k] is None:
                self.properties[k] = v
            elif self.properties[k] != v:
                self.properties[k] = self._resolve_prop_conflict(
                    k, self.properties[k], v, strategy
                )

        if self.provenance and other.provenance:
            self.provenance = self.provenance.merge(other.provenance)
        self.confidence = max(self.confidence, other.confidence)
        return self

    @staticmethod
    def _resolve_prop_conflict(
        key: str, mine: Any, theirs: Any, strategy: MergeStrategy
    ) -> Any:
        if strategy is MergeStrategy.KEEP_FIRST:
            return mine
        if strategy is MergeStrategy.KEEP_LONGEST:
            return max((mine, theirs), key=lambda v: len(str(v)))
        if strategy is MergeStrategy.KEEP_MOST_COMPLETE:
            # 非空、非 None、信息量更大的胜出
            if mine in (None, "", [], {}):
                return theirs
            if theirs in (None, "", [], {}):
                return mine
            return max((mine, theirs), key=lambda v: len(str(v)))
        if strategy is MergeStrategy.UNION_PROPERTIES:
            return mine if mine == theirs else [mine, theirs]
        return mine


@dataclass
class KGEdge:
    """图谱边。

    `object_id` 与 `object_literal` 互斥填充：
      * `object_id` 非空 → 指向实体节点的边
      * `object_literal` 非空 → 属性边（值直接挂在边上，也可下沉为节点属性）
    """
    edge_id: str
    subject_id: str
    predicate: str
    object_id: str | None = None
    object_literal: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    provenance: Provenance | None = None
    #: 事实的双时态标记。`None` = 无时间限制，视为恒真。
    #: 注意：`edge_id` 不含时间维度（身份稳定），时间只描述这条事实的
    #: 成立区间与录入区间；不同版本靠 `KnowledgeGraph.edges[edge_id]` 列表承载。
    temporal: BiTemporal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.object_id is None and self.object_literal is None:
            raise ContractViolation(
                f"KGEdge {self.edge_id}: object_id 与 object_literal 不可同时为空"
            )

    @property
    def is_current(self) -> bool:
        """是否为当前版本（尚未被双时态作废）。无时间标记时恒为 True。"""
        return self.temporal is None or self.temporal.is_current


@dataclass
class GraphStats:
    """建图统计。**统计是契约的一部分**，不是可选日志 —— 没有统计就无法
    判断一次构建是否正常（节点数突然腰斩 = 上游挂了）。"""
    entity_count: int = 0
    edge_count: int = 0
    literal_edge_count: int = 0
    mention_count: int = 0
    orphan_entity_count: int = 0      # 孤立节点，通常是抽取失败的信号
    by_type: dict[str, int] = field(default_factory=dict)
    by_predicate: dict[str, int] = field(default_factory=dict)
    build_ms: int = 0


@dataclass
class KnowledgeGraph:
    """图谱容器。

    两个 dict 的**值类型不对称**，这是刻意的：

      * `entities: dict[id, Entity]` —— 实体是**身份**，只有一个版本。
        实体的属性会变（用 properties 承载），但"张三"这个身份只有一个。
      * `edges: dict[id, list[KGEdge]]` —— 边是**事实**，可以有多个
        时间版本。同一条 (张三, 任职于, A公司) 在不同 valid_time 下是
        不同的事实版本，必须都留着，否则双时态查询无从谈起。

    用 dict 而非 list 承载，是因为 `entity_id`/`edge_id` 的内容寻址特性
    让"合并"退化成一次 dict 键碰撞 —— 这是把 ID 设计成内容寻址后
    自然获得的能力。
    """
    entities: dict[str, Entity] = field(default_factory=dict)
    edges: dict[str, list[KGEdge]] = field(default_factory=dict)
    stats: GraphStats = field(default_factory=GraphStats)
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- 写入 ---------------------------------------------------------------
    def add_entity(self, e: Entity) -> Entity:
        """幂等写入：已存在则按策略合并，返回合并后的实体。"""
        existing = self.entities.get(e.entity_id)
        if existing is None:
            self.entities[e.entity_id] = e
            return e
        return existing.merge_with(e, MergeStrategy.KEEP_MOST_COMPLETE)

    def add_edge(self, e: KGEdge) -> KGEdge:
        """写入一条事实。

        幂等规则与单时态不同 —— **相同时间版本才合并，不同版本则追加**：

          * `temporal` 与某个已有版本相同 → 视为同一事实的重复印证，
            合并置信度（取最大）与来源，**不产生新版本**
          * `temporal` 不同 → 追加为新版本，保留历史

        这条规则是双时态能成立的基础：它让"重复 build 同一批数据"仍然是
        幂等的（因为 temporal 相同），同时"事实随时间演进"能留下多个版本。
        """
        versions = self.edges.setdefault(e.edge_id, [])
        for existing in versions:
            if _same_temporal(existing.temporal, e.temporal):
                existing.confidence = max(existing.confidence, e.confidence)
                if existing.provenance and e.provenance:
                    existing.provenance = existing.provenance.merge(e.provenance)
                elif e.provenance:
                    existing.provenance = e.provenance
                return existing
        versions.append(e)
        return e

    def supersede(self, edge_id: str, new_edge: KGEdge, at: datetime) -> KGEdge:
        """用新版本取代当前版本。

        把 `edge_id` 的当前版本作废（记录 `invalidated_at=at`），
        再追加新版本。这是**修正**而非**覆盖** —— 旧版本仍可经
        `at()` 在历史时点查到。
        """
        for old in self.edges.get(edge_id, []):
            if old.is_current and old.temporal is not None:
                old.temporal = old.temporal.invalidate(at)
        return self.add_edge(new_edge)

    # -- 读取 ---------------------------------------------------------------
    def all_edges(self) -> list[KGEdge]:
        """全部事实的全部版本（含历史）。"""
        return [e for versions in self.edges.values() for e in versions]

    def current_edges(self) -> list[KGEdge]:
        """当前视图：每个事实只取尚未作废（``is_current``）的版本。

        严格按双时态语义：某事实若被 QA 裁决作废（落选边）或被
        ``supersede`` 取代，其旧版本不在当前视图中 —— 历史仍保留在
        ``all_edges()`` 里，但「现在采信什么」只由当前版本决定。因此这里
        不会把已作废的版本兜底放回当前视图（否则作废就失去了意义）。
        """
        out: list[KGEdge] = []
        for versions in self.edges.values():
            out.extend(e for e in versions if e.is_current)
        return out

    def at(
        self,
        valid_time: datetime,
        tx_time: datetime | None = None,
    ) -> "KnowledgeGraph":
        """双时态快照：在给定时刻，图谱长什么样。

        Args:
            valid_time: 想看"现实世界哪个时刻"的状态。
            tx_time: 想看"系统在哪时刻的认知"。默认与 valid_time 相同，
                     即"当时的认知"；传 `utcnow()` 则是"以现在的认知回看
                     当时"，二者的区别正是双时态的价值所在。

        Returns:
            一个新的 KnowledgeGraph，只含在该时点成立且已被采信的内容。
        """
        tx = tx_time or valid_time
        g = KnowledgeGraph(metadata={
            **self.metadata,
            "snapshot_valid_time": valid_time.isoformat(),
            "snapshot_tx_time": tx.isoformat(),
        })

        for e in self.entities.values():
            if e.temporal is None or (
                e.temporal.valid_at(valid_time) and e.temporal.known_at(tx)
            ):
                g.entities[e.entity_id] = e

        for edge_id, versions in self.edges.items():
            hits = [
                e for e in versions
                if e.temporal is None
                or (e.temporal.valid_at(valid_time) and e.temporal.known_at(tx))
            ]
            if hits:
                # 同一事实在该时点可能有多个已采信版本，取录入时间最晚的
                latest = max(
                    hits,
                    key=lambda e: e.temporal.recorded_at if (
                        e.temporal and e.temporal.recorded_at
                    ) else datetime.min.replace(tzinfo=timezone.utc),
                )
                # 快照内该版本即「当前」：清除全局事务作废标记，否则
                # current_edges() 会因为这些版本在「现在」已被作废而误删它们。
                # 用 replace 生成新对象，绝不改动原图（避免 aliasing 污染）。
                if latest.temporal is not None and latest.temporal.invalidated_at is not None:
                    latest = replace(
                        latest,
                        temporal=replace(
                            latest.temporal,
                            invalidated_at=None,
                        ),
                    )
                g.edges[edge_id] = [latest]
        return g

    def neighbors(self, entity_id: str) -> list[KGEdge]:
        return [
            e for e in self.current_edges()
            if e.subject_id == entity_id or e.object_id == entity_id
        ]

    def out_edges(self, entity_id: str) -> list[KGEdge]:
        return [e for e in self.current_edges() if e.subject_id == entity_id]

    def by_type(self, t: EntityType) -> list[Entity]:
        return [e for e in self.entities.values() if e.entity_type is t]

    def recompute_stats(self, build_ms: int = 0) -> GraphStats:
        """统计基于**当前视图**，不含历史版本 —— 否则数字会虚高。"""
        by_type: dict[str, int] = {}
        for e in self.entities.values():
            by_type[e.entity_type.value] = by_type.get(e.entity_type.value, 0) + 1
        by_pred: dict[str, int] = {}
        lit = 0
        linked: set[str] = set()
        edges = self.current_edges()
        for e in edges:
            by_pred[e.predicate] = by_pred.get(e.predicate, 0) + 1
            if e.object_literal is not None:
                lit += 1
            if e.object_id:
                linked.add(e.object_id)
            linked.add(e.subject_id)
        self.stats = GraphStats(
            entity_count=len(self.entities),
            edge_count=len(edges),
            literal_edge_count=lit,
            orphan_entity_count=sum(
                1 for eid in self.entities if eid not in linked
            ),
            by_type=by_type,
            by_predicate=by_pred,
            build_ms=build_ms,
        )
        return self.stats


# ===========================================================================
# 8. Step 6 · QA（冲突检测 + 去重）
# ===========================================================================

@dataclass(frozen=True)
class ConflictValue:
    """冲突中的一个候选值，带完整出处。"""
    value: Any
    source_refs: tuple[SourceRef, ...] = ()
    confidence: float = 1.0
    fetched_at: datetime | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None


@dataclass
class Conflict:
    """同一 (subject, predicate) 上出现互斥/不一致取值。

    `conflict_id` 只由 (subject, predicate) 决定 —— 所有候选值汇聚成
    **一个**冲突对象，而不是每个值一个，便于整体裁决。
    """
    conflict_id: str
    kind: ConflictKind
    subject_id: str
    predicate: str
    values: list[ConflictValue] = field(default_factory=list)
    severity: float = 0.0            # 0..1，越高越需要人工介入
    resolution: "Resolution | None" = None

    @property
    def resolved(self) -> bool:
        return self.resolution is not None


@dataclass(frozen=True)
class Resolution:
    """冲突裁决结果。**必须带 rationale** —— 无法解释的自动裁决不可接受。"""
    strategy: ResolutionStrategy
    chosen: Any
    rationale: str
    discarded: tuple[Any, ...] = ()
    needs_review: bool = False       # True 时进人工队列，不写回图


@dataclass
class DuplicateCluster:
    """一组被判定为同一实体的节点。"""
    cluster_id: str
    entity_ids: list[str] = field(default_factory=list)
    canonical_id: str = ""
    pairwise_scores: dict[tuple[str, str], float] = field(default_factory=dict)
    merged_properties: dict[str, Any] = field(default_factory=dict)
    strategy: MergeStrategy = MergeStrategy.KEEP_MOST_COMPLETE


@dataclass
class QualityMetrics:
    conflict_count: int = 0
    unresolved_count: int = 0
    duplicate_cluster_count: int = 0
    entities_merged: int = 0
    edges_remapped: int = 0
    mean_confidence: float = 0.0
    qa_ms: int = 0


@dataclass
class QAResult:
    """QA 的产物。

    **关键契约：QA 必须在 Store 之前跑完。**

    semantica 原版 `graph_builder.py` 的顺序是先 `add_nodes`(:818) /
    `add_edges`(:835) 落库，再 `detect_conflicts`(:844) —— 落库的是未消歧
    数据，冲突清单还不落盘。本实现把 QA 显式插在 Build 与 Store 之间，
    且 `graph` 字段返回的是**已修复的图**，从类型上就不可能再犯这个错。
    """
    graph: KnowledgeGraph
    conflicts: list[Conflict] = field(default_factory=list)
    duplicate_clusters: list[DuplicateCluster] = field(default_factory=list)
    metrics: QualityMetrics = field(default_factory=QualityMetrics)

    @property
    def passed(self) -> bool:
        """是否所有冲突都已裁决。Store 步应当拒绝 `passed=False` 的图。"""
        return self.metrics.unresolved_count == 0


# ===========================================================================
# 9. Step 7 · Store
# ===========================================================================

@dataclass(frozen=True)
class VectorRecord:
    """一条待写入向量库的记录。

    `payload` 里必须带 `entity_id`，否则检索结果无法回指图谱 —— 向量库
    和图谱的连接点就在这里，缺了它 RAG 只能返回无出处的文本。
    """
    vector_id: str
    vector: tuple[float, ...]
    text: str
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.vector:
            raise ContractViolation("VectorRecord.vector 不可为空")


@dataclass(frozen=True)
class SearchHit:
    vector_id: str
    score: float
    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    rank: int = 0
    source: str = ""     # "vector" | "graph" | "hybrid"


@dataclass(frozen=True)
class StoreReceipt:
    """写入回执。**强制 upsert 语义**。

    原版 Neo4j store 全用 `CREATE`（`MERGE` 零命中），重复构建会重复插入。
    这里要求所有 backend 实现 upsert，`upserted_count` 必须返回，
    让调用方能验证幂等性。
    """
    backend: str
    entities_written: int = 0
    entities_updated: int = 0
    edges_written: int = 0
    edges_updated: int = 0
    vectors_written: int = 0
    elapsed_ms: int = 0
    idempotent: bool = True       # backend 声明是否幂等；False 时编排层告警


# ===========================================================================
# 10. Step 8 · Deliver
# ===========================================================================

@dataclass(frozen=True)
class Citation:
    """一条交付事实的出处引用 —— Deliver 的可信度全靠它。

    原版的 provenance 只到"来自哪个文档"，这里要求到**字符区间**，
    并且给出 `quoted_text` 原文片段，让使用方（人或 Agent）能立刻校验。
    """
    source_ref: SourceRef
    char_span: tuple[int, int] | None = None   # orig 坐标系
    quoted_text: str = ""
    chunk_id: str | None = None
    extractor: str = ""
    confidence: float = 1.0


@dataclass
class ContextPackage:
    """交付给 Agent/应用的上下文包。

    设计取舍：检索与组装是确定性的；`answer` 字段可由 LLM 在确定性
    facts 之上做自然语言合成（可选，默认 None，不影响幂等）。原版 `ContextGraph` 的
    `find_similar_decisions` / `analyze_decision_impact` 属于推理范畴，
    其"潜在因果"是启发式（共现实体 + 时间更早），并非真推理 —— 精简版
    不复制这条路径，把它留给上层应用自己决定。
    """
    query: str
    facts: list[Triplet] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    generated_at: datetime = field(default_factory=utcnow)
    retrieval: str = "hybrid"      # "vector" | "graph" | "hybrid"
    degradation_notes: list[str] = field(default_factory=list)
    answer: str | None = None     # 可选：LLM 基于 facts 合成的自然语言答案（确定性检索之上）

    def render(self, max_facts: int = 50, with_citations: bool = True) -> str:
        """渲染成可直接塞进 prompt 的文本。"""
        lines = [f"## 与「{self.query}」相关的事实", ""]
        citation_by_key = {
            (c.source_ref.source_id, c.char_span): c for c in self.citations
        }
        for t in self.facts[:max_facts]:
            obj = t.object
            line = f"- ({t.subject} —[{t.predicate}]→ {obj})"
            if with_citations and t.provenance and t.provenance.primary_source:
                src = t.provenance.primary_source
                line += f"  [来源: {src.uri}]"
            lines.append(line)
        if len(self.facts) > max_facts:
            lines.append(f"... 另有 {len(self.facts) - max_facts} 条")
        return "\n".join(lines)


# ===========================================================================
# 11. 错误体系
# ===========================================================================

class SminiError(Exception):
    """所有 smini 异常的基类。"""


class ContractViolation(SminiError):
    """数据结构不变量被破坏。

    这是**编程错误**，不是运行时异常 —— 出现即说明某个 step 写错了，
    应当让流水线立刻崩掉，而不是记日志继续跑。
    """


class StepError(SminiError):
    """某个 step 执行失败。"""

    def __init__(self, step: str, message: str, *, cause: Exception | None = None):
        self.step = step
        self.cause = cause
        super().__init__(f"[{step}] {message}")


class DependencyMissing(SminiError):
    """所需的可选依赖/凭据缺失。

    与 semantica 静默降级不同：这里**抛出**而不是吞掉。由调用方显式
    决定是降级（登记 Degradation）还是失败。默认应当失败 —— 静默降级
    是知识图谱质量事故的头号来源。
    """


class DimensionMismatch(SminiError):
    """向量维度与索引维度不匹配。

    原版 `vector_store.py:303-307` 在 embedding 失败时返回**随机向量**，
    只打一条 WARNING。结果是索引可检索但语义完全无意义，且极难排查。
    这里直接抛错。
    """


# ===========================================================================
# 12. 序列化
# ===========================================================================

def to_dict(obj: Any) -> Any:
    """把 dataclass / Enum / datetime / bytes 递归转成 JSON 友好结构。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return {"__bytes__": base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        # tuple key（如 pairwise_scores）转成字符串，JSON 不支持非字符串键
        return {
            (f"{k[0]}|{k[1]}" if isinstance(k, tuple) else k): to_dict(v)
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [to_dict(v) for v in obj]
    return obj


def iter_documents(container: Any) -> Iterable[Any]:
    """从任意层级的容器里迭代出文档对象，供编排层做类型分发。"""
    if is_dataclass(container) and not isinstance(container, type):
        yield container
    elif isinstance(container, dict):
        for v in container.values():
            yield from iter_documents(v)
    elif isinstance(container, (list, tuple)):
        for v in container:
            yield from iter_documents(v)
