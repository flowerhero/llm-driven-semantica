"""smini.steps.normalize — Step 3 · Normalize

清洗文本并记录全部坐标漂移（SpanPatch），让后续 Extract 给出的 span 能
逆向映射回原文。同时**顺手**用正则识别规则实体（日期/金额/百分比/数量），
作为 Extract 步 entity-aware 切分的依据。

清洗项（v1）：
  * Unicode NFC 组合（é 等）
  * 控制字符剥离（\\x00-\\x08, \\x0b, \\x0c, \\x0e-\\x1f）
  * 连续空格折叠为单空格
  * 日期/金额的规范形式归一（仅改 canonical，不改原文表面）

SpanPatch 构造严格满足 ``validate_patches`` 的几何约束（按 new_start 升序、
区间不重叠、new_start == orig_start + 累积漂移），否则会立刻 ContractViolation。
"""

from __future__ import annotations

import re
import unicodedata

from ..ids import mention_id
from ..protocols import PipelineState, PipelineStep, RunContext
from ..types import (
    EntityMention,
    EntityType,
    NormalizedDocument,
    ParsedDocument,
    Provenance,
    SpanPatch,
    validate_patches,
)


def _forward_offset(offset: int, patches: list[SpanPatch], *, at_end: bool = False) -> int:
    """orig 坐标系 → norm 坐标系（SpanPatch 的反向映射）。"""
    result = offset
    for p in patches:
        if p.orig_end <= offset:
            result += p.delta
        elif p.orig_start <= offset < p.orig_end:
            result = p.new_end if at_end else p.new_start
            break
        else:
            break
    return max(result, 0)


def _normalize_text(text: str) -> tuple[str, list[SpanPatch]]:
    """逐字符扫描，产出归一化文本与 SpanPatch 列表。"""
    parts: list[str] = []
    patches: list[SpanPatch] = []
    i = 0
    n = len(text)
    orig_pos = 0  # 当前字符在 orig 文本中的偏移
    new_pos = 0   # 当前在 norm 文本中的偏移
    cum = 0       # 累积漂移

    def emit(orig_s: int, orig_e: int, replacement: str, kind: str, original: str) -> None:
        nonlocal new_pos, cum
        new_s = new_pos  # == orig_s + cum（passthrough 保持同步）
        new_e = new_s + len(replacement)
        patches.append(SpanPatch(
            orig_start=orig_s, orig_end=orig_e,
            new_start=new_s, new_end=new_e,
            kind=kind, original=original, replacement=replacement,
        ))
        parts.append(replacement)
        new_pos = new_e
        cum += (new_e - new_s) - (orig_e - orig_s)

    while i < n:
        c = text[i]
        # 控制字符（保留 \n 与 \t）
        if ord(c) < 0x20 and c not in ("\n", "\t"):
            emit(i, i + 1, "", "control", c)
            i += 1
            continue
        # 连续空格折叠
        if c == " ":
            j = i
            while j < n and text[j] == " ":
                j += 1
            if j - i > 1:
                emit(i, j, " ", "whitespace", text[i:j])
                i = j
                continue
            parts.append(" ")
            i += 1
            new_pos += 1
            orig_pos = i
            continue
        # NFC
        nf = unicodedata.normalize("NFC", c)
        if len(nf) != 1 or nf != c:
            emit(i, i + 1, nf, "unicode_nfc", c)
            i += 1
            continue
        parts.append(c)
        i += 1
        new_pos += 1
        orig_pos = i

    norm = "".join(parts)
    validate_patches(patches)
    return norm, patches


# ----------------------------- 规则实体 -----------------------------------

_DATE_RE = re.compile(
    r"\d{4}年\d{1,2}月(?:\d{1,2}日?)?"
    r"|\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?"
    r"|\d{1,2}月\d{1,2}日?"
    r"|\d{4}年|\d{1,2}月"
)
_MONEY_RE = re.compile(
    r"[¥$€£]\s?\d[\d,]*(?:\.\d+)?\s*(?:万|亿)?\s*(?:元|美元|欧元|英镑)?"
    r"|\d[\d,]*(?:\.\d+)?\s*(?:万|亿)?\s*(?:元|美元|欧元|英镑)"
)
_PERCENT_RE = re.compile(r"\d+(?:\.\d+)?%")
_QUANTITY_RE = re.compile(
    r"\d[\d,]*(?:\.\d+)?\s?(?:kg|千克|公里|km|米|吨|升|毫升|平方米|平方厘米|个|摄氏度|°C|度)"
)

_RULES = [
    (EntityType.DATE, _DATE_RE, "rule.date.v1", lambda s: s.replace("年", "-").replace("月", "-").replace("日", "")),
    (EntityType.MONEY, _MONEY_RE, "rule.money.v1", None),
    (EntityType.PERCENT, _PERCENT_RE, "rule.percent.v1", lambda s: s.rstrip("%")),
    (EntityType.QUANTITY, _QUANTITY_RE, "rule.quantity.v1", None),
]


def _normalize_value(etype: EntityType, raw: str) -> str:
    for t, _, _, fn in _RULES:
        if t is etype and fn is not None:
            return fn(raw)
    return raw


def _extract_rule_mentions(
    text: str, doc_id: str, source_ref: object
) -> list[EntityMention]:
    """在 norm 文本上抽取规则实体。span 落在 norm 坐标系。"""
    raw_hits: list[tuple[int, int, EntityType, str, str]] = []
    for etype, rx, extractor, _ in _RULES:
        for m in rx.finditer(text):
            raw_hits.append((m.start(), m.end(), etype, m.group(0), extractor))
    # 解决重叠：按 (start, -length) 排序，长优先，跳过被覆盖者
    raw_hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    mentions: list[EntityMention] = []
    covered: list[tuple[int, int]] = []
    for s, e, etype, surface, extractor in raw_hits:
        if any(cs < e and ce > s for cs, ce in covered):
            continue
        covered.append((s, e))
        normalized = _normalize_value(etype, surface)
        prov = Provenance(
            source_refs=(source_ref,),  # type: ignore[arg-type]
            extractor=extractor,
            confidence=1.0,
            char_span=(s, e),
        )
        mentions.append(EntityMention(
            mention_id=mention_id(doc_id, s, e, surface),
            doc_id=doc_id,
            text=surface,
            normalized=normalized,
            entity_type=etype,
            char_start=s,
            char_end=e,
            confidence=1.0,
            extractor=extractor,
            provenance=prov,
        ))
    return mentions


class NormalizeStep(PipelineStep[list, list[NormalizedDocument]]):
    """把 ParsedDocument 列表清洗成 NormalizedDocument 列表。"""

    name = "normalize"
    reads = ("parsed",)
    writes = ("normalized",)

    def select(self, state: PipelineState):
        return state.parsed

    def transform(self, parsed_list: list, ctx: RunContext) -> list[NormalizedDocument]:
        out: list[NormalizedDocument] = []
        for pd in parsed_list:
            if pd.parser == "unsupported":
                # 解析阶段已降级：原样透传，避免二次噪声
                out.append(NormalizedDocument(
                    doc_id=pd.doc_id, source=pd.source, text=pd.text,
                    blocks=pd.blocks, patches=[], mentions=[],
                    warnings=pd.warnings,
                ))
                continue
            norm, patches = _normalize_text(pd.text)
            # 把 parsed 的 block span 重新映射到 norm 坐标系
            new_blocks = []
            for b in pd.blocks:
                ns = _forward_offset(b.char_start, patches, at_end=False)
                ne = _forward_offset(b.char_end, patches, at_end=True)
                if 0 <= ns < ne <= len(norm):
                    nb_text = norm[ns:ne]
                else:
                    # 区块 span 无效（如 LLM 解析未给可靠偏移）→ 保留原样，
                    # 不拿错位的 norm 切片覆盖，避免破坏版式文本。
                    ns, ne = b.char_start, b.char_end
                    nb_text = b.text
                new_blocks.append(b.__class__(
                    block_id=b.block_id, kind=b.kind, text=nb_text,
                    order=b.order, char_start=ns, char_end=ne,
                    level=b.level, rows=b.rows, language=b.language,
                    metadata=b.metadata,
                ))
            # 规则实体：仅当没有 LLM 接管抽取时才做（LLM 路径会覆盖
            # DATE/MONEY/PERCENT/QUANTITY 等类型化实体，避免重复）。
            llm_on = bool(ctx.llm and ctx.llm.is_available())
            mentions = [] if llm_on else _extract_rule_mentions(norm, pd.doc_id, pd.source)
            out.append(NormalizedDocument(
                doc_id=pd.doc_id, source=pd.source, text=norm,
                blocks=new_blocks, patches=patches, mentions=mentions,
                warnings=pd.warnings,
            ))
            ctx.log("info", f"normalize: {pd.doc_id} → {len(patches)} patches, "
                             f"{len(mentions)} rule mentions")
        return out

    def commit(self, state: PipelineState, output: list[NormalizedDocument]) -> None:
        state.normalized = output
