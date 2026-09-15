---
name: semantica-qa
description: "对知识图谱做质量保障：检测事实冲突、归并同义谓词、去重合并实体，产出已修复的 QAResult。LLM 承担同义谓词归并（成立于/创立于 → 同一谓词）与语义冲突识别（TYPE/MUTEX/LOGIC/TEMPORAL）；确定性冲突检测与双时态裁决由 Python 执行。Use when the user says 质检/检查冲突/去重/check conflicts/deduplicate, or after build-kg. ★关键契约：QA 必须在 Store 之前跑完。"
---

# semantica-qa · 质检（Step 6）

> 检出冲突、裁决、去重，产出**已修复的图谱**。
>
> **★ 关键契约：QA 必须在 Store 之前跑完。**
> 原版 semantica 先落库再检测冲突 —— 落进库的是**未消歧的脏数据**，
> 事后才发现冲突，但库已经被污染。这里 QA 返回修复后的 graph，
> `passed == false` 时 Store 必须拒绝落库。

## ★ 分工

| 谁 | 做什么 |
|---|---|
| **Skill（LLM）** | ① 同义谓词归并表 ② 语义冲突识别（TYPE / MUTEX / LOGIC / TEMPORAL） |
| **Python** | VALUE 类冲突检测、裁决执行、双时态作废、去重合并、指标统计 |

## 承载的 semantica 规则

### ConflictKind 5 类（`types.py::ConflictKind`）

| kind | 含义 | 谁检 |
|---|---|---|
| `VALUE` | 同主语同谓语，多个不同值 | **Python**（确定性） |
| `TYPE` | 同一实体被赋予互斥类型 | **LLM**（语义） |
| `TEMPORAL` | 时间区间矛盾（任期重叠） | **LLM**（语义） |
| `MUTEX` | 互斥关系（一人不能同时是两家公司 CEO） | **LLM**（语义） |
| `LOGIC` | 逻辑矛盾（出生晚于死亡） | **LLM**（语义） |

> 原版 `conflict_detector.py:60-95` 定义了这 5 类，但**只实现了 VALUE**。
> LLM 路径可以真正覆盖另外 4 类 —— 这是明确的增值点。

### ResolutionStrategy 6 种（`types.py::ResolutionStrategy`）

`most_recent` `highest_confidence` `majority_vote` `source_priority` `keep_all` `manual`

**默认裁决规则**：
```
有时态信息 → MOST_RECENT
否则       → MAJORITY_VOTE
```

### 铁律：落选边作废，绝不删除

```
落选边.temporal.invalidate(now)
```

- 历史版本仍可通过 `all_edges()` 查询
- 但不在 `current_edges()` 当前视图中
- **绝不物理删除** —— 删了就无法审计"为什么当时这么裁决"

### 去重规则（`qa.py`）

- canonical_name / 别名相撞 → 聚成一个 `DuplicateCluster`
- 规范实体选 **`max(len(mention_ids), len(properties), len(aliases))`**
- **★ 被合并方的名字必须进 `aliases`，绝不丢弃**
  > 原版 `entity_merger` 合并时丢别名，导致用旧名字查不到实体 —— 这是个真实 bug。

### Resolution 必须带 rationale

无法解释的自动裁决不可接受。没有理由 → `needs_review = true`，交人工。

## Workflow（声明式）

### 1. 读输入
`runs/<run_id>/05-graph.json`

### 2. ★ 产出同义谓词归并表（LLM）

解决的问题：不归并的话，「X 成立于 2003」与「X 创立于 2005」
因为谓词不同，**不会被判定为冲突**，错误事实就这么溜过去了。

```json
{
  "groups": [
    { "canonical": "成立于", "variants": ["创立于", "创建于", "组建于"] },
    { "canonical": "任职于", "variants": ["供职于", "就职于", "加盟"] }
  ]
}
```

只归**真正同义**的。语义有差异的（如「收购」vs「投资」）不要归。

### 3. ★ 语义冲突识别（LLM）

扫 `entities` 与 `current_edges`，按 TYPE / MUTEX / LOGIC / TEMPORAL 四类找矛盾，
每条给出：`kind` / `subject_id` / `predicate` / `values` / `severity` /
**建议的 resolution（含 rationale）**。

### 4. 落盘建议
写 `runs/<run_id>/06-suggestions.json`，遵循 `contracts/graph.schema.json`
的 `predicate_normalization` 与 `conflicts` 部分。

### 5. 调 Python 执行裁决

```bash
python -m smini.cli graph qa \
  --in runs/<run_id>/05-graph.json \
  --suggestions runs/<run_id>/06-suggestions.json \
  --out runs/<run_id>/06-qa.json
```

Python 做的事：
1. 按归并表归一化谓词
2. 检测 VALUE 类冲突（同 `subject_id` + 同归一化谓词 → 多个不同值）
3. 合并 LLM 报的语义冲突
4. 按默认策略裁决（有时态→most_recent，否则→majority_vote）
5. 落选边 `invalidate(now)`
6. 去重合并实体（被合并方名字进 aliases）
7. 算 `QualityMetrics`

### 6. 判 `passed`

```
unresolved_count == 0  →  passed = true  → 可进 Store
否则                    →  passed = false → 中止于 Store 之前
```

## 输出契约

`runs/<run_id>/06-qa.json` → 遵循 `contracts/graph.schema.json`
字段：`graph`（已修复）/ `conflicts[]` / `duplicate_clusters[]` / `metrics`

`QualityMetrics`：`conflict_count` / `unresolved_count` /
`duplicate_cluster_count` / `entities_merged` / `edges_remapped` /
`mean_confidence` / `qa_ms`

## 降级

LLM 建议缺失 → Python 只做 VALUE 类确定性检测，
登记 `Degradation(component="qa.semantic", kind=unavailable,
fallback="value-only", impact="TYPE/MUTEX/LOGIC/TEMPORAL 四类语义冲突未覆盖")`。

## 自检清单

- [ ] 谓词归并表里每组都是**真同义**（不是近义）
- [ ] 每条冲突都有 `rationale`
- [ ] 语义冲突的四类都扫过（不是只扫了 VALUE）
- [ ] 拿不准的标了 `needs_review` 而不是硬裁决
- [ ] `passed=false` 时已明确阻止进入 Store
- [ ] 去重后被合并方的旧名字进了 `aliases`
