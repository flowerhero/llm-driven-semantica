---
name: semantica-deliver
description: "从图谱检索并组装查询就绪的 ContextPackage。LLM 承担查询理解（从问句抽实体与意图）与在确定性 facts 之上合成中文答案；确定性检索与 Citation 组装由 Python 执行。Use when the user says 查询图谱/从图谱回答/检索上下文/query the graph/answer from the KG, or after store."
---

# semantica-deliver · 交付（Step 8）

> 把用户问句变成「有出处的事实包」。
>
> **★ 基线是确定性 facts，不是 LLM 答案。** `answer` 是**可选**字段，
> 默认 `None`，不影响幂等。LLM 只负责让它更好读，不负责产生事实。

## ★ 分工

| 谁 | 做什么 |
|---|---|
| **Skill（LLM）** | ① 查询理解（抽实体与意图）② 在 facts 之上合成中文答案 |
| **Python** | 确定性检索（双向）、Citation 组装（到字符区间） |

> 为什么检索不能给 LLM：精确召回，**不能靠 LLM 回忆图里有什么** ——
> 那是幻觉的温床。LLM 只拿到检索结果后做表达。

## 承载的 semantica 规则

### Citation 必须到字符区间（`types.py::Citation`）

```python
Citation(
    source_ref=...,          # 来自哪个 SourceRef
    char_span=(start, end),  # 在原文中的字符区间
    quoted_text="...",       # ★ 原文片段，让使用方能立刻校验
    chunk_id=...,
    extractor="llm.v1",
    confidence=1.0,
)
```

> 原版 provenance **只到「来自哪个文档」**，无法定位到具体位置。
> 这里要求到字符区间 + 带原文片段 —— 使用者一眼就能看出引用对不对。

**坐标系**：`char_span` 默认 **orig 坐标系**（原文）。
若手上是 norm 坐标，需经 `map_span_back()`（用 `patches` 反查）换算回 orig。

### 双向检索（`deliver.py`）

主语视角 **和** 宾语视角都要召回。

> 不对称会怎样：查「特斯拉 总部」有结果，查「谁的总部在帕洛阿托」没结果。
> 用户会觉得图谱时灵时不灵。所以两个方向都得建索引。

### `answer` 是可选字段

`ContextPackage.answer` 默认 `None`。
**确定性 facts 才是基线** —— 没有 LLM 也能交付，有了只是更好读。

## Workflow（声明式）

### 1. 查询理解（LLM）

从用户问句里抽出：
- **目标实体**：问的是谁/什么（如「特斯拉」「马斯克」）
- **意图谓词**：想知道什么关系（如「总部」「任职」「成立于」）
- **约束**：时间、金额等限定

把抽出的实体/谓词交给 Python 做检索，不要让 LLM 自己回忆图谱内容。

### 2. 调 Python 做确定性检索

```bash
python -m smini.cli deliver \
  --query "特斯拉 总部" \
  --in runs/<run_id>/06-qa.json \
  --out runs/<run_id>/08-package.json
```

Python 做的事：
1. 双向检索（主语视角 + 宾语视角）
2. 召回 top 实体与相关边
3. 组装 `facts: Triplet[]`
4. 组装 `citations: Citation[]`（到字符区间 + quoted_text）
5. 算 `generated_at`

### 3. 合成中文答案（LLM，可选）

在 Python 给的 `facts` 之上写一段自然语言回答。
**约束**：
- 只用 facts 里有的信息，**不得补充图谱外的内容**
- 每条事实尽量带上出处提示
- facts 为空 → 直接说"图谱中没有相关信息"，**不要编**
- 合成失败 → `answer = None`（确定性 facts 仍完整交付）

写回 `08-package.json` 的 `answer` 字段。

### 4. 落盘
`runs/<run_id>/08-package.json` → `ContextPackage`

## 输出契约

`ContextPackage` 字段：
`query` / `facts: Triplet[]` / `entities: Entity[]` / `citations: Citation[]` /
`generated_at` / `retrieval` / `degradation_notes[]` / `answer: str | None`

## 降级

| 情况 | 动作 | Degradation |
|---|---|---|
| 检索无结果 | 返回空 facts，`answer` 写"图谱中没有相关信息" | 无（正常情况） |
| 答案合成失败 | `answer = None`，facts 照常交付 | `component="deliver.answer", kind=unavailable` |
| span 无法映射回 orig | `char_span = null`，保留 `quoted_text` | `component="deliver.span", kind=parse_failed` |

## 自检清单

- [ ] 检索走的是 Python，不是 LLM 回忆
- [ ] 双向都召回了（换问法也能查到）
- [ ] 每条 Citation 都有 `quoted_text`
- [ ] `char_span` 是 orig 坐标系（norm 的已换算）
- [ ] `answer` 里的每个事实都能在 `facts` 里找到
- [ ] facts 为空时没有编造答案
