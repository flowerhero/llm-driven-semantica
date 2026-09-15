"""smini.steps.qa — Step 6 · QA（冲突检测 + 去重）

**关键契约：QA 必须在 Store 之前跑完，且 ``QAResult.graph`` 返回的是
已修复的图**。相对原版「先落库再检测冲突」，这里从类型上就不可能犯那个错。

v1 做两件事：
  1. 冲突检测：同一 (subject, predicate) 出现多个不同宾语 → Conflict。
     裁决策略默认 MAJORITY_VOTE（无时间信息时）或 MOST_RECENT（有时间时），
     落选的边被**双时态作废**（保留历史，不删除），赢家留在当前视图。
  2. 去重：实体的 canonical_name / 别名相撞 → 集群，把从属实体合并进
     规范实体（重指边、合并别名与属性、删除从属节点）。
"""

from __future__ import annotations

import json
from datetime import datetime

from ..ids import edge_id, stable_id
from ..protocols import PipelineState, PipelineStep, RunContext
from ..types import (
    Conflict,
    ConflictKind,
    ConflictValue,
    DependencyMissing,
    DuplicateCluster,
    KnowledgeGraph,
    KGEdge,
    MergeStrategy,
    QualityMetrics,
    QAResult,
    Resolution,
    ResolutionStrategy,
    StepError,
)


def _value_key(e: KGEdge) -> tuple[str, str]:
    if e.object_literal is not None:
        return ("lit", e.object_literal)
    return ("ent", e.object_id or "")


def _value_text(g: KnowledgeGraph, e: KGEdge) -> str:
    if e.object_literal is not None:
        return e.object_literal
    ent = g.entities.get(e.object_id or "")
    return ent.canonical_name if ent else (e.object_id or "")


def _detect_conflicts(g: KnowledgeGraph, pred_norm: "Callable[[str], str]" = lambda p: p) -> list[Conflict]:
    """按 (subject, predicate) 分组当前视图中的边，多值即冲突。

    ``pred_norm`` 把语义相同的谓词归并到同一规范谓词（如「成立于」「创立于」
    都映射到「成立于」），从而把**改写表达但同义**的关系也纳入冲突检测——
    这是确定性 VALUE 检测之上可选的 LLM 增强（pred_norm 默认恒等，不影响
    确定性路径）。
    """
    groups: dict[tuple[str, str], list[KGEdge]] = {}
    for e in g.current_edges():
        groups.setdefault((e.subject_id, pred_norm(e.predicate)), []).append(e)

    conflicts: list[Conflict] = []
    for (subj, pred), edges in groups.items():
        # 同一事实的多版本（同宾语）不算冲突
        seen: dict[tuple[str, str], list[KGEdge]] = {}
        for e in edges:
            seen.setdefault(_value_key(e), []).append(e)
        if len(seen) <= 1:
            continue
        vals: list[ConflictValue] = []
        for key, vers in seen.items():
            v = vers[0]
            prov = v.provenance
            vals.append(ConflictValue(
                value=_value_text(g, v),
                source_refs=(prov.source_refs if prov else ()),
                confidence=v.confidence,
                valid_from=v.temporal.valid.start if (v.temporal and v.temporal.valid.start) else None,
                valid_to=v.temporal.valid.end if (v.temporal and v.temporal.valid.end) else None,
            ))
        conflicts.append(Conflict(
            conflict_id=stable_id("conflict", subj, pred, prefix="x_"),
            kind=ConflictKind.VALUE,
            subject_id=subj, predicate=pred, values=vals,
        ))
    return conflicts


def _build_pred_norm(ctx: RunContext, g: KnowledgeGraph) -> "Callable[[str], str]":
    """构造谓词归一函数（确定性恒等；有 LLM 时尝试同义归并）。

    仅当 ctx.llm 可用且图谱中出现 >1 种谓词时才调用模型；任何失败都回退恒等，
    绝不改变确定性基线行为。
    """
    llm = ctx.llm
    if llm is None or not llm.is_available():
        return lambda p: p
    preds = sorted({e.predicate for e in g.current_edges()})
    if len(preds) <= 1:
        return lambda p: p
    try:
        prompt = (
            "下面是知识图谱中出现的关系谓词列表。请把**语义相同**的谓词归为一组，"
            "每组指定一个规范谓词（用列表中已有的词）。只返回分组。\n"
            + json.dumps(preds, ensure_ascii=False)
        )
        data = llm.extract(prompt, {
            "type": "object",
            "properties": {
                "groups": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "canonical": {"type": "string"},
                            "members": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                }
            },
        })
        norm: dict[str, str] = {}
        for grp in data.get("groups", []) or []:
            canon = grp.get("canonical")
            for m in grp.get("members", []) or []:
                if canon:
                    norm[m] = canon
        if norm:
            return lambda p: norm.get(p, p)
    except (DependencyMissing, StepError, KeyError, ValueError) as exc:
        ctx.log("warning", f"qa: LLM 谓词归一失败，按确定性检测：{exc}")
    return lambda p: p


def _resolve_conflicts(g: KnowledgeGraph, conflicts: list[Conflict], ctx: RunContext) -> None:
    now = ctx.now()
    for c in conflicts:
        # 频率统计
        freq: dict[tuple[str, str], int] = {}
        edges_by_key: dict[tuple[str, str], list[KGEdge]] = {}
        for e in g.current_edges():
            if e.subject_id == c.subject_id and e.predicate == c.predicate:
                k = _value_key(e)
                freq[k] = freq.get(k, 0) + 1
                edges_by_key.setdefault(k, []).append(e)
        # 有时态信息 → 最近；否则多数票
        has_temporal = any(
            e.temporal and e.temporal.valid.start for e in sum(edges_by_key.values(), [])
        )
        if has_temporal:
            strategy = ResolutionStrategy.MOST_RECENT
            # 取 valid.start 最晚者
            def sort_key(k):
                return max(
                    (e.temporal.valid.start for e in edges_by_key[k] if e.temporal and e.temporal.valid.start),
                    default=datetime.min,
                )
            chosen_key = max(freq, key=sort_key)
        else:
            strategy = ResolutionStrategy.MAJORITY_VOTE
            chosen_key = max(freq, key=lambda k: (freq[k],))
        chosen_text = _value_text(g, edges_by_key[chosen_key][0])
        discarded = tuple(
            _value_text(g, e) for k, es in edges_by_key.items() if k != chosen_key for e in es
        )
        c.resolution = Resolution(
            strategy=strategy, chosen=chosen_text,
            rationale=f"按{strategy.value}裁决：'{(chosen_text)}' 胜出（共 {sum(freq.values())} 条，"
                      f"{len(discarded)} 条被作废）",
            discarded=discarded, needs_review=False,
        )
        # 作废落选边（双时态：保留历史）
        for k, es in edges_by_key.items():
            if k == chosen_key:
                continue
            for e in es:
                if e.temporal is not None:
                    e.temporal = e.temporal.invalidate(now)


def _merge_duplicates(g: KnowledgeGraph, ctx: RunContext) -> list[DuplicateCluster]:
    alias_map: dict[str, list[str]] = {}
    for eid, ent in g.entities.items():
        keys = {ent.canonical_name.casefold(), *(a.casefold() for a in ent.aliases)}
        for k in keys:
            alias_map.setdefault(k, []).append(eid)

    clusters: list[DuplicateCluster] = []
    for key, ids in alias_map.items():
        if len(ids) <= 1:
            continue
        members = [g.entities[i] for i in ids]
        canonical = max(members, key=lambda e: (len(e.mention_ids), len(e.properties), len(e.aliases)))
        others = [e for e in members if e.entity_id != canonical.entity_id]
        if not others:
            continue
        # 重指边 + 合并别名/属性 + 删除从属
        for o in others:
            for versions in g.edges.values():
                for v in versions:
                    if v.subject_id == o.entity_id:
                        v.subject_id = canonical.entity_id
                    if v.object_id == o.entity_id:
                        v.object_id = canonical.entity_id
                    v.edge_id = edge_id(v.subject_id, v.predicate, v.object_id, v.object_literal)
            canonical.aliases.extend(a for a in o.aliases if a.casefold() not in {x.casefold() for x in canonical.aliases})
            for k2, val in o.properties.items():
                if k2 not in canonical.properties or canonical.properties[k2] in (None, "", [], {}):
                    canonical.properties[k2] = val
            canonical.mention_ids.extend(m for m in o.mention_ids if m not in canonical.mention_ids)
            canonical.confidence = max(canonical.confidence, o.confidence)
            del g.entities[o.entity_id]
        # 重键 edge 字典（subject/object 改变后 edge_id 可能变化）
        new_edges: dict[str, list[KGEdge]] = {}
        for versions in g.edges.values():
            for v in versions:
                new_edges.setdefault(v.edge_id, []).append(v)
        g.edges = new_edges
        clusters.append(DuplicateCluster(
            cluster_id=stable_id("dup", key, prefix="dup_"),
            entity_ids=ids, canonical_id=canonical.entity_id,
            strategy=MergeStrategy.KEEP_MOST_COMPLETE,
        ))
    return clusters


class QAStep(PipelineStep[KnowledgeGraph, QAResult]):
    """对图谱做冲突检测与去重，返回已修复的图。"""

    name = "qa"
    reads = ("graph",)
    writes = ("qa",)

    def select(self, state: PipelineState) -> KnowledgeGraph:
        return state.graph  # type: ignore[return-value]

    def transform(self, graph: KnowledgeGraph, ctx: RunContext) -> QAResult:
        g = graph  # 就地修复（保留历史版本）
        conflicts = _detect_conflicts(g, _build_pred_norm(ctx, g))
        _resolve_conflicts(g, conflicts, ctx)
        clusters = _merge_duplicates(g, ctx)
        g.recompute_stats()

        metrics = QualityMetrics(
            conflict_count=len(conflicts),
            unresolved_count=sum(1 for c in conflicts if not c.resolved),
            duplicate_cluster_count=len(clusters),
            entities_merged=sum(len(c.entity_ids) - 1 for c in clusters),
            edges_remapped=sum(len(c.entity_ids) - 1 for c in clusters),
        )
        ctx.log("info", f"qa: {metrics.conflict_count} 冲突, "
                         f"{metrics.duplicate_cluster_count} 重复集群, "
                         f"合并 {metrics.entities_merged} 实体")
        return QAResult(graph=g, conflicts=conflicts, duplicate_clusters=clusters, metrics=metrics)

    def commit(self, state: PipelineState, output: QAResult) -> None:
        state.qa = output
