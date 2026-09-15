"""smini.pipeline_factory — 组装默认 8 步流水线。

采用 ``sequential-pipeline`` 模式：8 步严格有序、硬依赖、早失败即停。
不使用 sub-agent（避免 context 隔离开销），不使用 MCP（smini 是本地
Python，违反技能自身「不该为 CLI 能做的事加 MCP」的反模式）。

v1 默认跑全套确定性实现；LLMProvider / Embedder / VectorStore 都是
**可选注入**，缺省即走确定性路径（不降级、不报错）。
"""

from __future__ import annotations

from typing import Sequence

from .protocols import Embedder, GraphStore, LLMProvider, Pipeline, RunContext, VectorStore
from .steps import (
    BuildKGStep,
    DeliverStep,
    ExtractStep,
    IngestStep,
    NormalizeStep,
    ParseStep,
    QAStep,
    StoreStep,
)
from .stores import MemoryGraphStore


def build_pipeline(
    sources: Sequence[str] | None = None,
    *,
    store: GraphStore | None = None,
    embedder: Embedder | None = None,
    vector_store: VectorStore | None = None,
    query: str | None = None,
    llm: LLMProvider | None = None,
) -> Pipeline:
    """组装标准 8 步流水线。

    Args:
        sources: 数据源 spec 列表（text:// / file:// / 路径 / inline）。
        store: 图存储后端，默认进程内内存。
        embedder / vector_store: 可选，注入后 Store 步同时写向量。
        query: 交付查询；不传则 Deliver 步在运行时从 ctx 取。
        llm: 可选大模型提供方；注入后 Parse/Extract/QA/Deliver 走 LLM 路径，
            不可用则确定性兜底并登记 Degradation（不静默）。
    """
    return Pipeline(
        steps=[
            IngestStep(sources),
            ParseStep(),
            NormalizeStep(),
            ExtractStep(),
            BuildKGStep(),
            QAStep(),
            StoreStep(store or MemoryGraphStore(), embedder, vector_store),
            DeliverStep(query),
        ],
        name="semantica-smini",
        llm=llm,
    )


def make_context(
    sources: Sequence[str] | None = None,
    query: str | None = None,
    *,
    seed: int = 0,
    run_id: str = "",
    config: dict | None = None,
    llm: LLMProvider | None = None,
    raw_docs: "list | None" = None,
) -> RunContext:
    """构造运行上下文。时间由 ctx.now() 注入，保证可复现。

    Args:
        llm: 大模型提供方（可选）。
        raw_docs: agent 用 WorkBuddy 工具已摄入的 RawDocument 列表；非空时
            Ingest 步直接透传，跳过本地解析（实现「摄取原生」）。
    """
    cfg: dict = dict(config or {})
    if sources is not None:
        cfg.setdefault("ingest", {})["sources"] = list(sources)
    if query is not None:
        cfg.setdefault("deliver", {})["query"] = query
    return RunContext(run_id=run_id, seed=seed, config=cfg, llm=llm, raw_docs=raw_docs)
