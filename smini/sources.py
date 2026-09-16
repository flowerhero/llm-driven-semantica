"""smini.sources — 源读取原语（extract-only 流水线的输入层）。

唯一职责：**原样搬运，不做任何解读**。
拿到字节 → 算 sha256 校验和 → 记下来源 SourceRef。

数据源说明（source spec）
------------------------
  * ``text://<内容>``            内存字符串，便于测试与 API 直投
  * ``file:///绝对路径``         读本地文件字节
  * 形如路径且存在的字符串        当作 file://
  * 其它字符串                   当作 text://（inline）

Web / DB / Stream 源不支持，显式抛错而非静默降级。

> 设计立场（对齐 `skills/smini-extract/SKILL.md`）：**摄入/归一化外包给宿主
> agent** —— 宿主用自身工具读文件/网页/PDF 后，要么直接按契约交卷，要么把
> 文本经 ``text://`` 投喂本模块。Python 只保留「字节 + 校验和 + 来源」的
> 确定性原语，供 CLI 与编排读取。
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any, Sequence

from .ids import content_hash, doc_id
from .types import (
    DocumentFormat,
    RawDocument,
    SourceRef,
    SourceType,
    StepError,
)


def _format_from_path(path: str) -> DocumentFormat:
    """从文件扩展名推断格式。未知扩展名给 UNKNOWN（供调用方决定处理）。"""
    suffix = Path(path).suffix.lower().lstrip(".")
    mapping = {
        "pdf": DocumentFormat.PDF,
        "docx": DocumentFormat.DOCX,
        "pptx": DocumentFormat.PPTX,
        "xlsx": DocumentFormat.XLSX,
        "html": DocumentFormat.HTML,
        "htm": DocumentFormat.HTML,
        "md": DocumentFormat.MARKDOWN,
        "markdown": DocumentFormat.MARKDOWN,
        "txt": DocumentFormat.TEXT,
        "text": DocumentFormat.TEXT,
        "csv": DocumentFormat.CSV,
        "json": DocumentFormat.JSON,
        "xml": DocumentFormat.XML,
        "epub": DocumentFormat.EPUB,
        "py": DocumentFormat.CODE,
        "js": DocumentFormat.CODE,
        "ts": DocumentFormat.CODE,
        "java": DocumentFormat.CODE,
        "go": DocumentFormat.CODE,
        "rs": DocumentFormat.CODE,
        "cpp": DocumentFormat.CODE,
        "c": DocumentFormat.CODE,
    }
    return mapping.get(suffix, DocumentFormat.UNKNOWN)


def resolve_source(spec: str, *, fetched_at: Any = None) -> RawDocument:
    """把一个 source spec 解析成一个 RawDocument（仅持有字节）。"""
    if spec.startswith("file://"):
        path = spec[len("file://"):]
        return _read_file(path)
    if spec.startswith("text://"):
        raw = urllib.parse.unquote(spec[len("text://"):])
        return _from_text(raw)
    if spec.startswith(("http://", "https://", "ftp://")):
        raise StepError(
            "ingest",
            f"Web 源 {spec!r} 不支持（需联网能力）；"
            f"请先下载为本地文件再用 file:// 投喂",
        )
    # 形如路径且存在 → 当文件；否则当 inline 文本
    p = Path(spec)
    if p.exists() and p.is_file():
        return _read_file(str(p.resolve()))
    return _from_text(spec)


def raw_from_sources(sources: Sequence[str]) -> list[RawDocument]:
    """把 source spec 列表统一解析为 RawDocument 列表（编排层输入层）。"""
    return [resolve_source(s) for s in sources]


def _read_file(path: str) -> RawDocument:
    data = Path(path).read_bytes()
    uri = f"file://{Path(path).resolve()}"
    source = SourceRef.of(uri, SourceType.FILE, checksum=content_hash(data))
    did = doc_id(uri, data)
    return RawDocument(
        doc_id=did,
        source=source,
        media_type="application/octet-stream",
        content=data,
        size_bytes=len(data),
        checksum=source.checksum or content_hash(data),
        fetched_at=source.fetched_at,  # type: ignore[arg-type]
        metadata={"path": str(Path(path).resolve())},
    )


def _from_text(text: str) -> RawDocument:
    data = text.encode("utf-8")
    # 内存源的 uri 用 content 的 sha256 前缀自解释，便于去重
    uri = f"memory://inline/{content_hash(data)[:16]}"
    source = SourceRef.of(uri, SourceType.TEXT, checksum=content_hash(data))
    return RawDocument(
        doc_id=doc_id(uri, data),
        source=source,
        media_type="text/plain",
        content=data,
        size_bytes=len(data),
        checksum=source.checksum or content_hash(data),
        fetched_at=source.fetched_at,  # type: ignore[arg-type]
        metadata={"inline": True},
    )
