---
name: semantica-build-kg
description: "把抽取结果汇聚成 KnowledgeGraph。LLM 只承担实体消歧建议（判定哪些 mention 该归一到同一 canonical）；ID 计算、幂等写入、双时态标记、统计重算全部由 Python 确定性执行。Use when the user says 建图/构建知识图谱/build the graph/construct KG, or after extract."
---

# semantica-build-kg · 建图（Step 5）

> 把 mention 收敛成实体、把关系组装成双时态边，产出可存储的图谱。
>
> **这一步以 Python 为主** —— 幂等语义要求"确定性位运算级别的一致"，
> Skill 承担不了。Skill 只提供**实体消歧建议**这一份"智能输入"。

## ★ 分工

| 谁 | 做什么 |
|---|---|
| **Skill（LLM）** | 实体消歧建议：哪些 surface 该归一到同一 canonical |
| **Python** | ID 计算、幂等写入、双时态标记、自环防卫、统计重算 |

## 承载的 semantica 规则

### ID 规范（`smini/ids.py`）—— Python 算，Skill 不碰

```
entity_id = sha256(type, canonical_name.strip().casefold())
edge_id   = sha256(subject_id, predicate, object_id | object_literal)
```

- 同批数据重跑 → ID 不变 → **严格幂等**
- 不同文档抽到同一实体 → 自动收敛为同一节点
- 实体消歧/合并的本质 = **让若干实体共享 canonical_name**，再按 ID 自然归并

> 原版用 Python 内置 `hash()`（带随机盐），跨进程 ID 漂移、图谱无限膨胀。
> 改成 sha256 内容寻址是本项目最关键的修正之一。

### 固定事务时间 `FIXED_TX`（`build_kg.py:34`）

```python
FIXED_TX = datetime(2000, 1, 1, tzinfo=timezone.utc)
```

**不用 `ctx.now()`**。理由：每次 run 的 `recorded_at` 若不同，
`add_edge` 会误判为新版本而**无限追加**，幂等直接崩塌。
`valid_time` 设为开区间（恒真）。

### `add_edge` 幂等规则（`types.py::KnowledgeGraph.add_edge`）

- `temporal` 与某已有版本**相同** → 视为同一事实的重复印证，
  **合并**（置信度取最大、来源取并集），**不产生新版本**
- `temporal` **不同** → **追加为新版本**（保留历史）

### 自环防卫

`subject_id == object_id` 的边直接丢弃。

### 字面量边 vs 实体边 —— 互斥填充

```
object_literal=true  →  KGEdge.object_literal = 值, object_id = null
object_literal=false →  KGEdge.object_id      = 实体ID, object_literal = null
```

这个分支由抽取阶段的 `object_literal` 判定决定（见 `semantica-extract`）。

## Workflow（声明式）

### 1. 读输入
- `runs/<run_id>/04-extraction.json`（已由 Python 补齐 ID）

### 2. ★ 产出实体消歧建议（LLM 核心贡献）

判断哪些 surface 指向同一真实实体。写进 `disambiguation.groups[]`：

```json
{
  "entity_type": "ORGANIZATION",
  "canonical": "特斯拉公司",
  "surfaces": ["特斯拉", "Tesla", "Tesla Inc.", "特斯拉公司"],
  "rationale": "同一家电动汽车制造商；'特斯拉'为中文简称，'Tesla Inc.'为英文全称"
}
```

**判定要点**：
- 同一 `entity_type` 内才归并（跨类型不并）
- `canonical` 取**最完整、最正式**的那个 surface
- **必须给 `rationale`** —— 无法解释的归并不可接受，会被拒绝
- 拿不准就**不归并**（漏并可由 QA 的去重补，错并则事实污染）

### 3. 调 Python 建图

```bash
python -m smini.cli graph build \
  --in runs/<run_id>/04-extraction.json \
  --disambiguation runs/<run_id>/05-disambiguation.json \
  --out runs/<run_id>/05-graph.json
```

Python 做的事：
1. 按消歧建议把 mention 归到 canonical
2. 算 `entity_id` / `edge_id`（sha256）
3. 幂等 `add_entity` / `add_edge`
4. 挂双时态（`FIXED_TX`）
5. 自环防卫、字面量/实体边分流
6. 重算 `GraphStats`

### 4. 校验
```bash
python -m smini.cli validate graph --in runs/<run_id>/05-graph.json
```

## 输出契约

`runs/<run_id>/05-graph.json` → `KnowledgeGraph`
字段：`entities{id → Entity}` / `edges{id → KGEdge[]}` / `stats` / `metadata`

`Entity` 含 `entity_id` / `canonical_name` / `entity_type` / `aliases[]` /
`properties` / `mention_ids[]` / `confidence` / `provenance` / `temporal`

## 降级

消歧建议缺失或不合法 → **不是错误**，Python 按原始 surface 各自建实体，
登记 `Degradation(component="build_kg.disambiguation", kind=unavailable,
fallback="per-surface entities", impact="同一实体的不同写法可能未归并")`。

## 自检清单

- [ ] 消歧建议的每条都有非空 `rationale`
- [ ] 没有跨 `entity_type` 归并
- [ ] `canonical` 是组内最完整的形式
- [ ] 拿不准的没有强行归并
- [ ] 建图后 `stats.entity_count` 与预期量级相符（异常膨胀说明消歧失效）
