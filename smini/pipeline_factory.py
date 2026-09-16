"""smini.pipeline_factory — 组装 extract-only 流水线。

契约即规则（Rule-by-Contract）架构下，完整通路被极大简化：

    ingest（读源原语）→ extract（宿主 LLM 契约抽取，★核心）→ build_kg（惰性锚点消费层）

- **ingest**：``_SourceStep`` 只做字节搬运（``text://`` / ``file://`` / inline），
  或透传宿主 agent 已用自身工具摄入的 ``ctx.raw_docs`` —— 摄入/归一化外包宿主。
- **extract**：宿主按约定 JSON 契约交卷（十一件套），Python 薄壳映射 + 惰性锚点。
- **build_kg**：实体-边图构建 + 属性双落位 + 六件套 count 登记（消费层兜底）。

解析（parse）/ 归一化（normalize）/ 质检（qa）/ 存储（store）/ 交付（deliver）
步骤与对应 Skill 已随冗余清理删除：不再承担摄入解读、版式解析、清洗、冲突
检测、落库与检索交付 —— 这些能力要么外包宿主，要么不属于抽取主链路。
"""

from __future__ import annotations

from typing import Sequence

from .protocols import LLMProvider, Pipeline, PipelineStep, RunContext
from .sources import raw_from_sources
from .steps import BuildKGStep, ExtractStep
from .types import RawDocument


class _SourceStep(PipelineStep[Sequence[str], list[RawDocument]]):
    """读源原语：把 source spec 列表变成 RawDocument 列表。

    优先级：
      1. ``ctx.raw_docs`` —— 宿主 agent 已用自身工具摄入的内容，直接透传。
      2. 构造参数 ``sources`` —— 确定性解析（``text://`` / ``file://`` / inline）。
    """

    name = "ingest"
    reads: tuple[str, ...] = ()
    writes = ("raw",)

    def __init__(self, sources: Sequence[str] | None = None) -> None:
        self._sources = list(sources) if sources else []

    def select(self, state: object) -> Sequence[str]:  # type: ignore[override]
        return self._sources

    def transform(self, sources: Sequence[str], ctx: RunContext) -> list[RawDocument]:
        if ctx.raw_docs:
            ctx.log("info", f"ingest: 透传宿主提供的 {len(ctx.raw_docs)} 个 RawDocument")
            return list(ctx.raw_docs)
        if not sources:
            cfg = ctx.config_for("ingest")
            sources = cfg.get("sources", [])  # type: ignore[assignment]
        if not sources:
            from .types import StepError
            raise StepError("ingest", "没有可摄入的数据源（sources 为空且 ctx.raw_docs 未提供）")
        docs = raw_from_sources(sources)
        for rd in docs:
            ctx.log("info", f"ingest ← {rd.source.uri} ({rd.size_bytes}B)")
        return docs

    def commit(self, state: object, output: list[RawDocument]) -> None:
        from .protocols import PipelineState
        assert isinstance(state, PipelineState)
        state.raw = output


def build_pipeline(
    sources: Sequence[str] | None = None,
    *,
    llm: LLMProvider | None = None,
) -> Pipeline:
    """组装 extract-only 流水线（ingest → extract → build_kg）。

    Args:
        sources: 数据源 spec 列表（text:// / file:// / 路径 / inline）。
        llm: 可选大模型提供方（宿主 agent 充当 LLM）；不传则抽取为空并登记
            ``Degradation(no_credential)`` —— 零规则架构无确定性兜底，不造假。
    """
    return Pipeline(
        steps=[
            _SourceStep(sources),
            ExtractStep(),
            BuildKGStep(),
        ],
        name="semantica-smini-extract",
        llm=llm,
    )


def make_context(
    sources: Sequence[str] | None = None,
    *,
    seed: int = 0,
    run_id: str = "",
    config: dict | None = None,
    llm: LLMProvider | None = None,
    raw_docs: "list | None" = None,
) -> RunContext:
    """构造运行上下文。时间由 ctx.now() 注入，保证可复现。

    Args:
        llm: 大模型提供方（宿主 agent 充当 LLM，可选）。
        raw_docs: 宿主 agent 已用自身工具摄入的 RawDocument 列表；非空时
            ``_SourceStep`` 直接透传，跳过本地解析（实现「摄入原生」）。
    """
    cfg: dict = dict(config or {})
    if sources is not None:
        cfg.setdefault("ingest", {})["sources"] = list(sources)
    return RunContext(run_id=run_id, seed=seed, config=cfg, llm=llm, raw_docs=raw_docs)
