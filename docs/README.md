# smini — semantica 精简知识图谱流水线

在 WorkBuddy 中可运行的 **semantica 8 步知识图谱流水线**精简实现。
把文档/RAG 语料构建成可查询的知识图谱，并按查询组装成带出处的 `ContextPackage`。

**特性**：确定性 · 零外部依赖（仅 Python 标准库）· 双时态 · 内容寻址 ID · 降级显式化

```
ingest → parse → normalize → extract → build_kg → qa → store → deliver
```

---

## 快速开始

```bash
# 内置样例，查询「特斯拉 总部」
python -m smini.cli build --sample -q "特斯拉 总部"

# 自定义文本 / 文件
python -m smini.cli build "text://特斯拉公司成立于2003年，总部位于美国加州。"
python -m smini.cli build "file:///path/to/doc.txt" -q "关键词"

# 导出整条 state
python -m smini.cli build --sample -q "特斯拉" --json state.json
```

Python API：

```python
from smini import build_pipeline, make_context

src = ["特斯拉公司成立于2003年，总部位于美国加利福尼亚州。"]
ctx = make_context(src, query="特斯拉 总部")
state, _ = build_pipeline(src, query="特斯拉 总部").run(ctx=ctx)
print(state.delivered.render())   # 可直接塞进 prompt 的上下文
```

---

## 文档

| 文件 | 内容 |
|------|------|
| [`IMPLEMENTATION.md`](IMPLEMENTATION.md) | 架构、各步职责、关键设计决策、运行与测试 |
| [`CONTRACTS.md`](CONTRACTS.md) | 每一步的接口契约与数据结构（含 60 项契约测试说明） |
| [`semantica-8步流水线源码解析.md`](semantica-8步流水线源码解析.md) | 原版 semantica 源码解析（实现依据） |

---

## 测试

```bash
python -m unittest discover -s tests -v   # 契约 60 + 端到端 10，全绿
```

---

## 设计要点

- **内容寻址 ID**：同输入逐字节同构，重跑天然幂等。
- **双时态**：每条事实带 `valid_time` + `transaction_time`，冲突裁决**废版本不删边**。
- **Deliver 双向检索**：查询实体作主语/宾语都召回，边带 `Citation` 出处。
- **降级显式**：v1 纯规则抽取，能力缺失必须登记 `Degradation`，绝不伪造向量。
