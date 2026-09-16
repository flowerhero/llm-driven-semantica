# smini — 本体抽取（extract-only · 契约即规则）

在宿主 agent（豆包）中可运行的 **本体抽取** 精简实现：把文档/文本按约定 JSON
契约抽取为知识图谱（实体-关系三元组、属性、业务规则、业务流程、预测决策
六件套、时态/动作/约束/授权），并渲染自包含 HTML 查看器。

**特性**：确定性 · 零外部依赖（仅 Python 标准库）· 内容寻址 ID · 幂等 · 宿主即 LLM（零凭证）

```
ingest（读源原语）→ extract（宿主 LLM 契约抽取 ★）→ build_kg（惰性锚点消费层）
```

---

## 快速开始

```bash
# 内置样例（宿主未交卷 → 抽取为空但管线跑通，明确提示）
python -m smini.cli build --sample

# 宿主 agent 充当 LLM：按契约交卷后抽取
python -m smini.cli build --sample --host-contract contract.json

# 自定义文本 / 文件
python -m smini.cli build "text://特斯拉公司成立于2003年，总部位于美国加州。"
python -m smini.cli build "file:///path/to/doc.txt"

# 导出整条 state
python -m smini.cli build --sample --json state.json
```

Python API：

```python
from smini import build_pipeline, make_context, HostAgentLLMProvider

sources = ["特斯拉公司成立于2003年，总部位于美国加利福尼亚州。"]
llm = HostAgentLLMProvider().inject({...})   # 宿主按契约交卷
ctx = make_context(sources, llm=llm)
state, _ = build_pipeline(sources, llm=llm).run(ctx=ctx)
print(state.graph.stats)   # 实体/边统计
```

---

## 文档

| 文件 | 内容 |
|------|------|
| [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | 架构、薄壳职责、惰性锚点、运行与测试 |
| [`CONTRACTS.md`](CONTRACTS.md) | 十一件套契约与数据结构（含契约测试说明） |
| [`SKILL-ARCHITECTURE.md`](SKILL-ARCHITECTURE.md) | Skill 架构（extract-only）+ 原 9-skill 规划归档 |
| [`semantica-8步流水线源码解析.md`](semantica-8步流水线源码解析.md) | 原版 semantica 源码解析（实现依据） |
| 属性 / 业务规则 / 业务流程 / 预测决策本体 / 七模型借鉴增量设计 | 五份增量设计文档 |

---

## 测试

```bash
python -m unittest discover -s tests   # 126 项全绿
```

---

## 设计要点

- **契约即规则**：抽取规则全部声明式（大模型理解），Python 只做确定性映射与校验。
- **内容寻址 ID**：同输入逐字节同构，重跑天然幂等。
- **惰性锚点**：`object_literal` 由 `object_type` 派生、属性双落位（`entity.properties` +
  字面量边）、六件套 count 登记 —— 宿主交卷可省略的字段由消费层兜底，不造假。
- **宿主即 LLM（方式 3）**：`HostAgentLLMProvider` 未注入时不可用 → 空结果 +
  `Degradation(no_credential)`，不静默、不伪造。
