"""smini.steps.build_kg — Step 5 · Build KG

把 ExtractionResult 的提及与三元组**汇聚**成一张图谱。本步是「契约即规则」
架构下的**惰性锚点层**——抽取层零规则，正确性在这里兜底：

  锚点 1 · ID = sha256(type, canonical)   —— 内容寻址，跨进程幂等
  锚点 2 · 去重 = 同 canonical 即同实体    —— ensure_entity 按 ID 合并
  锚点 3 · object_literal = 数值类类型派生 —— 即使上游漏标，这里强制修正
  锚点 4 · 溯源 = evidence 片段（抽取层已尽力定位；此处消费 canonical 身份）

核心靠 ``entity_id = f(type, canonical_name)`` 的内容寻址：

  * 同批数据重复 build → ID 不变 → add_entity/add_edge 幂等合并
  * 不同文档抽到同一实体 → 自动收敛为同一节点
  * 实体消歧/合并 = 让若干实体共享 canonical_name，再按 ID 归并

双时态（v1 简化）：所有事实用**固定的事务时间** ``FIXED_TX``（而非
``ctx.now()``），这样「同一批数据重跑」是严格幂等的 —— 否则每次 run 的
recorded_at 不同会让 add_edge 误判为新版本而无限追加。valid_time 设为
开区间（恒真）。
"""

from __future__ import annotations

from datetime import datetime, timezone

from ..ids import edge_id, entity_id
from ..protocols import PipelineState, PipelineStep, RunContext
from ..types import (
    BiTemporal,
    Entity,
    EntityType,
    ExtractionResult,
    GraphStats,
    KGEdge,
    KnowledgeGraph,
    MergeStrategy,
    TimeInterval,
)

FIXED_TX = datetime(2000, 1, 1, tzinfo=timezone.utc)

# 惰性锚点 3：宾语为这些类型 → 强制按字面量属性处理（建图时加属性而非节点）
_LITERAL_TYPES = {
    EntityType.DATE, EntityType.MONEY, EntityType.PERCENT, EntityType.QUANTITY,
}


def _edge_meta(t) -> dict:
    """把 v6 关系传导增强（prop_kind/strength）透传到边属性。

    仅携带非空字段（空即普通事实/未标注）；总是返回 dict（KGEdge.metadata
    在 graph schema 中要求 object 类型，不能为 None）。
    """
    meta: dict = {}
    if getattr(t, "prop_kind", ""):
        meta["prop_kind"] = t.prop_kind
    if getattr(t, "strength", ""):
        meta["strength"] = t.strength
    return meta


def _new_temporal() -> BiTemporal:
    return BiTemporal(valid=TimeInterval(), recorded_at=FIXED_TX, invalidated_at=None)


class BuildKGStep(PipelineStep[list, KnowledgeGraph]):
    """把 extraction 列表汇聚成一张 KnowledgeGraph。"""

    name = "build_kg"
    reads = ("extractions",)
    writes = ("graph",)

    def select(self, state: PipelineState) -> list:
        return state.extractions

    def transform(self, extractions: list, ctx: RunContext) -> KnowledgeGraph:
        g = KnowledgeGraph()
        entities_seen: set[str] = set()
        rule_count = 0
        process_count = 0
        state_count = 0
        function_count = 0
        temporal_count = 0
        action_count = 0
        constraint_count = 0
        permission_count = 0

        def ensure_entity(etype: EntityType, norm: str, surface: str,
                          confidence: float, prov) -> Entity:
            eid = entity_id(etype.value, norm)
            ent = g.entities.get(eid)
            if ent is None:
                aliases = [surface] if surface and surface.casefold() != norm.casefold() else []
                ent = Entity(
                    entity_id=eid, canonical_name=norm, entity_type=etype,
                    aliases=aliases, mention_ids=[], confidence=confidence,
                    provenance=prov, temporal=_new_temporal(),
                )
                g.add_entity(ent)
                entities_seen.add(eid)
            else:
                if surface and surface.casefold() not in {a.casefold() for a in ent.aliases} \
                        and surface.casefold() != ent.canonical_name.casefold():
                    ent.aliases.append(surface)
                ent.confidence = max(ent.confidence, confidence)
                if ent.provenance and prov:
                    ent.provenance = ent.provenance.merge(prov)
            return ent

        for ex in extractions:
            # 0) 业务规则（v3）：不进入实体-边图（规则是条件-动作/结论，不是
            #    实体间事实），仅在 metadata 登记 rule_count 供编排层可见。
            rule_count += len(ex.rules or [])
            # 0b) 业务流程（v4）：同样不入实体-边图（流程是持续体 perdurant
            #    而非实体间事实），仅在 metadata 登记 process_count。
            process_count += len(ex.processes or [])
            # 0c) 预测决策本体（v6）六件套：均独立产出不入实体-边图，仅计数登记
            state_count += len(ex.states or [])
            function_count += len(ex.functions or [])
            temporal_count += len(ex.temporal or [])
            action_count += len(ex.actions or [])
            constraint_count += len(ex.constraints or [])
            permission_count += len(ex.permissions or [])

            # 1) 提及 → 实体
            for m in ex.mentions:
                ensure_entity(m.entity_type, m.normalized, m.text, m.confidence, m.provenance)
                ent = g.entities[entity_id(m.entity_type.value, m.normalized)]
                if m.mention_id not in ent.mention_ids:
                    ent.mention_ids.append(m.mention_id)

            # 2) 三元组 → 边（惰性锚点 3：object_literal 由类型强制派生）
            for t in ex.triplets:
                # 上游（LLM 薄壳）可能漏标/标错 literal，这里按类型强制修正
                if t.object_type in _LITERAL_TYPES:
                    t.object_literal = True
                subj = ensure_entity(t.subject_type, t.subject, t.subject, t.confidence, t.provenance)
                if t.object_literal:
                    eid = edge_id(subj.entity_id, t.predicate, None, t.object)
                    edge = KGEdge(
                        edge_id=eid, subject_id=subj.entity_id, predicate=t.predicate,
                        object_id=None, object_literal=t.object,
                        confidence=t.confidence, provenance=t.provenance, temporal=_new_temporal(),
                        metadata=_edge_meta(t),
                    )
                    # 属性双落位：字面量边是权威事实（带 evidence/双时态），
                    # Entity.properties 只是聚合视图（merge_with 已覆盖同名键）。
                    # 仅属性三元组（value_type 非空）落 properties——普通字面量
                    # 关系（如「成立于」→2003年）不进属性视图。
                    if t.value_type is not None:
                        subj.properties[t.predicate] = t.object
                else:
                    obj = ensure_entity(t.object_type or EntityType.OTHER, t.object, t.object,
                                        t.confidence, t.provenance)
                    if t.object_literal is False and t.subject.lower() == t.object.lower():
                        # 自环防卫（理论上不应发生）
                        continue
                    eid = edge_id(subj.entity_id, t.predicate, obj.entity_id, None)
                    edge = KGEdge(
                        edge_id=eid, subject_id=subj.entity_id, predicate=t.predicate,
                        object_id=obj.entity_id, object_literal=None,
                        confidence=t.confidence, provenance=t.provenance, temporal=_new_temporal(),
                        metadata=_edge_meta(t),
                    )
                g.add_edge(edge)

        g.recompute_stats()
        g.metadata["rule_count"] = rule_count
        g.metadata["process_count"] = process_count
        g.metadata["state_count"] = state_count
        g.metadata["function_count"] = function_count
        g.metadata["temporal_count"] = temporal_count
        g.metadata["action_count"] = action_count
        g.metadata["constraint_count"] = constraint_count
        g.metadata["permission_count"] = permission_count
        ctx.log("info", f"build_kg: {g.stats.entity_count} 实体 / "
                         f"{g.stats.edge_count} 边 / {g.stats.literal_edge_count} 字面量边"
                         f" / {rule_count} 条业务规则 / {process_count} 条流程"
                         f" / {state_count} 状态机 / {function_count} 函数指标"
                         f" / {temporal_count} 时态 / {action_count} 动作"
                         f" / {constraint_count} 约束 / {permission_count} 授权")
        return g

    def commit(self, state: PipelineState, output: KnowledgeGraph) -> None:
        state.graph = output
