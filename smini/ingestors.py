"""smini.ingestors — 摄取能力的可插拔抽象。

设计立场：**摄取本身不需要大模型**，但需要「WorkBuddy 原生」。

  * ``LocalFileIngestor`` —— 确定性兜底：``text://`` / ``file://`` / 本地路径
    / inline 文本，纯 Python 读字节 + 算校验和。
  * 真正「原生」的摄取（网页、云文档、网盘、PDF 抽取、Office 解析）由
    **WorkBuddy 生态工具**承担：``agent-browser``（网页）、``pdf`` /
    ``pdfkit-py``（PDF 抽取）、``tencent-local-office-edit`` / ``tencent-docs``
    （docx/xlsx）、``资料库``（腾讯文档/网盘）。agent 用这些工具把源变成
    ``RawDocument`` 后，通过 ``RunContext.raw_docs`` 直投，Ingest 步**透传**
    跳过本地解析——这就是「摄取原生」的落地方式。

这样硬代码的「联网抓取 753 行 SSRF 守卫 + 各格式解码器」被生态工具取代，
smini 只保留确定性兜底与「字节 + 校验和 + 来源」的契约。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from .protocols import PipelineStep, RunContext
from .steps.ingest import resolve_source
from .types import RawDocument, StepError


class Ingestor(ABC):
    """一个数据源后端。"""

    @abstractmethod
    def supports(self, spec: str) -> bool:
        """该后端能否处理这个 source spec。"""

    @abstractmethod
    def ingest(self, spec: str, ctx: RunContext) -> RawDocument:
        """把 spec 变成一个 RawDocument（仅持字节 + 来源）。"""


class LocalFileIngestor(Ingestor):
    """确定性兜底：本地/内存文本源。"""

    def supports(self, spec: str) -> bool:
        return not spec.startswith(("http://", "https://", "ftp://"))

    def ingest(self, spec: str, ctx: RunContext) -> RawDocument:
        return resolve_source(spec)


def default_ingestors() -> list[Ingestor]:
    return [LocalFileIngestor()]


class IngestStep(PipelineStep[Sequence[str], list[RawDocument]]):
    """把 source spec 列表变成 RawDocument 列表。

    优先级：
      1. ``ctx.raw_docs`` —— agent 用 WorkBuddy 工具已摄入的内容，直接透传
         （「摄取原生」路径，跳过本地解析）。
      2. 构造参数 ``sources`` / ``ctx.config['ingest']['sources']`` —— 交给
         注入的 ``ingestors``（默认 ``LocalFileIngestor``）确定性解析。
    """

    name = "ingest"
    reads: tuple[str, ...] = ()
    writes = ("raw",)

    def __init__(
        self,
        sources: Sequence[str] | None = None,
        *,
        ingestors: Sequence[Ingestor] | None = None,
    ) -> None:
        self._sources = list(sources) if sources else []
        self._ingestors = list(ingestors) if ingestors else default_ingestors()

    def select(self, state: RunContext) -> Sequence[str]:  # type: ignore[override]
        return self._sources

    def transform(self, sources: Sequence[str], ctx: RunContext) -> list[RawDocument]:
        # 路径 1：agent 工具已摄入 → 透传
        if ctx.raw_docs:
            ctx.log("info", f"ingest: 透传 agent 提供的 {len(ctx.raw_docs)} 个 RawDocument")
            return list(ctx.raw_docs)

        if not sources:
            cfg = ctx.config_for("ingest")
            sources = cfg.get("sources", [])  # type: ignore[assignment]
        if not sources:
            raise StepError("ingest", "没有可摄入的数据源（sources 为空且 ctx.raw_docs 未提供）")

        out: list[RawDocument] = []
        for spec in sources:
            ingestor = next((ig for ig in self._ingestors if ig.supports(spec)), None)
            if ingestor is None:
                raise StepError(
                    "ingest",
                    f"无可用摄取器处理 {spec!r}（Web/云源请由 agent 用 WorkBuddy "
                    f"工具摄入后通过 ctx.raw_docs 直投）",
                )
            rd = ingestor.ingest(spec, ctx)
            ctx.log("info", f"ingest ← {rd.source.uri} ({rd.size_bytes}B)")
            out.append(rd)
        return out

    def commit(self, state: object, output: list[RawDocument]) -> None:
        from .protocols import PipelineState
        assert isinstance(state, PipelineState)
        state.raw = output
