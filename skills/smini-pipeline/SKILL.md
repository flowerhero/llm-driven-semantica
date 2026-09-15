---
name: semantica-build
description: "semantica 简易版的主编排：按 ingest → parse → normalize → extract → build-kg → qa → store → deliver 顺序调度 8 个 Step Skill，把文档变成可查询的知识图谱。Use PROACTIVELY when the user says 建知识图谱/跑 semantica 流水线/把这份文档结构化/从 X 建图/build a knowledge graph/run the semantica pipeline。★注意：这是主编排，不是 Step 5 的 semantica-build-kg。"
---

# semantica-build · 主编排

> **这是整条流水线的入口，不是 Step 5。**
> Step 5 建图是 `semantica-build-kg`；本 Skill 负责**调度全部 8 步**。

## 定位

本 Skill **自己不含任何处理逻辑**，只做编排：

```
选步骤 → 传文件 → 调 Skill → 调 Python 校验 → 记 Degradation → 下一步
```

所有"怎么解析版式""怎么抽实体""怎么判冲突"的规则都在各 Step Skill 的
`SKILL.md` 与 `references/` 里，本文件不重复。

## Workflow（声明式）

### 1. 初始化运行目录

```bash
RUN_ID=$(date +%Y%m%d-%H%M%S)
mkdir -p runs/$RUN_ID
```

每步的产物都落在 `runs/$RUN_ID/` 下，**Skill 之间靠 JSON 文件通信**
（Skill 在 agent 循环里跑，跨 Skill 无法共享内存）。

| 文件 | 产出方 |
|---|---|
| `01-raw.json` | `semantica-ingest` |
| `02-parsed.json` | `semantica-parse` |
| `03-normalized.json` | `semantica-normalize` |
| `04-extraction.json` | `semantica-extract` |
| `05-disambiguation.json` | `semantica-build-kg`（LLM 部分） |
| `05-graph.json` | `semantica-build-kg`（Python 部分） |
| `06-suggestions.json` | `semantica-qa`（LLM 部分） |
| `06-qa.json` | `semantica-qa`（Python 部分） |
| `07-store.json` | `semantica-store` |
| `08-package.json` | `semantica-deliver` |
| `manifest.json` | 本 Skill |

### 2. 按序调度 8 步

严格按序，**不接受跳步**：

```
semantica-ingest
  → semantica-parse
  → semantica-normalize
  → semantica-extract
  → semantica-build-kg
  → semantica-qa
  → semantica-store
  → semantica-deliver
```

每一步：
1. **调用对应 Step Skill**（让它完成 LLM 部分 + 调 Python 完成确定性部分）
2. **确认产物文件已写出**
3. **调 `smini validate` 校验线格式**（Schema + 领域约束）
4. 失败 → 按该 Step 的降级路径处理，并**把 Degradation 记进 manifest**

### 3. ★ QA 闸门

`semantica-qa` 结束后必须检查：

```
metrics.unresolved_count == 0  →  继续进 Store
metrics.unresolved_count  > 0  →  ★ 中止于 Store 之前
```

> 原版先落库再检测冲突，落进库的是未消歧的脏数据。
> 这里 QA 在 Store 之前跑完，未裁决的冲突**不许入库**。

中止时向用户报告：未裁决冲突清单（subject / predicate / 各候选值 /
为什么没能自动裁决），让用户决定。

### 4. 写 `manifest.json`

```json
{
  "run_id": "20260903-193000",
  "sources": ["file:///path/to/doc.md"],
  "steps": [
    { "name": "ingest",   "mode": "skill", "ok": true, "elapsed_ms": 120 },
    { "name": "parse",    "mode": "llm",   "ok": true, "elapsed_ms": 3400 },
    { "name": "extract",  "mode": "llm",   "ok": true, "elapsed_ms": 8900,
      "degradations": [] }
  ],
  "degradations": [],
  "stats": { "entity_count": 42, "edge_count": 57, "conflict_count": 0 }
}
```

`mode` 取值：`skill`（工具路由为主）/ `llm`（模型产出为主）/ `python`（确定性为主）/
`fallback`（走了确定性兜底）。

### 5. 交付
把 `08-package.json` 的 `facts` + `citations` + `answer` 呈现给用户，
并附上 manifest 摘要（实体数/边数/冲突数/降级清单）。

## 单步调试

任何一步都能独立跑（不依赖上游的内存状态，只读 JSON 文件）：

```
只跑抽取看看 → 触发 semantica-extract，指定 runs/<id>/03-normalized.json
重跑建图     → 触发 semantica-build-kg，指定 runs/<id>/04-extraction.json
```

## 硬规则

1. **降级必须出声** —— 任何 fallback 都要进 `manifest.degradations`，
   含 component / kind / reason / fallback / impact。绝不静默。
2. **同一输入跑两次必须幂等** —— `entity_id` / `edge_id` 集合逐字节一致
   （由 sha256 内容寻址 + `FIXED_TX` 保证）。这是验收标准，跑完要验。
3. **不写源目录以外的东西** —— 全部产物在 `runs/<id>/` 下。
4. **不谎报进度** —— 某步失败就原样报该步的错误，不要编造已完成的步骤。

## 验收清单

- [ ] 8 步产物齐全，`manifest.json` 已写
- [ ] 连跑两次，`entity_id` 与 `edge_id` 集合**逐字节一致**
- [ ] QA 未通过时确实中止在了 Store 之前（没往里写）
- [ ] 降级清单如实记录了所有 fallback
- [ ] Citations 的 `quoted_text` 能在原文里找到
