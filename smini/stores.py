"""smini.stores — Store 步用到的存储后端。

v1 只提供进程内内存实现（零外部依赖，数据留本地）。GraphStore 的 upsert
语义是契约强制的：同一 entity_id / edge_id 必须覆盖而非追加，且返回
(新增, 更新) 两个计数，让幂等性可被验证。
"""

from __future__ import annotations

from .protocols import GraphStore
from .types import (
    Entity,
    KGEdge,
    MergeStrategy,
    VectorRecord,
    _same_temporal,
)


class MemoryGraphStore(GraphStore):
    """进程内图存储。重复 upsert 同一批数据必须幂等。

    与原版 Neo4j store 全用 CREATE 不同，这里严格走 upsert：
      * 实体存在 → merge_with（保留别名与来源），计为「更新」
      * 边存在且 temporal 相同 → 合并置信度/来源，计为「更新」
      * 否则追加，计为「新增」
    """

    backend = "memory"

    def __init__(self) -> None:
        self.entities: dict[str, Entity] = {}
        self.edges: dict[str, list[KGEdge]] = {}
        self.vectors: dict[str, VectorRecord] = {}

    def upsert_entities(self, entities: "Iterable[Entity]") -> tuple[int, int]:
        written = updated = 0
        for e in entities:
            existing = self.entities.get(e.entity_id)
            if existing is None:
                self.entities[e.entity_id] = e
                written += 1
            else:
                existing.merge_with(e, MergeStrategy.KEEP_MOST_COMPLETE)
                updated += 1
        return written, updated

    def upsert_edges(self, edges: "Iterable[KGEdge]") -> tuple[int, int]:
        written = updated = 0
        for e in edges:
            versions = self.edges.setdefault(e.edge_id, [])
            hit = None
            for v in versions:
                same_obj = (v.object_id == e.object_id) and (v.object_literal == e.object_literal)
                if same_obj and _same_temporal(v.temporal, e.temporal):
                    hit = v
                    break
            if hit is None:
                versions.append(e)
                written += 1
            else:
                hit.confidence = max(hit.confidence, e.confidence)
                if hit.provenance and e.provenance:
                    hit.provenance = hit.provenance.merge(e.provenance)
                elif e.provenance:
                    hit.provenance = e.provenance
                updated += 1
        return written, updated

    def upsert_vectors(self, records: "list[VectorRecord]") -> int:
        n = 0
        for r in records:
            self.vectors[r.vector_id] = r
            n += 1
        return n

    def entity_count(self) -> int:
        return len(self.entities)

    def edge_count(self) -> int:
        return sum(len(v) for v in self.edges.values())
