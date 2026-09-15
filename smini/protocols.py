"""smini.protocols — 步骤契约与编排契约。

解决的问题
-----------
semantica 原版的编排层（`semantica/pipeline/`）有两个结构性缺陷：

1. **步骤间靠一个 `current_data` dict 传数据**（`execution_engine.py`）。
   dict 没有 schema，某个 step 少写一个 key 只能到运行时才炸，且 IDE
   无法静态检查。这里改用显式字段的 `PipelineState`。

2. **步骤顺序靠人工维护的 DAG 描述**。`ExecutionEngine` 用 Kahn 算法拓扑
   排序，但依赖边是手工声明的，声明错了只能运行时发现。这里改成
   **从每步自己声明的 `reads`/`writes` 反推依赖**，声明即契约，
   顺序错了在构造期就报错。

三段式步骤
----------
每个 step 拆成 `select` → `transform` → `commit`：

    select(state)           從 PipelineState 取出本步输入（胶水）
    transform(inputs, ctx)  **纯函数**，本步的全部业务逻辑（可单测）
    commit(state, output)   写回 PipelineState（胶水）

`transform` 是纯的，意味着单个 step 的单元测试不需要构造整个 pipeline。
原版的 step 直接读写共享 dict，测一个 step 得先把上游全跑一遍。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import (
    Any,
    Callable,
    ClassVar,
    Generic,
    Iterable,
    Mapping,
    Sequence,
    TypeVar,
)

from .types import (
    ContextPackage,
    DimensionMismatch,
    Entity,
    ExtractionResult,
    KGEdge,
    KnowledgeGraph,
    NormalizedDocument,
    ParsedDocument,
    QAResult,
    RawDocument,
    SearchHit,
    StoreReceipt,
    SminiError,
    StepError,
    VectorRecord,
    utcnow,
)

__all__ = [
    "RunContext", "PipelineState", "PipelineStep",
    "StepResult", "validate_step_order", "Pipeline", "run_pipeline",
    "GraphStore", "VectorStore", "Embedder", "LLMProvider",
    "DEFAULT_STEP_ORDER", "rrf_fuse",
]

I = TypeVar("I")
O = TypeVar("O")


# ===========================================================================
# 1. 运行上下文
# ===========================================================================

@dataclass
class RunContext:
    """注入给每个 step 的环境。

    **时间必须注入，不能自己取。**

    直接调 `datetime.now()` 会让流水线无法复现：三个月后重跑同一批数据，
    因为"现在"变了，双时态模型的 transaction_time 就不同，结果无法与
    历史版本比对。这里强制走 `ctx.now()`，测试时注入固定时钟即可
    逐字节复现历史构建。

    同理，ID 生成不注入（它由内容决定，本来就稳定），但**随机数种子**
    必须注入 —— 凡是引入随机性的组件（采样、聚类初始化）都要从
    `ctx.seed` 派生，不能用全局 random。
    """
    run_id: str = ""
    now: Callable[[], datetime] = utcnow
    seed: int = 0
    config: Mapping[str, Any] = field(default_factory=dict)
    logger: Any = None
    events: list[dict[str, Any]] = field(default_factory=list)
    # 可选能力提供方（确定性路径缺省为 None，不报错不降级）
    llm: "LLMProvider | None" = None
    # agent 用 WorkBuddy 工具(browser/pdf/office/资料库)已摄入的 RawDocument，
    # 非空时 Ingest 步直接透传，跳过本地解析（实现「摄取原生」）。
    raw_docs: "list[RawDocument] | None" = None

    def __post_init__(self) -> None:
        if not self.run_id:
            self.run_id = f"run_{int(time.time())}"

    def emit(self, event: str, **payload: Any) -> None:
        """记录一个结构化事件。全链路可观测性的最小实现。"""
        self.events.append({
            "event": event,
            "at": self.now().isoformat(),
            **payload,
        })

    def log(self, level: str, msg: str, **kw: Any) -> None:
        if self.logger is not None:
            getattr(self.logger, level, self.logger.info)(f"[smini] {msg}", **kw)

    def config_for(self, step: str) -> Mapping[str, Any]:
        """取某个 step 的配置片段，缺失返回空 mapping 而非 None。"""
        v = self.config.get(step, {})
        return v if isinstance(v, Mapping) else {}


# ===========================================================================
# 2. 流水线状态
# ===========================================================================

@dataclass
class PipelineState:
    """8 步之间的数据载体。

    每个字段对应一步的产出，**类型明确**。step 通过 `reads`/`writes`
    声明自己触碰哪些字段，编排层据此推导执行顺序。

    为什么不用 dict：
      * dict 拼错 key 静默通过，dataclass 拼错字段名直接 AttributeError
      * IDE 能补全、mypy 能检查
      * 可以静态推导步骤依赖（见 `validate_step_order`）
    """
    # Step 1
    raw: list[RawDocument] = field(default_factory=list)
    # Step 2
    parsed: list[ParsedDocument] = field(default_factory=list)
    # Step 3
    normalized: list[NormalizedDocument] = field(default_factory=list)
    # Step 4
    extractions: list[ExtractionResult] = field(default_factory=list)
    # Step 5
    graph: KnowledgeGraph | None = None
    # Step 6
    qa: QAResult | None = None
    # Step 7
    receipt: StoreReceipt | None = None
    # Step 8
    delivered: ContextPackage | None = None

    #: 字段名 → 该字段由哪一步写入。
    #: 必须是 ClassVar —— 若写成 dataclass 字段，它会被塞进 `__init__` 参数、
    #: 参与 `__eq__` 比较，且只能通过实例访问，编排层的类级校验会失效。
    FIELD_OWNER: ClassVar[dict[str, str]] = {
        "raw": "ingest",
        "parsed": "parse",
        "normalized": "normalize",
        "extractions": "extract",
        "graph": "build_kg",
        "qa": "qa",
        "receipt": "store",
        "delivered": "deliver",
    }

    def get(self, name: str) -> Any:
        if not hasattr(self, name):
            raise StepError("<state>", f"PipelineState 无字段 {name!r}")
        return getattr(self, name)


# ===========================================================================
# 3. 步骤基类
# ===========================================================================

@dataclass
class StepResult(Generic[O]):
    """step 的执行结果包装，让编排层能统一统计与容错。"""
    step: str
    output: O
    elapsed_ms: int = 0
    items_in: int = 0
    items_out: int = 0
    warnings: list[str] = field(default_factory=list)


class PipelineStep(ABC, Generic[I, O]):
    """流水线步骤基类。

    子类必须声明三个 ClassVar 并实现三个方法：

        class MyStep(PipelineStep[list[ParsedDocument], list[NormalizedDocument]]):
            name = "normalize"
            reads = ("parsed",)
            writes = ("normalized",)

            def select(self, state): return state.parsed
            def transform(self, inputs, ctx): ...        # 纯逻辑
            def commit(self, state, out): state.normalized = out

    注意 `transform` 应当保持纯粹：不读写 state、不碰全局时钟、不产生
    随机性。这样单测只需 `step.transform(fixture, ctx)`。
    """

    #: 步骤名，同时用作 `ctx.config` 的键
    name: str = ""
    #: 本步**读取**的 PipelineState 字段名
    reads: tuple[str, ...] = ()
    #: 本步**写入**的 PipelineState 字段名
    writes: tuple[str, ...] = ()

    # -- 子类实现 -----------------------------------------------------------
    @abstractmethod
    def select(self, state: PipelineState) -> I:
        """从 state 取出本步输入。"""

    @abstractmethod
    def transform(self, inputs: I, ctx: RunContext) -> O:
        """核心逻辑。**纯函数**。"""

    @abstractmethod
    def commit(self, state: PipelineState, output: O) -> None:
        """把输出写回 state。"""

    # -- 可选钩子 -----------------------------------------------------------
    def count_items(self, obj: Any) -> int:
        """统计处理规模，用于日志。默认按 len 或 1。"""
        try:
            return len(obj)  # type: ignore[arg-type]
        except TypeError:
            return 1 if obj is not None else 0

    def enabled(self, ctx: RunContext) -> bool:
        """是否启用本步。返回 False 则整步跳过（但仍需在 DAG 中合法）。"""
        return True

    # -- 模板方法（子类不要覆写） -------------------------------------------
    def execute(self, state: PipelineState, ctx: RunContext) -> StepResult[O]:
        """执行本步。这是编排层唯一调用的方法。"""
        if self.name in ("", None):
            raise StepError(type(self).__name__, "未声明 name")
        if not self.enabled(ctx):
            ctx.log("info", f"step {self.name} 已禁用，跳过")
            return StepResult(step=self.name, output=None)  # type: ignore[return-value]

        self._check_precondition(state)
        inputs = self.select(state)
        n_in = self.count_items(inputs)

        ctx.log("info", f"step {self.name} 开始，输入 {n_in} 项")
        t0 = time.perf_counter()
        try:
            output = self.transform(inputs, ctx)
        except SminiError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一包装成 StepError
            raise StepError(self.name, f"执行失败：{exc}", cause=exc) from exc
        elapsed = int((time.perf_counter() - t0) * 1000)

        self.commit(state, output)
        n_out = self.count_items(output)
        ctx.emit("step_done", step=self.name, ms=elapsed, n_in=n_in, n_out=n_out)
        ctx.log("info", f"step {self.name} 完成，输出 {n_out} 项，{elapsed}ms")
        return StepResult(
            step=self.name,
            output=output,
            elapsed_ms=elapsed,
            items_in=n_in,
            items_out=n_out,
        )

    def _check_precondition(self, state: PipelineState) -> None:
        """校验 reads 的字段已被上游填充。

        宁可在这里崩，也不要让 step 拿到 None 后在深处报出费解的错。
        """
        for f in self.reads:
            if f not in state.FIELD_OWNER:
                raise StepError(self.name, f"reads 声明了未知字段 {f!r}")
            value = getattr(state, f)
            if value is None:
                raise StepError(
                    self.name,
                    f"前置字段 {f!r} 为空 —— {state.FIELD_OWNER[f]} 步没有产出，"
                    f"或执行顺序错误",
                )
            if isinstance(value, (list, dict)) and len(value) == 0:
                raise StepError(
                    self.name,
                    f"前置字段 {f!r} 为空集合 —— 上游 {state.FIELD_OWNER[f]} "
                    f"步产出 0 条，继续跑没有意义",
                )


# ===========================================================================
# 4. 顺序校验（声明即依赖，不需要手工维护 DAG）
# ===========================================================================

def validate_step_order(steps: Sequence[PipelineStep]) -> None:
    """校验步骤序列的合法性。在 **构造期** 调用，不是运行期。

    规则：某步 `reads` 的每个字段，必须由**在它之前**的某步 `writes`。
    这等价于要求步骤序列是数据流的一个拓扑序，但依赖边不用手工写 ——
    从 reads/writes 自动推导。

    Raises:
        SminiError: 顺序非法、字段无人生产、或重复生产。
    """
    produced: dict[str, str] = {}
    seen_names: set[str] = set()

    for step in steps:
        if not step.name:
            raise SminiError(f"{type(step).__name__} 未声明 name")
        if step.name in seen_names:
            raise SminiError(f"步骤名重复：{step.name}")
        seen_names.add(step.name)

        # 读的字段必须已被生产
        for f in step.reads:
            if f not in produced:
                raise SminiError(
                    f"步骤 {step.name!r} 读取字段 {f!r}，但它之前的步骤"
                    f" [{', '.join(s.name for s in steps) or '无'}] 都没有产出该字段。"
                    f"该字段由 '{step.name}' 之外的步骤负责，顺序有误。"
                )

        # 写的字段不应重复生产（重复意味着后一步会覆盖前一步）
        for f in step.writes:
            if f in produced:
                raise SminiError(
                    f"字段 {f!r} 被 {produced[f]!r} 和 {step.name!r} 重复写入 —— "
                    f"后者会静默覆盖前者的产出。"
                )
            produced[f] = step.name

    missing = set(PipelineState.FIELD_OWNER) - set(produced)
    # 允许流水线只跑到某一步（如只做 extract 不建图），故缺失不一定是错。
    # 但必须保证：被生产出来的字段，其所有上游字段也都生产了 —— 这一点
    # 已由上面的 reads 检查覆盖。
    _ = missing


#: 官方 8 步的标准顺序（docs 站口径）。
#: 用于 validate_step_order 之外的文档与测试参照。
DEFAULT_STEP_ORDER: tuple[str, ...] = (
    "ingest",      # 01 → RawDocument
    "parse",       # 02 → ParsedDocument
    "normalize",   # 03 → NormalizedDocument
    "extract",     # 04 → ExtractionResult（含 split）
    "build_kg",    # 05 → KnowledgeGraph
    "qa",          # 06 → QAResult（冲突检测 + 去重）
    "store",       # 07 → StoreReceipt
    "deliver",     # 08 → ContextPackage
)


# ===========================================================================
# 5. 流水线
# ===========================================================================

@dataclass
class Pipeline:
    """8 步的线性编排。

    刻意**不做** DAG 并行调度。

    semantica 原版有 `ParallelismManager` 和基于 Kahn 算法的拓扑排序，
    但实际上这条流水线的 8 步是**严格线性**的 —— 每一步都依赖上一步的
    全部产出，没有任何可并行的分支。为一个天然线性的流程引入拓扑排序
    和并行调度，只会增加复杂度而不带来收益。

    真正值得并行的是**步内**的文档级处理（1000 个 PDF 的解析可以并行），
    那是各 step 实现自己的事，不该由编排层越俎代庖。
    """
    steps: list[PipelineStep] = field(default_factory=list)
    name: str = "default"
    llm: "LLMProvider | None" = None  # 可选：注入后 Parse/Extract/QA/Deliver 走 LLM 路径

    def __post_init__(self) -> None:
        validate_step_order(self.steps)

    def add(self, step: PipelineStep) -> "Pipeline":
        """追加一步并重新校验。返回 self 便于链式调用。"""
        self.steps.append(step)
        validate_step_order(self.steps)
        return self

    def run(
        self,
        state: PipelineState | None = None,
        ctx: RunContext | None = None,
        *,
        stop_after: str | None = None,
        fail_fast: bool = True,
    ) -> tuple[PipelineState, list[StepResult]]:
        """执行流水线。

        Args:
            state: 初始状态。为空则新建（此时第一步通常是 ingest）。
            ctx: 运行上下文。
            stop_after: 执行到该步为止（含），用于调试与只跑前半段。
            fail_fast: True 时任一步失败立即抛出；False 时记录后中止，
                       返回已完成的步骤结果。

        Returns:
            (最终 state, 每步的 StepResult)
        """
        state = state or PipelineState()
        ctx = ctx or RunContext()
        # 编排层持有 llm 时，自动注入到 ctx（除非调用方已显式指定）
        if self.llm is not None and ctx.llm is None:
            ctx.llm = self.llm
        results: list[StepResult] = []

        for step in self.steps:
            try:
                r = step.execute(state, ctx)
            except StepError as exc:
                ctx.emit("step_failed", step=step.name, error=str(exc))
                if fail_fast:
                    raise
                ctx.log("error", f"step {step.name} 失败，中止：{exc}")
                break
            results.append(r)
            if stop_after is not None and step.name == stop_after:
                ctx.log("info", f"已在 {step.name} 处按 stop_after 停止")
                break

        return state, results

    def summary(self, results: Sequence[StepResult]) -> str:
        lines = [f"pipeline={self.name}"]
        for r in results:
            lines.append(
                f"  {r.step:<10} {r.items_in:>6} → {r.items_out:<6} "
                f"{r.elapsed_ms:>6}ms"
            )
        return "\n".join(lines)


def run_pipeline(
    steps: Sequence[PipelineStep],
    state: PipelineState | None = None,
    ctx: RunContext | None = None,
) -> tuple[PipelineState, list[StepResult]]:
    """一步到位的便捷入口。"""
    return Pipeline(steps=list(steps)).run(state, ctx)


# ===========================================================================
# 6. Store 抽象
# ===========================================================================

class GraphStore(ABC):
    """图存储抽象。

    与原版的关键差别：**接口强制 upsert 语义**。

    原版 Neo4j store 全部用 `CREATE`（`MERGE` 零命中），同一批数据 build
    两次就得到两份节点。这里把 upsert 写进契约，并要求实现返回
    written/updated 两个计数，让幂等性**可被验证**而不只是被声称。

    原版另一个问题是三个 store（vector / graph / triplet）**没有任何共同
    基类**，靠 if/elif 工厂 + `hasattr()` 鸭子探测撑起"可替换"。这里有
    明确的 ABC，缺失方法在实例化时就报 TypeError，不会等到调用时才炸。
    """

    #: backend 标识，写进 StoreReceipt
    backend: str = "memory"

    @abstractmethod
    def upsert_entities(self, entities: Iterable[Entity]) -> tuple[int, int]:
        """写入实体，返回 (新增数, 更新数)。同一 entity_id 必须覆盖而非追加。"""

    @abstractmethod
    def upsert_edges(self, edges: Iterable[KGEdge]) -> tuple[int, int]:
        """写入边，返回 (新增数, 更新数)。"""

    def healthcheck(self) -> bool:
        """连通性检查。默认乐观返回 True，网络型 backend 应覆写。"""
        return True


class VectorStore(ABC):
    """向量存储抽象。

    关键差别：**dimension 在构造时声明，写入时强制校验**。

    原版 `VectorStore` 默认 768 维，而默认 embedder（`BAAI/bge-small-en-v1.5`）
    是 384 维 —— 两者天生不匹配。更糟的是 embedding 失败时返回随机向量
    只打一条 WARNING（`vector_store.py:303-307`），索引照样可检索，
    但语义完全无意义。这里在 `upsert` 入口硬校验维度，不匹配直接
    `DimensionMismatch`，绝不让脏数据进索引。
    """

    def __init__(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension 必须为正，收到 {dimension}")
        self.dimension = dimension

    @abstractmethod
    def upsert(self, records: Sequence[VectorRecord]) -> int:
        """写入向量，返回写入条数。

        Raises:
            DimensionMismatch: 任一记录的维度与 self.dimension 不符。
        """

    @abstractmethod
    def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        """检索。返回的 SearchHit 必须带 `payload["entity_id"]` 以便回指图谱。"""

    def _check_dim(self, records: Sequence[VectorRecord]) -> None:
        for r in records:
            if len(r.vector) != self.dimension:
                raise DimensionMismatch(
                    f"向量 {r.vector_id} 维度 {len(r.vector)} ≠ "
                    f"索引维度 {self.dimension}"
                )


# ===========================================================================
# 7. 能力提供方抽象（Step 04 / Step 07 的可选依赖）
# ===========================================================================
#
# 这两个不是"步骤"，而是步骤依赖的**能力**。把它们单列为 ABC 的意义：
# 实现层可以注入任意后端（spaCy / 本地模型 / 远程 API），契约层不关心。
# 唯一硬性规定是 —— 能力缺失时**抛 `DependencyMissing`，绝不返回垃圾结果**。
# 这是相对原版（`semantic_extract/ner_extractor.py` 静默退回正则）的核心修正。

class Embedder(ABC):
    """文本向量化能力。Store 步写入向量前依赖它。

    **禁止返回随机向量**：embedding 模型不可用必须抛 `DependencyMissing`，
    由调用方决定降级（登记 Degradation）还是失败。原版
    `vector_store.py:303-307` 在失败时返回随机向量只打 WARNING，结果索引
    可检索但语义完全无意义 —— 这里从类型与异常上杜绝。
    """

    def __init__(self, dimension: int) -> None:
        if dimension <= 0:
            raise ValueError(f"dimension 必须为正，收到 {dimension}")
        self.dimension = dimension

    @abstractmethod
    def embed(self, texts: Sequence[str]) -> list[tuple[float, ...]]:
        """批量向量化。返回的向量等长等于输入，且每个维度 == self.dimension。

        Raises:
            DependencyMissing: 模型未加载 / 无 GPU / 无网络。
        """

    def _check_dim(self, vectors: Sequence[tuple[float, ...]]) -> None:
        for i, v in enumerate(vectors):
            if len(v) != self.dimension:
                raise DimensionMismatch(
                    f"embed 产出维度 {len(v)} ≠ 声明维度 {self.dimension}（第 {i} 条）"
                )


class LLMProvider(ABC):
    """LLM 抽取能力。Extract 步的可选后端（NER / 关系 / 三元组）。

    契约要点：
      * `is_available()` 让编排层在构造期就知道能不能用，不必等到调用时才炸。
      * `extract()` 接受 JSON Schema 约束输出结构，返回 dict —— 调用方负责
        把 dict 映射成 `Entity`/`Relation`/`Triplet`。结构化约束是避免
        "LLM 自由发挥导致下游解析失败"的关键。
      * 不可用时不静默降级：抛 `DependencyMissing`，由调用方登记 `Degradation`。

    v1 默认不接 LLM（全确定性抽取），但这个接口必须在契约里就位，
    否则日后接入要改 Extract 的签名。
    """

    model: str = ""

    @abstractmethod
    def is_available(self) -> bool:
        """凭据 / 模型是否就绪。"""

    @abstractmethod
    def extract(
        self,
        prompt: str,
        schema: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按 prompt 抽取，schema 约束输出结构（JSON Schema）。

        Raises:
            DependencyMissing: 当 `is_available()` 为 False。
            StepError: 输出无法解析或不符合 schema。
        """


def rrf_fuse(
    *ranked_lists: Sequence[SearchHit],
    k: int = 60,
    top_k: int = 10,
) -> list[SearchHit]:
    """倒数排名融合（Reciprocal Rank Fusion）。

    score(d) = Σ_lists  1 / (k + rank_l(d))

    沿用原版 `hybrid_search.py:148-167` 的公式与默认 k=60，这是混合检索
    里验证最充分的融合方式，没有理由自创。
    """
    fused: dict[str, float] = {}
    payloads: dict[str, SearchHit] = {}

    for hits in ranked_lists:
        for hit in hits:
            fused[hit.vector_id] = fused.get(hit.vector_id, 0.0) + 1.0 / (k + hit.rank)
            payloads.setdefault(hit.vector_id, hit)

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
    out: list[SearchHit] = []
    for vid, score in ranked:
        base = payloads[vid]
        out.append(replace(base, score=score, rank=len(out), source="hybrid"))
    return out
