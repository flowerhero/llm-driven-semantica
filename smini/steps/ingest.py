"""smini.steps.ingest — Step 1 · Ingest

唯一职责：**原样搬运，不做任何解读**。
拿到字节 → 算 sha256 校验和 → 记下来源 SourceRef。编码推断推迟到 Parse。

数据源说明（source spec）
------------------------
  * ``text://<内容>``            内存字符串，便于测试与 API 直投
  * ``file:///绝对路径``         读本地文件字节
  * 形如路径且存在的字符串        当作 file://
  * 其它字符串                   当作 text://（inline）

Web / DB / Stream 源在 v1 不支持，显式抛错而非静默降级。
"""

from __future__ import annotations

import urllib.parse
from pathlib import Path
from typing import Any, Sequence

from ..ids import content_hash, doc_id, stable_id
from ..types import (
    DocumentFormat,
    RawDocument,
    SourceRef,
    SourceType,
    StepError,
)


def _format_from_path(path: str) -> DocumentFormat:
    """从文件扩展名推断格式。未知扩展名给 UNKNOWN，让 Parse 决定降级。"""
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
            f"Web 源 {spec!r} 在 v1 不支持（需联网能力）；"
            f"请先下载为本地文件再用 file:// 投喂",
        )
    # 形如路径且存在 → 当文件；否则当 inline 文本
    p = Path(spec)
    if p.exists() and p.is_file():
        return _read_file(str(p.resolve()))
    return _from_text(spec)


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


# 注意：IngestStep 已迁到 ``smini.ingestors``（支持 agent 用 WorkBuddy 工具
# 产出的 RawDocument 直投）。本模块只保留确定性的「字节搬运」原语，供
# LocalFileIngestor 复用——不再承担编排职责。
