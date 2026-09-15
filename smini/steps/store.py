"""smini.steps.store — Step 7 · Store

把 QA 修复后的图谱 upsert 进存储后端。v1 默认用进程内 ``MemoryGraphStore``
（零依赖，数据留本地）。若注入了 ``Embedder`` + ``VectorStore``，同时写向量
用于混合检索；否则向量数为 0（embedding 是可选的，缺了不报错、不造假向量，
这与 ``Embedder`` 契约一致）。

StoreReceipt 强制 upsert 语义：返回 (新增, 更新) 计数，让幂等性可被验证。
"""

from __future__ import annotations

from ..protocols import (
    Embedder,
    GraphStore,
    PipelineState,
    PipelineStep,
    RunContext,
    VectorStore,
)
from ..stores import MemoryGraphStore
from ..types import (
    QAResult,
    StoreReceipt,
    VectorRecord,
)


class StoreStep(PipelineStep[QAResult, StoreReceipt]):
    """把修复后的图谱落库，返回回执。"""

    name = "store"
    reads = ("qa",)
    writes = ("receipt",)

    def __init__(
        self,
        store: GraphStore | None = None,
        embedder: Embedder | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self.store = store or MemoryGraphStore()
        self.embedder = embedder
        self.vector_store = vector_store

    def select(self, state: PipelineState) -> QAResult:
        return state.qa  # type: ignore[return-value]

    def transform(self, qa: QAResult, ctx: RunContext) -> StoreReceipt:
        g = qa.graph
        entities = list(g.entities.values())
        edges = g.current_edges()

        ew, eu = self.store.upsert_entities(entities)
        gw, gu = self.store.upsert_edges(edges)
        ctx.log("info", f"store: 实体 +{ew}/~{eu}, 边 +{gw}/~{gu}")

        vectors_written = 0
        if self.embedder is not None and self.vector_store is not None:
            try:
                texts = [e.canonical_name for e in entities]
                vecs = self.embedder.embed(texts)
                records = [
                    VectorRecord(
                        vector_id=e.entity_id,
                        vector=tuple(vecs[i]),
                        text=e.canonical_name,
                        payload={"entity_id": e.entity_id, "entity_type": e.entity_type.value},
                    )
                    for i, e in enumerate(entities)
                ]
                vectors_written = self.vector_store.upsert(records)
                ctx.log("info", f"store: 向量 +{vectors_written}")
            except Exception as exc:  # noqa: BLE001
                ctx.log("error", f"store: 向量写入失败（不影响图）：{exc}")

        return StoreReceipt(
            backend=self.store.backend,
            entities_written=ew, entities_updated=eu,
            edges_written=gw, edges_updated=gu,
            vectors_written=vectors_written,
            elapsed_ms=0,
            idempotent=True,
        )

    def commit(self, state: PipelineState, output: StoreReceipt) -> None:
        state.receipt = output
