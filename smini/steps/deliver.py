"""smini.steps.deliver — Step 8 · Deliver

把修复后的图谱按查询组装成一个 ``ContextPackage`` 交给 Agent/应用。
设计取舍：只做**检索与组装**，不做推理（推理留给上层）。v1 用图谱内
关键词检索（实体名/别名/类型 + 谓词命中），未来可换成向量/混合检索。

每个交付事实都带 ``Citation``（出处 + 原文区间 + 置信度），可信度靠它。
"""

from __future__ import annotations

import re

from ..ids import triplet_id
from ..protocols import PipelineState, PipelineStep, RunContext
from ..types import (
    Citation,
    ContextPackage,
    DependencyMissing,
    EntityType,
    Provenance,
    QAResult,
    StepError,
    Triplet,
)


class DeliverStep(PipelineStep[QAResult, ContextPackage]):
    """按 query 检索图谱，产出 ContextPackage。"""

    name = "deliver"
    reads = ("qa",)
    writes = ("delivered",)

    def __init__(self, query: str | None = None) -> None:
        self._query = query

    def select(self, state: PipelineState) -> QAResult:
        return state.qa  # type: ignore[return-value]

    def transform(self, qa: QAResult, ctx: RunContext) -> ContextPackage:
        query = self._query or ctx.config_for("deliver").get("query")
        if not query:
            # 交付是「可选」的最后一步：没有查询意图时，产出空包而非报错，
            # 这样「只想把文档建成图谱并落库」的流水线（以及 cli 不带 -q）
            # 也能完整跑通。
            ctx.log("warn", "deliver: 未提供 query，交付空 ContextPackage")
            return ContextPackage(query="", facts=[], entities=[], citations=[], retrieval="graph")
        return self._deliver(qa, query, ctx)

    def _deliver(self, qa: QAResult, query: str, ctx: RunContext) -> ContextPackage:
        g = qa.graph
        tokens = set(re.findall(r"[一-龥A-Za-z0-9]+", query.lower()))

        matched: list = []
        for e in g.entities.values():
            blob = " ".join(
                [e.canonical_name.lower()] + [a.lower() for a in e.aliases]
                + [e.entity_type.value.lower()]
            )
            if any(tok in blob for tok in tokens):
                matched.append(e)

        facts: list[Triplet] = []
        seen: set[tuple[str, str, str]] = set()
        citations: list[Citation] = []

        def add_fact(subj_ent, edge) -> None:
            obj_name = (g.entities.get(edge.object_id).canonical_name
                        if edge.object_id else edge.object_literal)
            obj_type = (g.entities.get(edge.object_id).entity_type
                        if edge.object_id else None)
            t = Triplet(
                triplet_id=triplet_id(subj_ent.canonical_name, edge.predicate, obj_name or ""),
                subject=subj_ent.canonical_name, subject_type=subj_ent.entity_type,
                predicate=edge.predicate, object=obj_name or "",
                object_type=obj_type, object_literal=edge.object_literal is not None,
                confidence=edge.confidence, provenance=edge.provenance,
            )
            key = (t.subject, t.predicate, t.object)
            if key in seen or not t.object:
                return
            seen.add(key)
            facts.append(t)
            if t.provenance and t.provenance.primary_source:
                citations.append(Citation(
                    source_ref=t.provenance.primary_source,
                    char_span=t.provenance.char_span,
                    quoted_text=f"{t.subject} —[{t.predicate}]→ {t.object}",
                    extractor=t.provenance.extractor,
                    confidence=t.provenance.confidence,
                ))

        # 双向检索：实体既可能是主语，也可能是宾语。
        # neighbors() 同时返回两种方向的边；当实体是宾语时，把视角翻转
        # 成「另一实体 —[谓语]→ 该实体」，保证查询「特斯拉」也能召回
        # 「马斯克 职位 特斯拉」这类边。
        for e in matched:
            for edge in g.neighbors(e.entity_id):
                if edge.subject_id == e.entity_id:
                    add_fact(e, edge)
                else:
                    subj = g.entities.get(edge.subject_id)
                    if subj:
                        add_fact(subj, edge)

        # 谓词命中兜底（query 词命中某条边的谓语，但没命中任何实体）
        if not facts:
            for edge in g.current_edges():
                if any(tok == edge.predicate.lower() for tok in tokens):
                    subj = g.entities.get(edge.subject_id)
                    if subj:
                        add_fact(subj, edge)

        ctx.log("info", f"deliver: query={query!r} → {len(matched)} 实体, "
                         f"{len(facts)} 事实, {len(citations)} 引用")

        # 可选：LLM 在确定性 facts 之上合成自然语言答案（不影响幂等）
        answer = self._synthesize_answer(query, facts, ctx)

        return ContextPackage(
            query=query, facts=facts, entities=matched, citations=citations,
            retrieval="graph", answer=answer,
        )

    def _synthesize_answer(self, query: str, facts: list[Triplet], ctx: RunContext) -> str | None:
        llm = ctx.llm
        if llm is None or not llm.is_available() or not facts:
            return None
        try:
            bullet = "\n".join(f"- {t.subject} —[{t.predicate}]→ {t.object}" for t in facts)
            prompt = (
                f"你是知识助手。基于下列已核实事实，用中文简洁回答用户问题，"
                f"不要编造事实之外的信息。\n用户问题：{query}\n已核实事实：\n{bullet}"
            )
            data = llm.extract(
                prompt,
                {"type": "object", "properties": {"answer": {"type": "string"}},
                 "required": ["answer"]},
            )
            return (data.get("answer") or "").strip() or None
        except (DependencyMissing, StepError, KeyError, ValueError) as exc:
            ctx.log("warning", f"deliver: LLM 答案合成失败，仅返回确定性事实：{exc}")
            return None

    def commit(self, state: PipelineState, output: ContextPackage) -> None:
        state.delivered = output
