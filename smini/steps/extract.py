"""smini.steps.extract — Extract（契约即规则 · 零规则薄壳）

extract-only 架构下为本流水线的**核心步骤**（宿主 LLM 契约抽取）。

v2 删除全部确定性抽取规则：正则 NER、8 条关系模式、伪实体黑名单、
entity-aware 切分、确定性 fallback。**LLM 是唯一抽取引擎**，本 step
只做一件事：把 LLM 按契约交卷的 dict 映射成 ``ExtractionResult``。

设计立场（契约即规则）：
  * LLM 输出契约：
      entities:  [{ surface, canonical, type }]
      relations: [{ subject, predicate, object, object_type, evidence }]
    不含字符偏移、不含 ID、不含 object_literal —— 这些由消费层确定。
  * 确定性锚点惰性化，推迟到消费层（build_kg / runtime）：
      - ID = sha256(canonical)      身份绑定规范名，LLM 不参与
      - span = surface/evidence 尽力 find，找不到给 (-1,-1) / None
      - object_literal = 由 object_type 派生（数值类 → 字面量）
      - 去重 = 同 canonical 即同实体（build_kg 按 ID 合并）
  * 无可用 LLM 时不造假：返回空结果并登记 Degradation，stats.mode="empty"；
    LLM 调用失败同样降级为空 + Degradation(unavailable)，绝不静默。
"""

from __future__ import annotations

from ..ids import (
    action_id,
    chunk_id,
    constraint_id,
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
from ..llm import EXTRACT_SCHEMA, build_extract_prompt
from ..protocols import PipelineState, PipelineStep, RunContext
from ..types import (
    Action,
    ActionLevel,
    ActorType,
    AttributeValueType,
    Certainty,
    Chunk,
    Constraint,
    ConstraintType,
    Degradation,
    DegradationKind,
    DependencyMissing,
    EntityMention,
    EntityType,
    ExtractionResult,
    FlowType,
    Function,
    FunctionOutputType,
    NormalizedDocument,
    Permission,
    PermissionEffect,
    Process,
    ProcessFlow,
    ProcessStep,
    Provenance,
    Relation,
    Rule,
    RuleModality,
    RuleType,
    StateDef,
    StateMachine,
    StepError,
    StepKind,
    Temporal,
    TemporalKind,
    TransitionDef,
    Triplet,
)

# 宾语为这些类型 → object_literal = True（字面量属性，建图时加属性而非节点）
_LITERAL_TYPES = {
    EntityType.DATE, EntityType.MONEY, EntityType.PERCENT, EntityType.QUANTITY,
}


def _normalized_from_raw(rd) -> NormalizedDocument:
    """extract-only 适配：RawDocument（字节）→ NormalizedDocument（纯文本）。

    摄入/归一化外包宿主后，解析步骤已删除；本步直接消费输入层的
    ``RawDocument.content``（UTF-8 解码，坏字节以替换符兜底）。
    """
    text = rd.content.decode("utf-8", errors="replace")
    return NormalizedDocument(doc_id=rd.doc_id, source=rd.source, text=text)


class ExtractStep(PipelineStep[list, list[ExtractionResult]]):
    """把归一化文档列表抽成 ExtractionResult 列表（零规则薄壳）。"""

    name = "extract"
    reads = ("raw",)
    writes = ("extractions",)

    def select(self, state: PipelineState):
        # extract-only 输入适配：优先消费宿主直投的 normalized（驱动脚本手写
        # 03-normalized.json 的场景），否则从输入层产出的 raw 构造纯文本文档。
        if getattr(state, "normalized", None):
            return state.normalized
        return [_normalized_from_raw(d) for d in state.raw]

    def transform(self, docs: list, ctx: RunContext) -> list[ExtractionResult]:
        llm = ctx.llm
        if llm is None or not llm.is_available():
            # 零规则架构无确定性兜底：不造假，空结果 + 显式降级
            return [self._empty_result(nd, DegradationKind.NO_CREDENTIAL,
                                       "未注入可用 LLMProvider，零规则架构无确定性兜底")
                    for nd in docs]
        try:
            return self._extract_with_llm(docs, ctx)
        except (StepError, DependencyMissing) as exc:
            ctx.log("warning", f"extract: LLM 抽取失败，降级为空结果：{exc}")
            return [self._empty_result(nd, DegradationKind.UNAVAILABLE,
                                       f"LLM 调用失败：{exc}") for nd in docs]

    # ------------------------------------------------------------------ #
    # 薄壳：LLM 提议 → dataclass（无业务判定）                            #
    # ------------------------------------------------------------------ #

    def _extract_with_llm(self, docs: list, ctx: RunContext) -> list[ExtractionResult]:
        out: list[ExtractionResult] = []
        for nd in docs:
            data = ctx.llm.extract(build_extract_prompt(nd.text), EXTRACT_SCHEMA)  # type: ignore[union-attr]
            entities = data.get("entities", []) or []
            relations = data.get("relations", []) or []
            attributes = data.get("attributes", []) or []
            rules = data.get("rules", []) or []
            processes = data.get("processes", []) or []
            states = data.get("states", []) or []
            functions = data.get("functions", []) or []
            temporal = data.get("temporal", []) or []
            actions = data.get("actions", []) or []
            constraints = data.get("constraints", []) or []
            permissions = data.get("permissions", []) or []

            mentions, by_key, by_surface = self._mentions_from_llm(entities, nd)
            rels, trips, created = self._relations_from_llm(relations, mentions, by_key, by_surface, nd)
            attr_rels, attr_trips, attr_created = self._attributes_from_llm(
                attributes, by_key, by_surface, nd)
            rule_objs = self._rules_from_llm(rules, nd)
            proc_objs = self._processes_from_llm(processes, nd)
            # v6：预测决策本体六件套（均独立产出，不入实体-边图）
            sm_objs = self._states_from_llm(states, nd)
            fn_objs = self._functions_from_llm(functions, nd)
            tm_objs = self._temporal_from_llm(temporal, nd)
            ac_objs = self._actions_from_llm(actions, nd)
            cn_objs = self._constraints_from_llm(constraints, nd)
            pm_objs = self._permissions_from_llm(permissions, nd)
            all_mentions = mentions + created + attr_created

            chunks = self._simple_chunks(nd, all_mentions)

            seen_t: set[str] = set()
            uniq_trips: list[Triplet] = []
            for t in trips + attr_trips:
                if t.triplet_id not in seen_t:
                    seen_t.add(t.triplet_id)
                    uniq_trips.append(t)

            # 规则去重：同 (subject, condition, action, modality) 即同规则（rule_id 内容寻址）
            seen_r: set[str] = set()
            uniq_rules: list[Rule] = []
            for r in rule_objs:
                if r.rule_id not in seen_r:
                    seen_r.add(r.rule_id)
                    uniq_rules.append(r)

            # 流程去重：同 name 即同流程（process_id 内容寻址）
            seen_p: set[str] = set()
            uniq_procs: list[Process] = []
            for pr in proc_objs:
                if pr.process_id not in seen_p:
                    seen_p.add(pr.process_id)
                    uniq_procs.append(pr)

            # v6 六件套去重（各自 ID 内容寻址，同 ID 即同一对象）
            uniq_sm = _dedup(sm_objs, lambda x: x.sm_id)
            uniq_fn = _dedup(fn_objs, lambda x: x.function_id)
            uniq_tm = _dedup(tm_objs, lambda x: x.temporal_id)
            uniq_ac = _dedup(ac_objs, lambda x: x.action_id)
            uniq_cn = _dedup(cn_objs, lambda x: x.constraint_id)
            uniq_pm = _dedup(pm_objs, lambda x: x.permission_id)

            attr_kept = sum(1 for t in uniq_trips if t.value_type is not None)

            # v7：document_meta（manifest 头借鉴）——source 强制取输入文档 URI；
            # domain/version/doc_type 由宿主契约可选交卷，薄壳仅清洗
            dm_raw = data.get("document_meta") or {}
            src_uri = getattr(nd.source, "uri", None)
            if not src_uri and isinstance(nd.source, dict):
                src_uri = nd.source.get("uri")
            dm = {
                "domain": str(dm_raw.get("domain") or "").strip(),
                "source": str(src_uri or "").strip() or nd.doc_id,
                "version": str(dm_raw.get("version") or "").strip(),
                "doc_type": str(dm_raw.get("doc_type") or "").strip(),
            }

            res = ExtractionResult(
                doc_id=nd.doc_id, chunks=chunks, mentions=all_mentions,
                relations=rels + attr_rels, triplets=uniq_trips, rules=uniq_rules,
                processes=uniq_procs, states=uniq_sm, functions=uniq_fn,
                temporal=uniq_tm, actions=uniq_ac, constraints=uniq_cn,
                permissions=uniq_pm, document_meta=dm,
                stats={
                    "mode": "llm", "chunks": len(chunks),
                    "mentions": len(all_mentions),
                    "relations": len(rels) + len(attr_rels),
                    "triplets": len(uniq_trips),
                    "attributes": attr_kept,
                    "rules": len(uniq_rules),
                    "processes": len(uniq_procs),
                    "states": len(uniq_sm),
                    "functions": len(uniq_fn),
                    "temporal": len(uniq_tm),
                    "actions": len(uniq_ac),
                    "constraints": len(uniq_cn),
                    "permissions": len(uniq_pm),
                },
            )
            out.append(res)
            ctx.log("info", f"extract(llm): {nd.doc_id} → {res.stats}")
        return out

    def _mentions_from_llm(self, entities: list, nd: NormalizedDocument
                           ) -> tuple[list[EntityMention], dict, dict]:
        """entities → EntityMention。span 尽力定位（找不到给 -1），ID 由 Python 算。

        返回 (mentions, by_key, by_surface)：
          by_key[(type, canonical.casefold())]   —— 供关系宾语按类型+规范名反查
          by_surface[surface|canonical.casefold()] —— 供关系按文本反查
        """
        out: list[EntityMention] = []
        by_key: dict = {}
        by_surface: dict = {}
        for e in entities:
            try:
                etype = EntityType(e.get("type") or "OTHER")
            except ValueError:
                etype = EntityType.OTHER
            surface = (e.get("surface") or "").strip()
            if not surface:
                continue
            canonical = (e.get("canonical") or surface).strip()
            s, en = _locate_span(nd.text, surface)
            if s >= 0:
                mid = mention_id(nd.doc_id, s, en, surface)
            else:
                # 定位不到：身份退化为 canonical（同 canonical 跨文档收敛）
                mid = stable_id("mention", canonical, prefix="m_")
            prov = Provenance(source_refs=(nd.source,), extractor="llm.ner.v1",
                              confidence=0.85, char_span=(s, en) if s >= 0 else None)
            out.append(EntityMention(
                mention_id=mid, doc_id=nd.doc_id, text=surface, normalized=canonical,
                entity_type=etype, char_start=s, char_end=en, confidence=0.85,
                extractor="llm.ner.v1", provenance=prov,
            ))
            by_key[(etype, canonical.casefold())] = out[-1]
            by_surface[surface.casefold()] = out[-1]
            by_surface[canonical.casefold()] = out[-1]
        return out, by_key, by_surface

    def _relations_from_llm(self, relations, mentions, by_key, by_surface, nd):
        """relations → Relation + Triplet。object_literal 由 object_type 派生。

        主语/宾语优先解析到已声明实体（保留 LLM 给出的真实类型），
        解析不到再合成 mention（extractor=llm.rel.synth.v1）。ID 全部由
        确定性函数算（sha256 内容寻址），LLM 不参与。
        """
        rels: list[Relation] = []
        trips: list[Triplet] = []
        created: list[EntityMention] = []

        def get_mention(surface: str, fallback_type: EntityType) -> EntityMention:
            hit = by_surface.get(surface.casefold())
            if hit is not None:
                return hit
            s, en = _locate_span(nd.text, surface)
            mid = mention_id(nd.doc_id, s, en, surface) if s >= 0 \
                else stable_id("mention", surface, prefix="m_")
            prov = Provenance(source_refs=(nd.source,), extractor="llm.rel.synth.v1",
                              confidence=0.7, char_span=(s, en) if s >= 0 else None)
            m = EntityMention(mention_id=mid, doc_id=nd.doc_id, text=surface,
                              normalized=surface, entity_type=fallback_type,
                              char_start=s, char_end=en, confidence=0.7,
                              extractor="llm.rel.synth.v1", provenance=prov)
            by_surface[surface.casefold()] = m
            created.append(m)
            return m

        for r in relations:
            subj_s = (r.get("subject") or "").strip()
            pred = (r.get("predicate") or "").strip()
            obj_s = (r.get("object") or "").strip()
            if not (subj_s and pred and obj_s):
                continue

            # 宾语类型：LLM 给的 object_type 优先；否则反查已声明实体；再否则 OTHER
            obj_t: EntityType | None = None
            raw_ot = r.get("object_type")
            if raw_ot:
                try:
                    obj_t = EntityType(raw_ot)
                except ValueError:
                    obj_t = None
            if obj_t is None:
                hit = by_surface.get(obj_s.casefold())
                obj_t = hit.entity_type if hit is not None else EntityType.OTHER

            is_lit = obj_t in _LITERAL_TYPES
            evidence = (r.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None

            # v6 传导增强：prop_kind 独立 4 类枚举（非法 → 空=普通事实）；
            # strength 透传 HIGH/MEDIUM/LOW 或原文程度词（可空）
            raw_pk = (r.get("prop_kind") or "").strip()
            if raw_pk and raw_pk not in ("FACTUAL", "DEPENDENCY", "CAUSAL", "TRIGGER"):
                raw_pk = ""
            strength = (r.get("strength") or "").strip()

            subj = get_mention(subj_s, EntityType.OTHER)
            prov = Provenance(source_refs=(nd.source,), extractor="llm.rel.v1",
                              confidence=0.8, char_span=ev_span_t)

            if is_lit:
                rid = edge_id(subj.mention_id, pred, None, obj_s)
                rel = Relation(
                    relation_id=rid, doc_id=nd.doc_id, subject_ref=subj.mention_id,
                    object_ref="", predicate=pred, confidence=0.8, chunk_id=None,
                    evidence_span=ev_span_t, evidence_text=evidence,
                    extractor="llm.rel.v1", provenance=prov,
                    prop_kind=raw_pk, strength=strength,
                )
                # triplet_id 与 cmd_ids 补算保持同一构成：主语用 entity_id 内容寻址
                trip = Triplet(
                    triplet_id=triplet_id(
                        entity_id(subj.entity_type.value, subj.normalized), pred, obj_s),
                    subject=subj.normalized, subject_type=subj.entity_type,
                    predicate=pred, object=obj_s, object_type=obj_t,
                    object_literal=True, confidence=0.8,
                    prop_kind=raw_pk, strength=strength,
                    source_mentions=(subj.mention_id,),
                    extractor="llm.rel.v1", provenance=prov,
                )
            else:
                obj = get_mention(obj_s, obj_t)
                rid = edge_id(subj.mention_id, pred, obj.mention_id)
                rel = Relation(
                    relation_id=rid, doc_id=nd.doc_id, subject_ref=subj.mention_id,
                    object_ref=obj.mention_id, predicate=pred, confidence=0.8,
                    chunk_id=None, evidence_span=ev_span_t, evidence_text=evidence,
                    extractor="llm.rel.v1", provenance=prov,
                    prop_kind=raw_pk, strength=strength,
                )
                trip = Triplet(
                    triplet_id=triplet_id(
                        entity_id(subj.entity_type.value, subj.normalized), pred,
                        entity_id(obj.entity_type.value, obj.normalized)),
                    subject=subj.normalized, subject_type=subj.entity_type,
                    predicate=pred, object=obj.normalized, object_type=obj_t,
                    object_literal=False, confidence=0.8,
                    prop_kind=raw_pk, strength=strength,
                    source_mentions=(subj.mention_id, obj.mention_id),
                    extractor="llm.rel.v1", provenance=prov,
                )
            rels.append(rel)
            trips.append(trip)
        return rels, trips, created

    def _attributes_from_llm(self, attributes, by_key, by_surface, nd):
        """attributes → Relation + Triplet（属性三元组，extractor=llm.attr.v1）。

        attributes 是实体**数据属性**（本体论 DatatypeProperty）：宾语是字面量，
        永远 ``object_literal=True``，value_type 独立枚举。实体归属按 canonical
        反查已声明实体；匹配不到则合成 mention（extractor=llm.attr.synth.v1，
        兜底 OTHER 实体），不因归属失配而丢属性。
        """
        rels: list[Relation] = []
        trips: list[Triplet] = []
        created: list[EntityMention] = []

        def get_entity(surface: str) -> EntityMention:
            hit = by_surface.get(surface.casefold())
            if hit is not None:
                return hit
            s, en = _locate_span(nd.text, surface)
            mid = mention_id(nd.doc_id, s, en, surface) if s >= 0 \
                else stable_id("mention", surface, prefix="m_")
            prov = Provenance(source_refs=(nd.source,), extractor="llm.attr.synth.v1",
                              confidence=0.7, char_span=(s, en) if s >= 0 else None)
            m = EntityMention(mention_id=mid, doc_id=nd.doc_id, text=surface,
                              normalized=surface, entity_type=EntityType.OTHER,
                              char_start=s, char_end=en, confidence=0.7,
                              extractor="llm.attr.synth.v1", provenance=prov)
            by_surface[surface.casefold()] = m
            created.append(m)
            return m

        for a in attributes:
            ent_s = (a.get("entity") or "").strip()
            name = (a.get("name") or "").strip()
            value = (a.get("value") or "").strip()
            if not (ent_s and name and value):
                continue

            # value_type：独立枚举（v7 起 12 类含 DICT_REF/ENTITY_REF）；非法 → OTHER 兜底
            raw_vt = (a.get("value_type") or "").strip()
            try:
                vt = AttributeValueType(raw_vt) if raw_vt else AttributeValueType.OTHER
            except ValueError:
                vt = AttributeValueType.OTHER

            # v7：required 必录性（M1 对象模型借鉴）——字符串/布尔都接受，兜底 False
            raw_req = a.get("required")
            if isinstance(raw_req, bool):
                req = raw_req
            elif isinstance(raw_req, str):
                req = raw_req.strip().lower() in ("true", "1", "yes", "是", "必填", "必须")
            else:
                req = False

            evidence = (a.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None

            ent = get_entity(ent_s)
            prov = Provenance(source_refs=(nd.source,), extractor="llm.attr.v1",
                              confidence=0.85, char_span=ev_span_t)

            rid = edge_id(ent.mention_id, name, None, value)
            rel = Relation(
                relation_id=rid, doc_id=nd.doc_id, subject_ref=ent.mention_id,
                object_ref="", predicate=name, confidence=0.85, chunk_id=None,
                evidence_span=ev_span_t, evidence_text=evidence,
                extractor="llm.attr.v1", provenance=prov,
            )
            trip = Triplet(
                triplet_id=triplet_id(
                    entity_id(ent.entity_type.value, ent.normalized), name, value),
                subject=ent.normalized, subject_type=ent.entity_type,
                predicate=name, object=value, object_type=None,
                object_literal=True, value_type=vt, confidence=0.85,
                source_mentions=(ent.mention_id,),
                extractor="llm.attr.v1", provenance=prov,
                required=req,
            )
            rels.append(rel)
            trips.append(trip)
        return rels, trips, created

    def _rules_from_llm(self, rules, nd: NormalizedDocument) -> list[Rule]:
        """rules → Rule（业务规则 v3，extractor="llm.rule.v1"）。

        规则是「条件-动作/结论」的规范性知识，**不进入实体-边图谱**，作为
        ExtractionResult.rules 独立产出。惰性锚点：
          * rule_id = rule_id(subject, condition, action, modality) 内容寻址
          * modality 非法 → OTHER 兜底（不自创类型）
          * evidence 尽力定位，找不到为 None
          * action 缺失 → 跳过（契约要求 action 必有）
        """
        out: list[Rule] = []
        for r in rules:
            subject = (r.get("subject") or "").strip()
            condition = (r.get("condition") or "").strip()
            action = (r.get("action") or "").strip()
            if not action:
                continue
            raw_mod = (r.get("modality") or "").strip()
            try:
                mod = RuleModality(raw_mod) if raw_mod else RuleModality.OTHER
            except ValueError:
                mod = RuleModality.OTHER

            # v7：rule_type（用途，与 modality 正交）/ certainty（通用-企业分级）
            raw_rt = (r.get("rule_type") or "").strip()
            try:
                rtype = RuleType(raw_rt) if raw_rt else RuleType.OTHER
            except ValueError:
                rtype = RuleType.OTHER
            raw_cert = (r.get("certainty") or "").strip()
            try:
                cert = Certainty(raw_cert) if raw_cert else Certainty.OTHER
            except ValueError:
                cert = Certainty.OTHER
            # output_type：复用函数指标输出枚举；非法 → OTHER
            raw_ot = (r.get("output_type") or "").strip()
            try:
                otype = FunctionOutputType(raw_ot).value if raw_ot else ""
            except ValueError:
                otype = FunctionOutputType.OTHER.value
            # reused_by：字符串数组清洗（去空白、去空项）
            raw_rb = r.get("reused_by") or []
            reused_by = [str(x).strip() for x in raw_rb if str(x).strip()] \
                if isinstance(raw_rb, list) else \
                ([str(raw_rb).strip()] if str(raw_rb).strip() else [])

            evidence = (r.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None

            prov = Provenance(source_refs=(nd.source,), extractor="llm.rule.v1",
                              confidence=0.85, char_span=ev_span_t)
            rid = rule_id(subject, condition, action, mod.value)
            out.append(Rule(
                rule_id=rid, doc_id=nd.doc_id, subject=subject, condition=condition,
                action=action, modality=mod, evidence_span=ev_span_t,
                evidence_text=evidence, extractor="llm.rule.v1", confidence=0.85,
                provenance=prov, rule_type=rtype, output_type=otype,
                reused_by=reused_by, certainty=cert,
            ))
        return out

    def _processes_from_llm(self, processes, nd: NormalizedDocument) -> list[Process]:
        """processes → Process（业务流程 v4，extractor="llm.proc.v1"）。

        流程是「有序活动 + 控制流」，**不进入实体-边图谱**（是持续体 perdurant
        而非实体间事实），作为 ExtractionResult.processes 独立产出。惰性锚点：
          * process_id = process_id(name) 内容寻址
          * step_id / flow_id 内容寻址
          * kind 非法 → TASK 兜底；flow.type 非法 → SEQUENCE 兜底（不自创类型）
          * flows 的 from/to 越界（<0 或 ≥len(steps)）→ **丢弃该 flow**
            （LLM 下标不可信，Python 惰性校验）
          * steps[].label 缺失 → 跳过（契约要求 label 必有）
          * evidence 尽力定位，找不到为 None
        """
        out: list[Process] = []
        for p in processes:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            pid = process_id(name)
            description = (p.get("description") or "").strip()
            raw_steps = p.get("steps") or []
            raw_flows = p.get("flows") or []

            steps: list[ProcessStep] = []
            for i, st in enumerate(raw_steps):
                label = (st.get("label") or "").strip()
                if not label:
                    continue
                raw_kind = (st.get("kind") or "").strip()
                try:
                    kind = StepKind(raw_kind) if raw_kind else StepKind.TASK
                except ValueError:
                    kind = StepKind.TASK
                actor = (st.get("actor") or "").strip()
                # v7：子流程引用（M6 SUB_FLOW_CALL）——引用另一流程 name，可空
                sub_ref = (st.get("sub_process_ref") or "").strip()
                evidence = (st.get("evidence") or "").strip() or None
                ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
                ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
                prov = Provenance(source_refs=(nd.source,), extractor="llm.proc.v1",
                                  confidence=0.8, char_span=ev_span_t)
                steps.append(ProcessStep(
                    step_id=step_id(pid, i, label), process_id=pid, index=i,
                    label=label, kind=kind, actor=actor, evidence_span=ev_span_t,
                    evidence_text=evidence, extractor="llm.proc.v1",
                    confidence=0.8, provenance=prov, sub_process_ref=sub_ref,
                ))

            flows: list[ProcessFlow] = []
            n = len(steps)
            for f in raw_flows:
                try:
                    fi = int(f.get("from"))
                    ti = int(f.get("to"))
                except (TypeError, ValueError):
                    continue
                if fi < 0 or ti < 0 or fi >= n or ti >= n:
                    # 下标越界：LLM 引用不可信，丢弃该 flow（惰性校验）
                    continue
                raw_ft = (f.get("type") or "").strip()
                try:
                    ft = FlowType(raw_ft) if raw_ft else FlowType.SEQUENCE
                except ValueError:
                    ft = FlowType.SEQUENCE
                condition = (f.get("condition") or "").strip()
                evidence = (f.get("evidence") or "").strip() or None
                ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
                ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
                prov = Provenance(source_refs=(nd.source,), extractor="llm.proc.v1",
                                  confidence=0.8, char_span=ev_span_t)
                flows.append(ProcessFlow(
                    flow_id=flow_id(pid, fi, ti, ft.value, condition), process_id=pid,
                    from_index=fi, to_index=ti, type=ft, condition=condition,
                    evidence_span=ev_span_t, evidence_text=evidence,
                    extractor="llm.proc.v1", confidence=0.8, provenance=prov,
                ))

            if not steps:
                continue
            prov = Provenance(source_refs=(nd.source,), extractor="llm.proc.v1",
                              confidence=0.8)
            # v7：流程级前后置条件（M6 preconditions/postconditions）——字符串数组清洗
            def _str_list(v):
                if isinstance(v, list):
                    return [str(x).strip() for x in v if str(x).strip()]
                s = str(v or "").strip()
                return [s] if s else []
            out.append(Process(
                process_id=pid, doc_id=nd.doc_id, name=name, steps=steps, flows=flows,
                description=description, extractor="llm.proc.v1", confidence=0.8,
                provenance=prov,
                preconditions=_str_list(p.get("preconditions")),
                postconditions=_str_list(p.get("postconditions")),
            ))
        return out

    # ------------------------------------------------------------------ #
    # v6：预测决策本体六件套薄壳映射（均独立产出，不入实体-边图）            #
    # ------------------------------------------------------------------ #

    def _states_from_llm(self, states, nd: NormalizedDocument) -> list[StateMachine]:
        """states → StateMachine（状态机，v6，extractor="llm.sm.v1"）。

        **预测 = 对象在时间窗内从状态 A 演化到状态 B**（动态本体）。惰性锚点：
          * sm_id = state_machine_id(object, name) 内容寻址
          * state_id / transition_id 内容寻址
          * transitions 的 from/to 越界（<0 或 ≥len(states)）→ **丢弃该 transition**
            （LLM 下标不可信，同 flows 处理）
          * states[].label 缺失 → 跳过；无状态的状态机 → 丢弃
          * object/name 缺失 → 跳过
        """
        out: list[StateMachine] = []
        for sm in states:
            object_name = (sm.get("object") or "").strip()
            name = (sm.get("name") or "").strip()
            if not (object_name and name):
                continue
            sid = state_machine_id(object_name, name)
            raw_states = sm.get("states") or []
            raw_trans = sm.get("transitions") or []

            state_defs: list[StateDef] = []
            for i, st in enumerate(raw_states):
                label = (st.get("label") or "").strip()
                if not label:
                    continue
                initial = bool(st.get("initial") or False)
                evidence = (st.get("evidence") or "").strip() or None
                ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
                ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
                prov = Provenance(source_refs=(nd.source,), extractor="llm.sm.v1",
                                  confidence=0.8, char_span=ev_span_t)
                state_defs.append(StateDef(
                    state_id=state_id(sid, i, label), sm_id=sid, index=i,
                    label=label, initial=initial, evidence_span=ev_span_t,
                    evidence_text=evidence, extractor="llm.sm.v1",
                    confidence=0.8, provenance=prov,
                ))

            trans: list[TransitionDef] = []
            n = len(state_defs)
            for t in raw_trans:
                try:
                    fi = int(t.get("from"))
                    ti = int(t.get("to"))
                except (TypeError, ValueError):
                    continue
                if fi < 0 or ti < 0 or fi >= n or ti >= n:
                    continue  # 下标越界：丢弃（LLM 下标不可信）
                event = (t.get("event") or "").strip()
                condition = (t.get("condition") or "").strip()
                action = (t.get("action") or "").strip()
                evidence = (t.get("evidence") or "").strip() or None
                ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
                ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
                prov = Provenance(source_refs=(nd.source,), extractor="llm.sm.v1",
                                  confidence=0.8, char_span=ev_span_t)
                trans.append(TransitionDef(
                    transition_id=transition_id(sid, fi, ti, event, condition),
                    sm_id=sid, from_index=fi, to_index=ti, event=event,
                    condition=condition, action=action, evidence_span=ev_span_t,
                    evidence_text=evidence, extractor="llm.sm.v1",
                    confidence=0.8, provenance=prov,
                ))

            if not state_defs:
                continue
            prov = Provenance(source_refs=(nd.source,), extractor="llm.sm.v1",
                              confidence=0.8)
            out.append(StateMachine(
                sm_id=sid, doc_id=nd.doc_id, object=object_name, name=name,
                states=state_defs, transitions=trans, extractor="llm.sm.v1",
                confidence=0.8, provenance=prov,
            ))
        return out

    def _functions_from_llm(self, functions, nd: NormalizedDocument) -> list[Function]:
        """functions → Function（函数指标，v6，extractor="llm.fn.v1"）。

        把业务经验写成可计算定义（**推演**的输入）。formula 声明式描述即可，
        不强制可执行数学表达式。output_type 独立枚举，非法 → OTHER 兜底。
        """
        out: list[Function] = []
        for f in functions:
            name = (f.get("name") or "").strip()
            formula = (f.get("formula") or "").strip()
            if not (name and formula):
                continue
            subject = (f.get("subject") or "").strip()
            inputs = [str(x).strip() for x in (f.get("inputs") or []) if str(x).strip()]
            raw_ot = (f.get("output_type") or "").strip()
            try:
                ot = FunctionOutputType(raw_ot) if raw_ot else FunctionOutputType.OTHER
            except ValueError:
                ot = FunctionOutputType.OTHER
            evidence = (f.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
            prov = Provenance(source_refs=(nd.source,), extractor="llm.fn.v1",
                              confidence=0.85, char_span=ev_span_t)
            fid = function_id(name, subject, formula)
            out.append(Function(
                function_id=fid, doc_id=nd.doc_id, name=name, formula=formula,
                subject=subject, inputs=inputs, output_type=ot,
                evidence_span=ev_span_t, evidence_text=evidence,
                extractor="llm.fn.v1", confidence=0.85, provenance=prov,
            ))
        return out

    def _temporal_from_llm(self, temporal, nd: NormalizedDocument) -> list[Temporal]:
        """temporal → Temporal（时序时态，v6，extractor="llm.tp.v1"）。

        把时间限定绑定到某事实/规则/流程/状态。kind 独立枚举，非法 →
        VALIDITY 兜底。与 DATE 实体分界：DATE 是实体（图中节点），temporal
        是把时间语义绑定到主体。
        """
        out: list[Temporal] = []
        for t in temporal:
            subject = (t.get("subject") or "").strip()
            value = (t.get("value") or "").strip()
            if not (subject and value):
                continue
            raw_kind = (t.get("kind") or "").strip()
            try:
                kind = TemporalKind(raw_kind) if raw_kind else TemporalKind.VALIDITY
            except ValueError:
                kind = TemporalKind.VALIDITY
            anchor = (t.get("anchor") or "").strip()
            evidence = (t.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
            prov = Provenance(source_refs=(nd.source,), extractor="llm.tp.v1",
                              confidence=0.85, char_span=ev_span_t)
            tid = temporal_id(subject, kind.value, value)
            out.append(Temporal(
                temporal_id=tid, doc_id=nd.doc_id, subject=subject, kind=kind,
                value=value, anchor=anchor, evidence_span=ev_span_t,
                evidence_text=evidence, extractor="llm.tp.v1",
                confidence=0.85, provenance=prov,
            ))
        return out

    def _actions_from_llm(self, actions, nd: NormalizedDocument) -> list[Action]:
        """actions → Action（动作处置，v6，extractor="llm.ac.v1"）。

        处置动作的元信息（怎么做/什么级别/什么代价/何时触发），供决策执行。
        level 独立 3 类枚举，非法 → OBSERVE 兜底。
        """
        out: list[Action] = []
        for a in actions:
            name = (a.get("name") or "").strip()
            if not name:
                continue
            raw_level = (a.get("level") or "").strip()
            try:
                level = ActionLevel(raw_level) if raw_level else ActionLevel.OBSERVE
            except ValueError:
                level = ActionLevel.OBSERVE
            actor = (a.get("actor") or "").strip()
            target = (a.get("target") or "").strip()
            side_effect = (a.get("side_effect") or "").strip()
            trigger = (a.get("trigger") or "").strip()
            # v7：动作前后置条件（M2 行为模型借鉴），可空
            precondition = (a.get("precondition") or "").strip()
            postcondition = (a.get("postcondition") or "").strip()
            evidence = (a.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
            prov = Provenance(source_refs=(nd.source,), extractor="llm.ac.v1",
                              confidence=0.85, char_span=ev_span_t)
            aid = action_id(name, actor, level.value, trigger)
            out.append(Action(
                action_id=aid, doc_id=nd.doc_id, name=name, level=level,
                actor=actor, target=target, side_effect=side_effect,
                trigger=trigger, evidence_span=ev_span_t, evidence_text=evidence,
                extractor="llm.ac.v1", confidence=0.85, provenance=prov,
                precondition=precondition, postcondition=postcondition,
            ))
        return out

    def _constraints_from_llm(self, constraints, nd: NormalizedDocument) -> list[Constraint]:
        """constraints → Constraint（结构约束，v6，extractor="llm.cn.v1"）。

        结构合法性约束（SHACL 式），与 rules 的业务规范分界：rules 是「该做
        什么」的规范（道义模态），constraints 是「什么合法」的结构约束。
        type 独立 6 类枚举，非法 → CONSISTENCY 兜底。
        """
        out: list[Constraint] = []
        for c in constraints:
            subject = (c.get("subject") or "").strip()
            description = (c.get("description") or "").strip()
            if not (subject and description):
                continue
            raw_type = (c.get("type") or "").strip()
            try:
                ctype = ConstraintType(raw_type) if raw_type else ConstraintType.CONSISTENCY
            except ValueError:
                ctype = ConstraintType.CONSISTENCY
            evidence = (c.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
            prov = Provenance(source_refs=(nd.source,), extractor="llm.cn.v1",
                              confidence=0.85, char_span=ev_span_t)
            cid = constraint_id(subject, ctype.value, description)
            out.append(Constraint(
                constraint_id=cid, doc_id=nd.doc_id, subject=subject,
                type=ctype, description=description, evidence_span=ev_span_t,
                evidence_text=evidence, extractor="llm.cn.v1",
                confidence=0.85, provenance=prov,
            ))
        return out

    def _permissions_from_llm(self, permissions, nd: NormalizedDocument) -> list[Permission]:
        """permissions → Permission（主体-动作授权，v6，extractor="llm.pm.v1"）。

        RBAC 授权边界（决策自动化治理层）。effect 独立 2 类枚举，非法 →
        DENY 兜底（默认禁止，最小权限原则）。
        """
        out: list[Permission] = []
        for p in permissions:
            actor = (p.get("actor") or "").strip()
            action = (p.get("action") or "").strip()
            if not (actor and action):
                continue
            raw_effect = (p.get("effect") or "").strip()
            try:
                effect = PermissionEffect(raw_effect) if raw_effect else PermissionEffect.DENY
            except ValueError:
                effect = PermissionEffect.DENY
            scope = (p.get("scope") or "").strip()
            # v7：角色中间层（M5 主体模型）+ 主体类型（HUMAN/SYSTEM）
            role = (p.get("role") or "").strip()
            raw_at = (p.get("actor_type") or "").strip()
            try:
                atype = ActorType(raw_at) if raw_at else ActorType.OTHER
            except ValueError:
                atype = ActorType.OTHER
            evidence = (p.get("evidence") or "").strip() or None
            ev_span = _locate_span(nd.text, evidence) if evidence else (-1, -1)
            ev_span_t = (ev_span[0], ev_span[1]) if ev_span[0] >= 0 else None
            prov = Provenance(source_refs=(nd.source,), extractor="llm.pm.v1",
                              confidence=0.85, char_span=ev_span_t)
            pid = permission_id(actor, action, effect.value)
            out.append(Permission(
                permission_id=pid, doc_id=nd.doc_id, actor=actor, action=action,
                effect=effect, scope=scope, evidence_span=ev_span_t,
                evidence_text=evidence, extractor="llm.pm.v1",
                confidence=0.85, provenance=prov, role=role, actor_type=atype,
            ))
        return out

    def _simple_chunks(self, nd: NormalizedDocument, mentions: list[EntityMention]) -> list[Chunk]:
        """零规则：不再做 entity-aware 切分，产出单个覆盖全文的 chunk。

        上下文长度由调用方在送 LLM 前自行控制；chunk 仅用于统计/溯源。
        """
        cs, ce = 0, len(nd.text)
        mids = tuple(m.mention_id for m in mentions
                     if m.char_start >= cs and m.char_end <= ce)
        return [Chunk(
            chunk_id=chunk_id(nd.doc_id, cs, ce), doc_id=nd.doc_id, text=nd.text,
            char_start=cs, char_end=ce, index=0, mention_ids=mids,
            token_estimate=ce // 2,
        )]

    def _empty_result(self, nd: NormalizedDocument, kind: DegradationKind,
                      reason: str) -> ExtractionResult:
        return ExtractionResult(
            doc_id=nd.doc_id,
            chunks=self._simple_chunks(nd, []),
            degradations=[Degradation(
                component="extract.llm", kind=kind, reason=reason,
                fallback="empty", impact="零规则架构无确定性兜底，未抽取任何事实",
            )],
            stats={"mode": "empty", "chunks": 1, "mentions": 0,
                   "relations": 0, "triplets": 0},
        )

    def commit(self, state: PipelineState, output: list[ExtractionResult]) -> None:
        state.extractions = output


def _locate_span(text: str, surface: str) -> tuple[int, int]:
    """在文本中定位 surface 的字符区间；找不到返回 (-1, -1)。"""
    if not surface:
        return (-1, -1)
    i = text.find(surface)
    if i >= 0:
        return (i, i + len(surface))
    low = text.lower()
    ls = surface.lower()
    i = low.find(ls)
    if i >= 0:
        return (i, i + len(ls))
    return (-1, -1)


def _dedup(items, key_fn):
    """按 key_fn 内容寻址去重（v6 六件套通用），保持首次出现顺序。"""
    seen: set = set()
    out = []
    for it in items:
        k = key_fn(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out
