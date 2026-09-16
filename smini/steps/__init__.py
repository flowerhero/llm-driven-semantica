"""smini.steps — extract-only 流水线的具体实现。

每个模块导出一个 ``PipelineStep`` 子类，对齐 ``smini.protocols`` 的
``select`` / ``transform`` / ``commit`` 三段式契约。

  * ``ExtractStep``   —— 宿主 LLM 契约抽取（★核心，零规则薄壳）
  * ``BuildKGStep``   —— 惰性锚点消费层（object_literal 派生 / 属性双落位 /
    六件套 count 登记 / 实体-边图构建）

解析、归一化、质检、存储、交付步骤已随冗余清理删除（能力外包宿主或
不在抽取主链路）。
"""

from .build_kg import BuildKGStep
from .extract import ExtractStep

__all__ = [
    "ExtractStep",
    "BuildKGStep",
]
