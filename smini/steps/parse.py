"""smini.steps.parse — Step 2 · Parse

把一种格式变成**统一的「文本 + 区块序列」**。关键不变量：每个 Block 的
``char_start``/``char_end`` 锚定在 ``ParsedDocument.text`` 上，且
``block.text == text[char_start:char_end]`` —— 这是整条链路偏移回溯的第一根桩。

v1 支持：**text / markdown / code / html(粗去标签) / csv / json / xml**。
pdf / docx / pptx / xlsx / epub / image / audio 在 v1 没有解析后端，
**显式降级**（记 warning，不静默造假内容）—— 这相对原版「猜错编码就全失真」
是刻意的修正。
"""

from __future__ import annotations

import re

from ..ids import stable_id
from ..llm import PARSE_SCHEMA, build_parse_prompt
from ..protocols import PipelineState, PipelineStep, RunContext
from ..types import (
    Block,
    BlockKind,
    Degradation,
    DegradationKind,
    DependencyMissing,
    DocumentFormat,
    ParsedDocument,
    SourceRef,
    StepError,
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE = re.compile(r"^([-*+]|\d+[.)])\s+(.*)$")
_QUOTE_RE = re.compile(r"^>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")


def _decode(content: bytes, fmt: DocumentFormat) -> tuple[str, str, list[str]]:
    """解码字节为文本。返回 (text, encoding, warnings)。"""
    warnings: list[str] = []
    if fmt in (
        DocumentFormat.MARKDOWN,
        DocumentFormat.TEXT,
        DocumentFormat.CODE,
        DocumentFormat.CSV,
        DocumentFormat.JSON,
        DocumentFormat.XML,
        DocumentFormat.HTML,
    ):
        encodings = ["utf-8", "utf-8-sig", "gb18030", "latin-1"]
    else:
        # 二进制格式不在此解码，交给下面的降级分支
        encodings = []

    for enc in encodings:
        try:
            return content.decode(enc), enc, warnings
        except UnicodeDecodeError:
            continue
    # 兜底：替换非法字节，宁可带乱码也不要崩
    text = content.decode("utf-8", errors="replace")
    warnings.append("utf-8 解码失败，已用 replacement 字符兜底（内容可能含乱码）")
    return text, "utf-8-replace", warnings


def _strip_html(text: str) -> str:
    """极简 HTML 去标签：删 <script>/<style> 内容 + 去其余标签 + 还原实体。"""
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    return re.sub(r"[ \t]+", " ", text)


def _parse_blocks(text: str, doc_id: str, fmt: DocumentFormat) -> tuple[list[Block], list[str]]:
    """把完整文本切成 Block 序列，span 锚定在 text 上。"""
    warnings: list[str] = []
    blocks: list[Block] = []
    order = 0

    def add(kind: BlockKind, seg: str, start: int, end: int, **kw: object) -> None:
        nonlocal order
        blocks.append(
            Block(
                block_id=stable_id("block", doc_id, order, prefix="b_"),
                kind=kind,
                text=seg,
                order=order,
                char_start=start,
                char_end=end,
                **kw,
            )
        )
        order += 1

    if fmt is DocumentFormat.HTML:
        text = _strip_html(text)

    lines = text.split("\n")
    n = len(lines)
    pos = 0  # 当前行首在 text 中的偏移
    para_start: int | None = None
    para_end: int = 0

    def flush_para() -> None:
        nonlocal para_start, para_end
        if para_start is not None:
            seg = text[para_start:para_end]
            if seg.strip():
                add(BlockKind.PARAGRAPH, seg, para_start, para_end)
            para_start = None

    i = 0
    while i < n:
        line = lines[i]
        line_start = pos
        line_end = pos + len(line)
        stripped = line.strip()

        if stripped == "":
            flush_para()
            pos = line_end + 1
            i += 1
            continue

        # 代码围栏（仅 markdown）
        if fmt is DocumentFormat.MARKDOWN and stripped.startswith("```"):
            flush_para()
            lang = stripped[3:].strip() or None
            j = i + 1
            fence_lines: list[str] = []
            while j < n and not lines[j].strip().startswith("```"):
                fence_lines.append(lines[j])
                j += 1
            content = "\n".join(fence_lines)
            cstart = line_end + 1
            cend = cstart + len(content)
            add(BlockKind.CODE, content, cstart, cend, language=lang)
            i = j + 1 if j < n else n
            # 结束围栏行本身也要计入偏移：cend + 1(换行) + len(```) + 1(换行)。
            # 原实现少算 1，代码块后紧跟正文时整体偏移并截断；
            # 若代码块后有空行，空行的 line_end+1 会碰巧把偏移"修正"回来，
            # 故只在无空行时暴露。
            pos = (cend + len(lines[j]) + 2) if j < n else cend
            continue

        # 表格（markdown）
        if fmt is DocumentFormat.MARKDOWN and _TABLE_ROW_RE.match(line) and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            rows: list[list[str]] = []
            k = i
            while k < n and _TABLE_ROW_RE.match(lines[k]):
                cells = [c.strip() for c in _TABLE_ROW_RE.match(lines[k]).group(1).split("|")]  # type: ignore[union-attr]
                rows.append(cells)
                k += 1
            # 表格占 i..k-1 行：逐行累加（每行末尾 +1 换行符，最后一行不计）。
            # 原实现用未随 k 递增的 pos 参与计算，导致 tend 偏小 —— 表格后半段
            # 被切成残片段落。因为 block.text 取自 text[tstart:tend]，切片自洽，
            # 校验查不出来，是典型的静默错误。
            tstart = line_start
            tend = line_start + sum(len(lines[m]) + 1 for m in range(i, k)) - 1
            add(BlockKind.TABLE, text[tstart:tend], tstart, tend, rows=rows)
            i = k
            pos = tend + 1
            continue

        if fmt is DocumentFormat.MARKDOWN:
            m = _HEADING_RE.match(line)
            if m:
                flush_para()
                level = min(len(m.group(1)), 6)
                content = m.group(2)
                # 用捕获组的真实起始定位，不能用 m.end(1)+1 —— 后者假设
                # 「# 与标题之间恰好 1 个分隔符」，多空格/tab 时会整体偏移
                cstart = line_start + m.start(2)
                add(BlockKind.HEADING, content, cstart, line_end, level=level)
                pos = line_end + 1
                i += 1
                continue
            m = _LIST_RE.match(line)
            if m:
                flush_para()
                content = m.group(2)
                cstart = line_start + m.start(2)
                add(BlockKind.LIST_ITEM, content, cstart, line_end)
                pos = line_end + 1
                i += 1
                continue
            m = _QUOTE_RE.match(line)
            if m:
                flush_para()
                content = m.group(1)
                # 正则的 \s? 会吃掉 ">" 后的空格，故必须按 group(1) 的
                # 实际起始定位；写死 +1 会让 span 与 block.text 错开一位
                cstart = line_start + m.start(1)
                add(BlockKind.QUOTE, content, cstart, line_end)
                pos = line_end + 1
                i += 1
                continue

        # 普通段落行：累加（直到空行或特殊行）
        if para_start is None:
            para_start = line_start
        para_end = line_end
        pos = line_end + 1
        i += 1

    flush_para()
    if not blocks:
        warnings.append("文档解析后无任何区块（可能为空）")
    return blocks, warnings


def _locate_block_span(text: str, block_text: str) -> tuple[int, int]:
    """在解析文本中定位区块文本的字符区间；找不到返回 (-1, -1)。"""
    if not block_text:
        return (-1, -1)
    i = text.find(block_text)
    if i >= 0:
        return (i, i + len(block_text))
    return (-1, -1)


class ParseStep(PipelineStep[list, list[ParsedDocument]]):
    """把 RawDocument 列表解析成 ParsedDocument 列表。"""

    name = "parse"
    reads = ("raw",)
    writes = ("parsed",)

    def select(self, state: PipelineState):
        return state.raw

    def transform(self, raws: list, ctx: RunContext) -> list[ParsedDocument]:
        out: list[ParsedDocument] = []
        for rd in raws:
            fmt = self._detect_format(rd, ctx)
            if fmt in (
                DocumentFormat.PDF,
                DocumentFormat.DOCX,
                DocumentFormat.PPTX,
                DocumentFormat.XLSX,
                DocumentFormat.EPUB,
                DocumentFormat.IMAGE,
                DocumentFormat.AUDIO,
            ):
                # 裸字节格式：LLM 也读不了，确定性占位（显式降级）
                pd = ParsedDocument(
                    doc_id=rd.doc_id,
                    format=fmt,
                    source=rd.source,
                    text=f"[格式 {fmt.value} 在 v1 不支持解析，仅记录原始字节 {rd.size_bytes}B]",
                    parser="unsupported",
                    warnings=[f"格式 {fmt.value} 在 v1 无解析后端，已降级为占位文本"],
                )
                ctx.log("warning", f"parse: {rd.doc_id} 格式 {fmt.value} 不支持，降级")
                out.append(pd)
                continue

            decoded, enc, warns = _decode(rd.content, fmt)
            if enc != "utf-8":
                warns.append(f"编码推断为 {enc}（非 utf-8）")

            llm = ctx.llm
            if llm is not None and llm.is_available():
                try:
                    out.append(self._parse_with_llm(decoded, rd, fmt, ctx))
                    continue
                except (DependencyMissing, StepError, KeyError, ValueError) as exc:
                    ctx.log("warning", f"parse: LLM 解析失败，回退确定性：{exc}")
                    # 落到下面的确定性路径，并登记降级
            # 确定性兜底
            blocks, bwarns = _parse_blocks(decoded, rd.doc_id, fmt)
            warns.extend(bwarns)
            warns = list(warns)
            if llm is not None:
                warns.append("LLM 解析不可用，已用确定性解析（smini.line.v1）")
            out.append(
                ParsedDocument(
                    doc_id=rd.doc_id,
                    format=fmt,
                    source=rd.source,
                    text=decoded,
                    blocks=blocks,
                    parser="smini.line.v1",
                    parser_version="1.0",
                    warnings=warns,
                )
            )
        return out

    def _parse_with_llm(self, decoded: str, rd: object, fmt: DocumentFormat,
                        ctx: RunContext) -> ParsedDocument:
        """LLM 抽结构化文本 + 版式。含长度保真校验，偏差过大回退确定性。"""
        data = ctx.llm.extract(build_parse_prompt(decoded, fmt.value), PARSE_SCHEMA)  # type: ignore[union-attr]
        text = data.get("text", "") or ""
        blocks_raw = data.get("blocks", []) or []
        # 保真：LLM 绝不能增删原文（否则下游偏移全乱）
        if abs(len(text) - len(decoded)) > max(8, int(0.05 * len(decoded))):
            raise StepError(
                "parse",
                f"LLM 解析文本长度偏差过大（{len(text)} vs 原文 {len(decoded)}）",
            )
        order = 0
        blocks: list[Block] = []
        for b in blocks_raw:
            try:
                kind = BlockKind(b.get("kind", "paragraph"))
            except ValueError:
                kind = BlockKind.PARAGRAPH
            bt = b.get("text", "") or ""
            s, e = _locate_block_span(text, bt)
            blocks.append(Block(
                block_id=stable_id("block", rd.doc_id, order, prefix="b_"),
                kind=kind, text=bt, order=order, char_start=s, char_end=e,
                level=b.get("level"), rows=b.get("rows"), language=b.get("language"),
            ))
            order += 1
        return ParsedDocument(
            doc_id=rd.doc_id, format=fmt, source=rd.source, text=text, blocks=blocks,
            parser="llm.v1", parser_version="1.0",
            warnings=["LLM 结构化解析（保真校验通过）"],
        )

    @staticmethod
    def _detect_format(rd: "object", ctx: RunContext) -> DocumentFormat:
        from pathlib import Path
        from .ingest import _format_from_path

        uri = getattr(getattr(rd, "source", None), "uri", "")
        if uri.startswith("file://"):
            suf = Path(uri[len("file://"):]).suffix.lower().lstrip(".")
            return _format_from_path(("x." + suf) if suf else "x")
        if uri.startswith("memory://"):
            return DocumentFormat.TEXT
        mt = (getattr(rd, "media_type", "") or "").lower()
        if "markdown" in mt:
            return DocumentFormat.MARKDOWN
        if "html" in mt:
            return DocumentFormat.HTML
        if "json" in mt:
            return DocumentFormat.JSON
        if "xml" in mt:
            return DocumentFormat.XML
        if "csv" in mt:
            return DocumentFormat.CSV
        return DocumentFormat.TEXT

    def commit(self, state: PipelineState, output: list[ParsedDocument]) -> None:
        state.parsed = output
