"""smini — 本体抽取的确定性薄壳（extract-only）。

契约即规则（Rule-by-Contract）架构：语义抽取（实体-关系三元组、属性、
业务规则、业务流程、预测决策六件套）由**宿主 agent（大模型）**按约定
JSON 契约交卷；Python 只保留确定性薄壳——

    ingest（读源原语）→ extract（宿主契约抽取，★核心）→ build_kg（惰性锚点消费层）

可用 ``python -m smini.cli build ...`` 端到端运行；或由宿主 agent 交卷
契约文件（``--host-contract``）驱动薄壳映射。无任何 ``SMINI_LLM_*``
凭证依赖，不发起模型 HTTP 调用。
"""

from .ids import (
    action_id,
    chunk_id,
    constraint_id,
    content_hash,
    doc_id,
    edge_id,
    entity_id,
    flow_id,
    function_id,
    mention_id,
    permission_id,
    process_id,
    rule_id,
    stable_id,
    state_id,
    state_machine_id,
    step_id,
    temporal_id,
    transition_id,
    triplet_id,
)
from .pipeline_factory import build_pipeline, make_context
from .protocols import (
    DEFAULT_STEP_ORDER,
    Embedder,
    GraphStore,
    LLMProvider,
    Pipeline,
    PipelineState,
    PipelineStep,
    RunContext,
    VectorStore,
    rrf_fuse,
    run_pipeline,
    validate_step_order,
)
from .llm import (
    EXTRACT_SCHEMA,
    HostAgentLLMProvider,
    StubLLMProvider,
    build_extract_prompt,
    default_llm,
)
from .steps import (
    BuildKGStep,
    ExtractStep,
)
from .types import (
    AttributeValueType,
    Block,
    BlockKind,
    Chunk,
    Citation,
    Conflict,
    ConflictKind,
    ConflictValue,
    ContextPackage,
    ContractViolation,
    Degradation,
    DegradationKind,
    DependencyMissing,
    DimensionMismatch,
    DocumentFormat,
    DuplicateCluster,
    Entity,
    EntityMention,
    EntityType,
    ExtractionResult,
    GraphStats,
    KGEdge,
    KnowledgeGraph,
    MergeStrategy,
    NormalizedDocument,
    ParsedDocument,
    Provenance,
    QAResult,
    QualityMetrics,
    RawDocument,
    Relation,
    Resolution,
    ResolutionStrategy,
    Rule,
    RuleModality,
    RuleType,
    Certainty,
    ActorType,
    Process,
    ProcessStep,
    ProcessFlow,
    StepKind,
    FlowType,
    FunctionOutputType,
    TemporalKind,
    ActionLevel,
    ConstraintType,
    PermissionEffect,
    RelationPropKind,
    StateDef,
    TransitionDef,
    StateMachine,
    Function,
    Temporal,
    Action,
    Constraint,
    Permission,
    SearchHit,
    SminiError,
    SourceRef,
    SourceType,
    SpanPatch,
    StepError,
    StoreReceipt,
    TimeInterval,
    BiTemporal,
    Triplet,
    VectorRecord,
    map_offset_back,
    map_span_back,
    to_dict,
    validate_patches,
)

__version__ = "0.1.0"

__all__ = [
    # 编排
    "Pipeline", "PipelineState", "PipelineStep", "RunContext", "StepResult",
    "run_pipeline", "validate_step_order", "DEFAULT_STEP_ORDER",
    "build_pipeline", "make_context",
    "GraphStore", "VectorStore", "Embedder", "LLMProvider", "rrf_fuse",
    # 步骤
    "ExtractStep", "BuildKGStep",
    # 数据结构
    "RawDocument", "ParsedDocument", "NormalizedDocument", "SpanPatch",
    "ExtractionResult", "Chunk", "EntityMention", "Relation", "Triplet",
    "Rule", "Process", "ProcessStep", "ProcessFlow",
    "StateDef", "TransitionDef", "StateMachine",
    "Function", "Temporal", "Action", "Constraint", "Permission",
    "Degradation", "Entity", "KGEdge", "KnowledgeGraph", "Conflict",
    "DuplicateCluster", "QAResult", "QualityMetrics", "Resolution",
    "StoreReceipt", "VectorRecord", "Citation", "ContextPackage",
    "Block", "Provenance", "SourceRef",
    "TimeInterval", "BiTemporal",
    # 枚举
    "SourceType", "DocumentFormat", "BlockKind", "EntityType", "ConflictKind",
    "ResolutionStrategy", "MergeStrategy", "DegradationKind", "AttributeValueType",
    "RuleModality", "StepKind", "FlowType",
    "RuleType", "Certainty", "ActorType",
    "FunctionOutputType", "TemporalKind", "ActionLevel", "ConstraintType",
    "PermissionEffect", "RelationPropKind",
    # 错误
    "SminiError", "ContractViolation", "StepError", "DependencyMissing",
    "DimensionMismatch",
    # 工具
    "stable_id", "entity_id", "edge_id", "chunk_id", "mention_id",
    "rule_id", "process_id", "step_id", "flow_id", "doc_id", "content_hash",
    "state_machine_id", "state_id", "transition_id", "function_id",
    "temporal_id", "action_id", "constraint_id", "permission_id",
    "map_span_back", "map_offset_back", "validate_patches", "to_dict",
    # LLM 接缝（智能由宿主 agent 承担：方式 3 · 宿主即 LLM）
    "LLMProvider", "HostAgentLLMProvider", "StubLLMProvider",
    "EXTRACT_SCHEMA", "build_extract_prompt", "default_llm",
]
