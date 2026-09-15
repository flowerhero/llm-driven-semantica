---
name: semantica-store
description: "把质检通过的图谱持久化到存储后端，产出 StoreReceipt。以 Python 为主（幂等 upsert 是确定性语义，Skill 承担不了）；Skill 承担后端选择规则与回执校验。Use when the user says 落库/存储图谱/save the graph/store the KG, or after qa. ★前置条件：QA passed 必须为 true，否则拒绝落库。"
---

# semantica-store · 存储（Step 7）

> 把 QA 修复后的图谱写进后端。
>
> **这一步几乎纯 Python** —— 幂等 upsert 是确定性语义，必须位运算级别一致。
> Skill 承担两件事：**后端选择规则** 与 **回执校验**。

## ★ 前置闸门

```
QAResult.passed == true   →  允许落库
QAResult.passed == false  →  拒绝落库，报告未裁决冲突
```

> 原版 semantica 的缺陷：**先落库再检测冲突**，落进库的是未消歧的脏数据。
> 这里 QA 在 Store 之前跑完（见 `semantica-qa`），Store 只接受已修复的图谱。

## 承载的 semantica 规则

### 强制 upsert 语义（`types.py::StoreReceipt`）

**原版 Neo4j store 全用 `CREATE`，`MERGE` 零命中** ——
重复构建会**重复插入**，跑两次图谱就翻倍。

本步要求所有 backend 实现 **upsert**，并回传计数让调用方能验证幂等：

| 字段 | 含义 |
|---|---|
| `entities_written` | 新建实体数 |
| `entities_updated` | 更新实体数 |
| `edges_written` | 新建边数 |
| `edges_updated` | 更新边数 |
| `idempotent` | **必须为 `true`** |
| `backend` | 后端名 |
| `elapsed_ms` | 耗时 |

### 后端选择规则（Skill 承担）

| 场景 | backend | 说明 |
|---|---|---|
| 一次性分析、跑完即弃 | `memory` | 内存，最快，进程退出即失 |
| 需要留存 / 跨会话复用 | `json` | 落盘为本地 JSON 文件 |
| 后续要接向量检索 | 预留（`VectorStore` 接口已留） | v1 未实现 |

判断依据：用户说"存下来/留存/下次还要用" → `json`；
否则默认 `memory`。

## Workflow（声明式）

### 1. 检查前置条件
读 `runs/<run_id>/06-qa.json`，确认：
- `metrics.unresolved_count == 0`（即 `passed`）
- 若否 → **停止**，向用户报告未裁决冲突清单，不落库

### 2. 选定 backend
按上面的规则表判断；不确定就问用户。

### 3. 调 Python 落库

```bash
python -m smini.cli graph store \
  --in runs/<run_id>/06-qa.json \
  --backend memory \
  --out runs/<run_id>/07-store.json
```

### 4. ★ 回执校验（Skill 的核心职责）

拿到 `StoreReceipt` 后逐项核对：

- [ ] `idempotent == true` —— **为 false 必须告警**，说明后端没实现 upsert
- [ ] `entities_written + entities_updated` == 图谱实体总数
- [ ] `edges_written + edges_updated` == 图谱边总数
- [ ] **`vectors_written` 为 0**（v1 无向量后端；非 0 说明配置异常）

### 5. ★ 幂等复验（推荐）

对同一份图谱**再跑一次** store：

```
第二次的 entities_written / edges_written 应该都是 0
（全部走 updated 分支）
```

非 0 即说明 upsert 失效，图谱在膨胀 —— 这是原版踩过的坑，必须验。

## 输出契约

`runs/<run_id>/07-store.json` → `StoreReceipt`
字段：`backend` / `entities_written` / `entities_updated` / `edges_written` /
`edges_updated` / `vectors_written` / `elapsed_ms` / `idempotent`

## 降级与异常

| 情况 | 动作 |
|---|---|
| QA 未通过 | **拒绝落库**，报告冲突清单 |
| `idempotent == false` | 告警并登记 `Degradation(component="store.upsert", ...)` |
| 二次跑写入数非 0 | 告警 + 登记 Degradation，说明图谱在膨胀 |
| 后端不可用 | 报错，不静默降级到其他后端 |

## 自检清单

- [ ] 落库前已确认 QA passed
- [ ] 后端选择有依据（不是随手默认）
- [ ] 回执四项计数与图谱规模对得上
- [ ] `idempotent` 为 true
- [ ] 做过二次跑的幂等复验
