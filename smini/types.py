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

    def __str__(self) -> str:
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
    TEXT = "text"


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
    IMAGE = "image"
    AUDIO = "audio"
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
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"
    STRING = "STRING"
    # v7 借鉴（M1 对象模型）：字典引用 / 实体引用
    DICT_REF = "DICT_REF"
    ENTITY_REF = "ENTITY_REF"
    OTHER = "OTHER"


class RuleModality(_StrEnum):
    """业务规则模态（独立于实体类型 / 属性值类型，只描述**规则**的规范性）。"""
    OBLIGATION = "OBLIGATION"
    PROHIBITION = "PROHIBITION"
    PERMISSION = "PERMISSION"
    CONDITIONAL = "CONDITIONAL"
    OTHER = "OTHER"


class RuleType(_StrEnum):
    """业务规则用途类型（v7 借鉴 M3 规则模型 ruleType，与 modality 正交并存）。"""
    VALIDATION = "VALIDATION"
    DERIVATION = "DERIVATION"
    TRIGGER = "TRIGGER"
    OTHER = "OTHER"


class Certainty(_StrEnum):
    """业务内容确定性分级（v7 借鉴对方需求探索 A/B 类分级思想）。"""
    GENERAL = "GENERAL"
    ENTERPRISE = "ENTERPRISE"
    OTHER = "OTHER"


class ActorType(_StrEnum):
    """主体类型（v7 借鉴 M5 主体模型 actorType：HUMAN/SYSTEM）。"""
    HUMAN = "HUMAN"
    SYSTEM = "SYSTEM"
    OTHER = "OTHER"


class StepKind(_StrEnum):
    """流程步骤类型（独立于实体/属性/规则枚举，只描述**流程步骤**的角色）。"""
    TASK = "TASK"
    GATEWAY = "GATEWAY"
    EVENT = "EVENT"
    # v7 借鉴（M6 流程模型）：步骤角色细分
    START = "START"
    END = "END"
    SYSTEM_TASK = "SYSTEM_TASK"


class FlowType(_StrEnum):
    """控制流类型（独立枚举，只描述**流程步骤之间的连接**）。"""
    SEQUENCE = "SEQUENCE"
    PARALLEL = "PARALLEL"
    CONDITIONAL = "CONDITIONAL"
    LOOP = "LOOP"


class FunctionOutputType(_StrEnum):
    """函数指标输出类型（独立枚举，只描述**函数指标的取值形态**）。"""
    NUMBER = "NUMBER"
    PERCENT = "PERCENT"
    RANK = "RANK"
    BOOLEAN = "BOOLEAN"
    ENUM = "ENUM"
    STRING = "STRING"
    OTHER = "OTHER"


class TemporalKind(_StrEnum):
    """时态标注类型（独立枚举，只描述**时间语义如何绑定到主体**）。"""
    VALIDITY = "VALIDITY"
    FREQUENCY = "FREQUENCY"
    WINDOW = "WINDOW"
    TRIGGER = "TRIGGER"


class ActionLevel(_StrEnum):
    """动作处置级别（独立 3 类枚举，对齐文章预测输出五项之「建议动作级别」）。"""
    OBSERVE = "OBSERVE"
    WARN = "WARN"
    INTERVENE = "INTERVENE"


class ConstraintType(_StrEnum):
    """约束类型（独立 6 类枚举，对齐 SHACL 约束组件）。"""
    CARDINALITY = "CARDINALITY"
    VALUE_RANGE = "VALUE_RANGE"
    ENUM = "ENUM"
    DISJOINT = "DISJOINT"
    REQUIRED = "REQUIRED"
    CONSISTENCY = "CONSISTENCY"


class PermissionEffect(_StrEnum):
    """权限效果（独立 2 类枚举，RBAC 授权语义）。"""
    PERMIT = "PERMIT"
    DENY = "DENY"


class RelationPropKind(_StrEnum):
    """关系传导类型（独立 4 类枚举，增强 relations 的传导语义）。"""
    FACTUAL = "FACTUAL"
    DEPENDENCY = "DEPENDENCY"
    CAUSAL = "CAUSAL"
    TRIGGER = "TRIGGER"


class ConflictKind(_StrEnum):
    """原版 `conflicts/conflict_detector.py:60-95` 的 5 类，保留并显式化。"""
    VALUE = "value"
    TYPE = "type"
    TEMPORAL = "temporal"
    MUTEX = "mutex"
    LOGIC = "logic"


class ResolutionStrategy(_StrEnum):
    MOST_RECENT = "most_recent"
    HIGHEST_CONFIDENCE = "highest_confidence"
    MAJORITY_VOTE = "majority_vote"
    SOURCE_PRIORITY = "source_priority"
    KEEP_ALL = "keep_all"
    MANUAL = "manual"


class MergeStrategy(_StrEnum):
    KEEP_MOST_COMPLETE = "keep_most_complete"
    KEEP_FIRST = "keep_first"
    KEEP_LONGEST = "keep_longest"
    UNION_PROPERTIES = "union_properties"


class DegradationKind(_StrEnum):
    """能力降级的原因分类。用于显式告知下游"这次跑得不全"。"""
    MODEL_MISSING = "model_missing"
    UNAVAILABLE = "unavailable"
    NO_CREDENTIAL = "no_credential"
    RATE_LIMITED = "rate_limited"
    PARSE_FAILED = "parse_failed"
    UNSUPPORTED_FORMAT = "unsupported_format"
    TRUNCATED = "truncated"


# ===========================================================================
# 2. 溯源（贯穿全链路）
# ===========================================================================

@dataclass(frozen=True)
class SourceRef:
    """指向一个数据来源的不可变引用。"""
    source_id: str
    uri: str
    source_type: SourceType
    checksum: str | None = None
    fetched_at: datetime | None = None
    locator: str | None = None
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
    """挂在实体/关系/事实上的溯源信息。"""
    source_refs: tuple[SourceRef, ...] = ()
    extractor: str = ""
    extracted_at: datetime | None = None
    confidence: float = 1.0
    char_span: tuple[int, int] | None = None
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
    """一个时间区间，用于表达 valid_time。"""
    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ContractViolation(
                f"TimeInterval 端点倒置：start={self.start} > end={self.end}"
            )

    def contains(self, t: datetime) -> bool:
        if self.start is not None and t < self.start:
            return False
        if self.end is not None and t >= self.end:
            return False
        return True

    def overlaps(self, other: "TimeInterval") -> bool:
        if self.end is not None and other.start is not None and self.end <= other.start:
            return False
        if other.end is not None and self.start is not None and other.end <= self.start:
            return False
        return True

    @property
    def is_open_ended(self) -> bool:
        return self.end is None

    @classmethod
    def since(cls, start: datetime) -> "TimeInterval":
        return cls(start=start, end=None)

    @classmethod
    def until(cls, end: datetime) -> "TimeInterval":
        return cls(start=None, end=end)


@dataclass(frozen=True)
class BiTemporal:
    """双时态标记：事实**何时为真** + **何时被录入系统**。"""
    valid: TimeInterval = field(default_factory=TimeInterval)
    recorded_at: datetime | None = None
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
        return self.invalidated_at is None

    def valid_at(self, t: datetime) -> bool:
        return self.valid.contains(t)

    def known_at(self, t: datetime) -> bool:
        if self.recorded_at is not None and t < self.recorded_at:
            return False
        if self.invalidated_at is not None and t >= self.invalidated_at:
            return False
        return True

    def invalidate(self, at: datetime) -> "BiTemporal":
        return BiTemporal(
            valid=self.valid,
            recorded_at=self.recorded_at,
            invalidated_at=at,
        )


def _same_temporal(a: BiTemporal | None, b: BiTemporal | None) -> bool:
    """两个双时态标记是否指向同一个版本。"""
    return a == b


# ===========================================================================
# 3. Step 1 · Ingest
# ===========================================================================

@dataclass
class RawDocument:
    """Ingest 的唯一产物：**原样搬运，不做任何解读**。"""
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
    """文档内的一个结构化区块。"""
    block_id: str
    kind: BlockKind
    text: str
    order: int
    char_start: int
    char_end: int
    level: int | None = None
    rows: list[list[str]] | None = None
    language: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.char_end < self.char_start:
            raise ContractViolation(
                f"Block.char_end({self.char_end}) < char_start({self.char_start})"
            )


@dataclass
class ParsedDocument:
    """Parse 的产物：把一种格式变成**统一的 文本 + 区块序列**。"""
    doc_id: str
    format: DocumentFormat
    source: SourceRef
    text: str = ""
    blocks: list[Block] = field(default_factory=list)
    parser: str = ""
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
    """记录一次归一化替换造成的坐标漂移。"""
    orig_start: int
    orig_end: int
    new_start: int
    new_end: int
    kind: str
    original: str = ""
    replacement: str = ""

    @property
    def delta(self) -> int:
        return (self.new_end - self.new_start) - (self.orig_end - self.orig_start)


def map_offset_back(
    offset: int,
    patches: Sequence[SpanPatch],
    *,
    at_end: bool = False,
) -> int:
    """把 norm 坐标系的偏移映射回 orig 坐标系。"""
    if not patches:
        return offset

    result = offset
    for p in patches:
        if p.new_end <= offset:
            result = offset + (p.orig_end - p.new_end)
        elif p.new_start <= offset < p.new_end:
            result = p.orig_end if at_end else p.orig_start
            break
        else:
            break
    return max(result, 0)


def validate_patches(patches: Sequence[SpanPatch]) -> None:
    """校验 SpanPatch 序列自洽。"""
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
    """Normalize 的产物。"""
    doc_id: str
    source: SourceRef
    text: str = ""
    blocks: list[Block] = field(default_factory=list)
    patches: list[SpanPatch] = field(default_factory=list)
    mentions: list["EntityMention"] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_original_span(self, span: tuple[int, int]) -> tuple[int, int]:
        return map_span_back(span, self.patches)

    def original_text(self, parsed: ParsedDocument, span: tuple[int, int]) -> str:
        o_s, o_e = self.to_original_span(span)
        return parsed.text[o_s:o_e]


# ===========================================================================
# 6. Step 4 · Extract（含 Split）
# ===========================================================================

@dataclass(frozen=True)
class Chunk:
    """切分后的文本块。"""
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
    """实体在一次**具体出现**中的记录（尚未消歧）。"""
    mention_id: str
    doc_id: str
    text: str
    normalized: str
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
    """两个 mention 之间的语义关系。"""
    relation_id: str
    doc_id: str
    subject_ref: str
    object_ref: str
    predicate: str
    confidence: float = 1.0
    chunk_id: str | None = None
    evidence_span: tuple[int, int] | None = None
    evidence_text: str | None = None
    extractor: str = ""
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    prop_kind: str = ""
    strength: str = ""


@dataclass
class Triplet:
    """(主语, 谓词, 宾语) —— 图谱的原子事实单元。"""
    triplet_id: str
    subject: str
    subject_type: EntityType
    predicate: str
    object: str
    object_type: EntityType | None = None
    object_literal: bool = False
    value_type: "AttributeValueType | None" = None
    required: bool = False
    prop_kind: str = ""
    strength: str = ""
    confidence: float = 1.0
    source_mentions: tuple[str, ...] = ()
    chunk_id: str | None = None
    extractor: str = ""
    provenance: Provenance | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Rule:
    """业务规则：条件-动作/结论（规范/派生知识），契约即规则第四件套。"""
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
    rule_type: "RuleType" = RuleType.OTHER
    output_type: str = ""
    reused_by: list[str] = field(default_factory=list)
    certainty: "Certainty" = Certainty.OTHER


@dataclass
class ProcessStep:
    """流程步骤（第五件套 processes 的元素）。"""
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
    sub_process_ref: str = ""


@dataclass
class ProcessFlow:
    """流程控制流（第五件套 processes 的元素）。"""
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


@dataclass
class Process:
    """业务流程：有序活动 + 控制流（第五件套）。"""
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
    preconditions: list[str] = field(default_factory=list)
    postconditions: list[str] = field(default_factory=list)


# ===========================================================================
# 6.5 Step 4 · 预测决策本体抽取（v6）
# ===========================================================================

@dataclass
class StateDef:
    """状态机的一个状态（第六件套 states 的元素）。"""
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
    """状态机的一条状态迁移（第六件套 states 的元素）。"""
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
    """对象状态机（第六件套 states）。"""
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
    """函数指标（第六件套 functions）。"""
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
    """时序时态标注（第六件套 temporal）。"""
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
    """动作处置（第六件套 actions）。"""
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
    precondition: str = ""
    postcondition: str = ""


@dataclass
class Constraint:
    """结构约束（第六件套 constraints）。"""
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
    """主体-动作授权（第六件套 permissions）。"""
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
    role: str = ""
    actor_type: "ActorType" = ActorType.OTHER


@dataclass(frozen=True)
class Degradation:
    """一次显式的能力降级声明。"""
    component: str
    kind: DegradationKind
    reason: str
    fallback: str
    impact: str
    recoverable: bool = True


@dataclass
class ExtractionResult:
    """Extract 的产物：一份文档抽出的全部结构化知识。"""
    doc_id: str
    chunks: list[Chunk] = field(default_factory=list)
    mentions: list[EntityMention] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
    triplets: list[Triplet] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    processes: list[Process] = field(default_factory=list)
    states: list[StateMachine] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    temporal: list[Temporal] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    constraints: list[Constraint] = field(default_factory=list)
    permissions: list[Permission] = field(default_factory=list)
    degradations: list[Degradation] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    document_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_degraded(self) -> bool:
        return bool(self.degradations)


# ===========================================================================
# 7. Step 5 · Build KG
# ===========================================================================

@dataclass
class Entity:
    """图谱节点。"""
    entity_id: str
    canonical_name: str
    entity_type: EntityType
    aliases: list[str] = field(default_factory=list)
    properties: dict[str, Any] = field(default_factory=dict)
    mention_ids: list[str] = field(default_factory=list)
    confidence: float = 1.0
    provenance: Provenance | None = None
    temporal: BiTemporal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def merge_with(self, other: "Entity", strategy: MergeStrategy) -> "Entity":
        seen_aliases = {self.canonical_name.casefold(), *{a.casefold() for a in self.aliases}}
        for name in (other.canonical_name, *other.aliases):
            if name.casefold() not in seen_aliases:
                self.aliases.append(name)
                seen_aliases.add(name.casefold())

        self.mention_ids.extend(m for m in other.mention_ids if m not in self.mention_ids)

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
    """图谱边。"""
    edge_id: str
    subject_id: str
    predicate: str
    object_id: str | None = None
    object_literal: str | None = None
    properties: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    provenance: Provenance | None = None
    temporal: BiTemporal | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.object_id is None and self.object_literal is None:
            raise ContractViolation(
                f"KGEdge {self.edge_id}: object_id 与 object_literal 不可同时为空"
            )

    @property
    def is_current(self) -> bool:
        return self.temporal is None or self.temporal.is_current


@dataclass
class GraphStats:
    """建图统计。"""
    entity_count: int = 0
    edge_count: int = 0
    literal_edge_count: int = 0
    mention_count: int = 0
    orphan_entity_count: int = 0
    by_type: dict[str, int] = field(default_factory=dict)
    by_predicate: dict[str, int] = field(default_factory=dict)
    build_ms: int = 0


@dataclass
class KnowledgeGraph:
    """图谱容器。"""
    entities: dict[str, Entity] = field(default_factory=dict)
    edges: dict[str, list[KGEdge]] = field(default_factory=dict)
    stats: GraphStats = field(default_factory=GraphStats)
    metadata: dict[str, Any] = field(default_factory=dict)

    def add_entity(self, e: Entity) -> Entity:
        existing = self.entities.get(e.entity_id)
        if existing is None:
            self.entities[e.entity_id] = e
            return e
        return existing.merge_with(e, MergeStrategy.KEEP_MOST_COMPLETE)

    def add_edge(self, e: KGEdge) -> KGEdge:
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
        for old in self.edges.get(edge_id, []):
            if old.is_current and old.temporal is not None:
                old.temporal = old.temporal.invalidate(at)
        return self.add_edge(new_edge)

    def all_edges(self) -> list[KGEdge]:
        return [e for versions in self.edges.values() for e in versions]

    def current_edges(self) -> list[KGEdge]:
        out: list[KGEdge] = []
        for versions in self.edges.values():
            out.extend(e for e in versions if e.is_current)
        return out

    def at(
        self,
        valid_time: datetime,
        tx_time: datetime | None = None,
    ) -> "KnowledgeGraph":
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
                latest = max(
                    hits,
                    key=lambda e: e.temporal.recorded_at if (
                        e.temporal and e.temporal.recorded_at
                    ) else datetime.min.replace(tzinfo=timezone.utc),
                )
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
# 8. Step 6 · QA
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
    """同一 (subject, predicate) 上出现互斥/不一致取值。"""
    conflict_id: str
    kind: ConflictKind
    subject_id: str
    predicate: str
    values: list[ConflictValue] = field(default_factory=list)
    severity: float = 0.0
    resolution: "Resolution | None" = None

    @property
    def resolved(self) -> bool:
        return self.resolution is not None


@dataclass(frozen=True)
class Resolution:
    """冲突裁决结果。**必须带 rationale**。"""
    strategy: ResolutionStrategy
    chosen: Any
    rationale: str
    discarded: tuple[Any, ...] = ()
    needs_review: bool = False


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
    """QA 的产物。**关键契约：QA 必须在 Store 之前跑完。**"""
    graph: KnowledgeGraph
    conflicts: list[Conflict] = field(default_factory=list)
    duplicate_clusters: list[DuplicateCluster] = field(default_factory=list)
    metrics: QualityMetrics = field(default_factory=QualityMetrics)

    @property
    def passed(self) -> bool:
        return self.metrics.unresolved_count == 0


# ===========================================================================
# 9. Step 7 · Store
# ===========================================================================

@dataclass(frozen=True)
class VectorRecord:
    """一条待写入向量库的记录。"""
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
    source: str = ""


@dataclass(frozen=True)
class StoreReceipt:
    """写入回执。**强制 upsert 语义**。"""
    backend: str
    entities_written: int = 0
    entities_updated: int = 0
    edges_written: int = 0
    edges_updated: int = 0
    vectors_written: int = 0
    elapsed_ms: int = 0
    idempotent: bool = True


# ===========================================================================
# 10. Step 8 · Deliver
# ===========================================================================

@dataclass(frozen=True)
class Citation:
    """一条交付事实的出处引用。"""
    source_ref: SourceRef
    char_span: tuple[int, int] | None = None
    quoted_text: str = ""
    chunk_id: str | None = None
    extractor: str = ""
    confidence: float = 1.0


@dataclass
class ContextPackage:
    """交付给 Agent/应用的上下文包。"""
    query: str
    facts: list[Triplet] = field(default_factory=list)
    entities: list[Entity] = field(default_factory=list)
    citations: list[Citation] = field(default_factory=list)
    generated_at: datetime = field(default_factory=utcnow)
    retrieval: str = "hybrid"
    degradation_notes: list[str] = field(default_factory=list)
    answer: str | None = None

    def render(self, max_facts: int = 50, with_citations: bool = True) -> str:
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
    """数据结构不变量被破坏。"""


class StepError(SminiError):
    """某个 step 执行失败。"""

    def __init__(self, step: str, message: str, *, cause: Exception | None = None):
        self.step = step
        self.cause = cause
        super().__init__(f"[{step}] {message}")


class DependencyMissing(SminiError):
    """所需的可选依赖/凭据缺失。"""


class DimensionMismatch(SminiError):
    """向量维度与索引维度不匹配。"""


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
