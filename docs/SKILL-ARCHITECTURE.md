# semantica 简易版 · 技能化架构规划（Skill-First）

> 目标：**应用主体 = md 格式的 Skill**。每个 Skill 承载从 semantica 源码/注释中
> 提炼出的处理逻辑（声明式 workflow + LLM 指令）。只有当 Skill 确实做不到时，
> 才调用 Python 执行确定性计算。
>
> 状态：**已确认 · 实施中**（用户 2026-09-03 认可架构，2026-09-04 起逐步实施）

### 实施进度

| 步 | Skill | 状态 | 验证 |
|---|---|---|---|
| 1 | `smini-ingest` | ✅ 已完成 | `verify.sh` 17/17 通过 |
| 2 | `smini-parse` | ⏳ 待实施 | — |
| 3 | `smini-normalize` | ⏳ 待实施 | — |
| 4 | `smini-extract` | ⏳ 待实施 | — |
| 5 | `smini-build-kg` | ⏳ 待实施 | — |
| 6 | `smini-qa` | ⏳ 待实施 | — |
| 7 | `smini-store` | ⏳ 待实施 | — |
| 8 | `smini-deliver` | ⏳ 待实施 | — |
| 9 | `smini-pipeline` | ⏳ 待实施 | — |

> 每个 Skill 自带 `verify.sh`，正向（功能/幂等）+ 反向（契约违规必须被拦）双向验证。
> 每步完成后交用户检查，确认后再进入下一步。

---

## 0. 与上一版（Python-First）的根本差异

| 维度 | 上一版（已实现） | 本规划（Skill-First） |
|---|---|---|
| 主体 | `smini/steps/*.py` 8 个 Python 类 | `skills/smini-*/SKILL.md` 9 个 Skill |
| 智能位置 | Python 正则/启发式 + LLM 可选旁路 | LLM（由 Skill 的声明式 workflow 驱动） |
| Python 角色 | 实现全部 8 步 | **只做 5 类确定性原语**，被 Skill 调用 |
| 步骤编排 | `Pipeline.run()` 内存对象流 | 主编排 Skill 按序 invoke 子 Skill |
| 步骤间通信 | Python 对象引用 | **JSON 文件落盘**（`runs/<id>/NN-*.json`） |
| 已有 70+ 测试 | 主验证手段 | 降级为 **fallback 路径**的回归基线 |

---

## 1. 交付物结构

```
semantica简易版/
├── skills/                          # ★ 应用主体：9 个 Skill
│   ├── smini-pipeline/SKILL.md         # 主编排（入口，串联 8 步）
│   ├── smini-ingest/SKILL.md           # Step 1 · 摄取
│   ├── smini-parse/SKILL.md            # Step 2 · 解析
│   ├── smini-normalize/SKILL.md        # Step 3 · 归一化
│   ├── smini-extract/SKILL.md          # Step 4 · 抽取（核心）
│   ├── smini-build-kg/SKILL.md         # Step 5 · 建图
│   ├── smini-qa/SKILL.md               # Step 6 · 质检
│   ├── smini-store/SKILL.md            # Step 7 · 存储
│   └── smini-deliver/SKILL.md          # Step 8 · 交付
│   └── （各 Skill 下 references/*.md = 从 semantica 源码提炼的规则规范）
│
├── smini/                              # ★ 降级为 Skill 调用的确定性运行时
│   ├── types.py        保留 · 数据契约（dataclass 层，跨步 JSON 的字段定义源）
│   ├── ids.py          保留 · sha256 内容寻址 ID
│   ├── spans.py        新增 · SpanPatch 坐标映射 + 几何校验
│   ├── graph.py        新增 · 建图 / 双时态 / QA 裁决 / 去重（幂等）
│   ├── validate.py     新增 · JSON Schema 校验 + Parse 保真比对
│   ├── retrieve.py     新增 · 确定性检索 + Citation 组装
│   ├── steps/*.py      降级 · 8 步确定性实现 → **fallback 兜底**（回归基线）
│   └── cli.py          改造 · 从「跑整条流水线」改为「暴露 5 类原子命令」
│
├── contracts/                          # ★ 每步 JSON 线格式（Skill 与 Python 共用的唯一契约）
│   ├── raw.schema.json
│   ├── parsed.schema.json
│   ├── normalized.schema.json
│   ├── extraction.schema.json
│   └── graph.schema.json
│
└── runs/<run_id>/                      # 每次运行的中间产物（Skill 间靠文件传递）
    ├── 01-raw.json  →  02-parsed.json  →  03-normalized.json
    ├── 04-extraction.json  →  05-graph.json  →  06-qa.json
    └── 07-package.json  +  manifest.json（含 Degradation 清单）
```

---

## 2. 五条架构铁律

1. **Skill 决定「做什么、按什么规则做」** —— 规则以 md 写进 SKILL.md 与 `references/`，
   来源必须是 semantica 源码/注释的提炼，可追溯到具体行。
2. **Python 只做 Skill 算不准或做不到的事**，仅限 5 类（见 §4）。
3. **LLM 只「提议」事实，绝不出 ID** —— 所有 `entity_id` / `edge_id` / `mention_id`
   由 Python `smini ids` 用 sha256 计算。理由见 `smini/ids.py` 头部注释：
   原版用 `hash()` 带随机盐，导致跨进程 ID 漂移、图谱无限膨胀。
4. **Skill 之间只用 JSON 文件通信** —— Skill 在 agent 循环里运行，跨 Skill 无法共享内存，
   必须落盘。每步输出先写 `runs/<id>/NN-*.json`，再交给下一步。
5. **降级必须出声** —— Skill/LLM 做不到 → 调 Python 确定性路径 → 写 `Degradation`
   （component / kind / reason / fallback / impact），绝不静默。

---

## 3. 八个 Step Skill 的设计

### Step 1 · `smini-ingest` —— 摄取

**触发**：摄取文档 / 从 X 建图 / 导入 PDF / 抓取网页

**声明式 workflow**（按 source 形态路由到宿主工具）：

| source 形态 | 调用的工具 / Skill | 产出 |
|---|---|---|
| `https://` 网页 | `agent-browser` Skill 或 `WebFetch` | 页面正文 → RawDocument |
| `.pdf` | `pdf` Skill / `pdfkit-py` | 提取文本 → RawDocument |
| `.docx/.xlsx/.pptx` | Office 相关 Skill | 文本 → RawDocument |
| 扫描版 PDF / 图片 | OCR 工具 | 文本 → RawDocument |
| 在线文档 / 资料库 | 资料库 Skill | 文本 → RawDocument |
| 本地文件 / `text://` | **Python 兜底**：`smini ingest --source <spec>` | RawDocument |

**承载的 semantica 规则**（源自 `smini/steps/ingest.py`）：
- `DocumentFormat` 15 类后缀→格式推断表（`_format_from_path`）
- `SourceRef` URI 约定：`file://` / `https://` / `memory://inline/<sha256>`
- 铁律：**Ingest 不做任何解读**，只做三件事——拿字节、算 checksum、记出处
  （原版在此阶段就解码 str，猜错编码则全链路失真且无从恢复）

**输出**：`01-raw.json` — `RawDocument[]`（content 用 base64，checksum = sha256）

---

### Step 2 · `smini-parse` —— 解析

**触发**：解析文档 / 提取版式 / 分块

**LLM 承担**：从原文提取**结构化文本 + 版式区块**，产出 `text` + `blocks[]`。

**承载的 semantica 规则**（源自 `types.py::BlockKind` + `steps/parse.py::_parse_blocks`）：
- **BlockKind 10 类封闭枚举**：`paragraph / heading / table / code / list_item /
  quote / caption / image_ref / footnote / metadata`
- 版式判定细则（从行式解析逻辑提炼为 LLM 指令）：
  - `heading`：`^(#{1,6})\s+`，level = `#` 个数（上限 6）
  - `list_item`：`^([-*+]|\d+[.)])\s+`
  - `quote`：`^>\s?`
  - `table`：markdown 管道表，必须解析出 `rows: string[][]`
  - `code`：``` 围栏，需提取 `language`
  - `paragraph`：空行之间的连续行累加
- `order` 跨 kind 全局单调递增；`block.text` 必须严格等于 `text[char_start:char_end]`

**保真铁律**（LLM 最易违背，必须由 Python 校验）：

```
abs(len(text) - len(原文)) > max(8, 0.05 * len(原文))  →  判定失败，回退确定性解析
```

**Python 调用**：
- `smini validate parse --out 02-parsed.json --orig 01-raw.json` — 保真 + Schema 双校验
- 二进制格式（PDF/DOCX 等）若工具链转不出文本 → 显式降级
  （`parser="unsupported"` + warning，**绝不静默造假内容**）

---

### Step 3 · `smini-normalize` —— 归一化

**LLM 承担**：产出**归一化操作清单**（patch 提议），而非直接给结果文本。

**承载的 semantica 规则**（源自 `steps/normalize.py` + `types.py::SpanPatch`）：
- 清洗项：Unicode NFC 组合、控制字符剥离（保留 `\n` `\t`）、连续空格折叠
- 规则实体识别（原为 4 条正则，改为 LLM 指令）：
  `DATE / MONEY / PERCENT / QUANTITY`，且 DATE 需归一为 `YYYY-MM-DD` 形式
- 冲突消解：多个规则命中同一区间时，按 `(start, -length)` 排序、长者优先、跳过被覆盖者

**为什么坐标必须 Python 算**：SpanPatch 需满足三条几何约束（`validate_patches`）：
1. 在 new 坐标系下按 `new_start` 升序
2. 区间不重叠
3. `new_start == orig_start + 前面所有 patch 的 delta 之和`

LLM 逐字符算漂移几乎必错，且错了会**静默偏移几个字符**（极难排查的溯源断裂）。

**Python 调用**：`smini normalize --apply --in 03-normalized.json --out 03-normalized.json`
（执行 LLM 提议的 patch + 校验几何约束 + 重映射 block span；失败则回退确定性清洗）

---

### Step 4 · `smini-extract` —— 抽取 ★核心

**触发**：抽取实体 / 抽取关系 / 识别命名实体 / 跑 S4

**LLM 承担**：命名实体识别 + 关系抽取（这是最能体现 Skill 价值的一步）。
本步已从「规则驱动」升级为「**契约即规则**」：确定性抽取规则（正则 NER、
8 条关系模式、伪实体黑名单、entity-aware 切分、确定性 fallback）**全部删除**，
LLM 只按约定 JSON 交卷，Python 侧只剩薄壳映射 + 惰性锚点。

**输出契约**（LLM 必须交的卷）：

```json
{
  "entities": [{"surface": "...", "canonical": "...", "type": "PERSON|...|OTHER"}],
  "relations": [{"subject": "...", "predicate": "...", "object": "...",
                 "object_type": "DATE|MONEY|PERCENT|QUANTITY|...", "evidence": "..."}]
}
```

- 实体类型仍是 **19 类封闭枚举**（12 通用 + 7 金融扩展 `FINANCIAL_INSTRUMENT/REGULATION/FINANCIAL_INDICATOR/MARKET/RISK/TRANS/SECURITY_CODE`）+ `OTHER` 逃生口（`EntityType`，校验保留）
- LLM **不输出** 偏移 / ID / `object_literal`
- 宾语 `object_type ∈ {DATE,MONEY,PERCENT,QUANTITY}` → 字面量（节点属性）

**惰性锚点**（确定性，留在 Python / 消费层，保证幂等与正确性）：

| 锚点 | 归属 |
|---|---|
| ID = sha256 内容寻址（mention / relation / triplet） | 薄壳 + `ids extract` |
| span 定位：对 surface 尽力 `find`，找不到给 `-1`（未定位，validate 放行） | 薄壳 |
| `object_literal` 由 `object_type` 派生 | 薄壳 + `build_kg` 强制修正 |
| canonical 去重（同一 canonical 收敛为同一实体） | `build_kg` |

**切分**：规则化 entity-aware 切分已删除；薄壳产出单个覆盖全文的 chunk，
超长文档的上下文控制由调用方送 LLM 前自行处理。

**Python 调用**：
- `smini ids extract --in 04-extraction.json` — 补算 `mention_id` / `triplet_id`（LLM 不给 ID）
- `smini validate extract --in 04-extraction.json` — Schema 校验 + 类型枚举校验 + span 校验

**降级（无确定性兜底）**：LLM 不可用 / 未配置 → 空结果 + 登记
`Degradation`（`no_credential` / `unavailable`），绝不静默、绝不造假。

---

### Step 5 · `smini-build-kg` —— 建图

**LLM 承担**：**实体消歧建议** —— 判定哪些 mention 应归一到同一 canonical name
（如「特斯拉」「Tesla Inc.」→ 同一实体）。

**承载的 semantica 规则**（源自 `steps/build_kg.py` + `types.py::KnowledgeGraph`）：
- `entity_id = sha256(type, canonical_name.strip().casefold())` →
  同批数据重跑 ID 不变（幂等）；不同文档抽到同一实体自动收敛
- `edge_id = sha256(subject_id, predicate, object_id|object_literal)`
- **FIXED_TX = 2000-01-01T00:00:00Z** 固定事务时间（不用 `ctx.now()`），
  否则每次 run 的 `recorded_at` 不同会让 `add_edge` 误判为新版本而无限追加
- `add_edge` 幂等规则：**temporal 相同才合并**（取置信度最大、来源并集），
  不同则追加为新版本（保留历史）
- 自环防卫：`subject == object` 的边丢弃
- 字面量边（object_literal）vs 实体边（object_id）互斥填充

**Python 调用**：`smini graph build --in 04-extraction.json --out 05-graph.json`
（ID 计算、幂等写入、双时态标记、统计重算——全部确定性，Skill 无法承担）

---

### Step 6 · `smini-qa` —— 质检

**LLM 承担**：
- **同义谓词归并**（「成立于」/「创立于」→ 同一规范谓词），使改写表达但同义的
  关系也纳入冲突检测
- **语义冲突识别**：`TYPE`（同实体互斥类型）/ `MUTEX`（一人不能同时是两公司 CEO）/
  `LOGIC`（出生晚于死亡）/ `TEMPORAL`（任期重叠）

**承载的 semantica 规则**（源自 `types.py::ConflictKind` + `steps/qa.py`）：
- **ConflictKind 5 类**：`VALUE / TYPE / TEMPORAL / MUTEX / LOGIC`
  （原版 `conflict_detector.py:60-95` 的 5 类，保留并显式化）
- **ResolutionStrategy 6 种**：`MOST_RECENT / HIGHEST_CONFIDENCE / MAJORITY_VOTE /
  SOURCE_PRIORITY / KEEP_ALL / MANUAL`
- 默认裁决：**有时态信息 → MOST_RECENT；否则 → MAJORITY_VOTE**
- 铁律：**落选边用 `temporal.invalidate(now)` 作废，绝不删除** —— 历史仍在
  `all_edges()` 可查，但不在 `current_edges()` 当前视图
- 去重：canonical_name / 别名相撞 → 集群；规范实体选 `max(len(mention_ids),
  len(properties), len(aliases))`；**被合并方的名字必须进 aliases，绝不丢弃**
  （原版 `entity_merger` 合并时丢别名，导致用旧名字查不到）
- `Resolution` **必须带 rationale** —— 无法解释的自动裁决不可接受

**关键契约：QA 必须在 Store 之前跑完**（原版先落库再检测冲突，落库的是未消歧数据）。
`QAResult.passed == false` 时 Store 必须拒绝落库。

**Python 调用**：`smini graph qa --in 05-graph.json --out 06-qa.json --pred-norm <归一表>.json`

---

### Step 7 · `smini-store` —— 存储

**几乎纯 Python**（幂等 upsert 是确定性语义，Skill 无法承担）：
- `smini graph store --in 06-qa.json --backend memory|json --out runs/<id>/store.json`

**Skill 承担**：后端选择规则 + **回执校验**（`StoreReceipt.idempotent` 必须为 `true`）。

**承载的 semantica 规则**（源自 `types.py::StoreReceipt`）：
- **强制 upsert 语义** —— 原版 Neo4j store 全用 `CREATE`（`MERGE` 零命中），
  重复构建会重复插入。这里要求所有 backend 实现 upsert，并回传
  `entities_written / updated / edges_written / updated` 让调用方能验证幂等。

---

### Step 8 · `smini-deliver` —— 交付

**LLM 承担**：查询理解（从 query 抽实体与意图）+ 在确定性 facts 之上合成中文答案。

**承载的 semantica 规则**（源自 `types.py::Citation` + `ContextPackage`）：
- **Citation 必须到字符区间**，且带 `quoted_text` 原文片段，
  让使用方能立刻校验（原版 provenance 只到「来自哪个文档」）
- `char_span` 默认 **orig 坐标系**，需经 `map_span_back()` 从 norm 换算
- 双向检索：主语视角 + **宾语视角**都要召回（否则查「特斯拉 总部」和
  查「谁的总部在帕洛阿托」结果不对称）
- `answer` 是**可选**字段，确定性 facts 才是基线 —— 默认 `None`，不影响幂等

**Python 调用**：`smini deliver --query "..." --in 06-qa.json --out 07-package.json`

---

## 4. Python 边界（明确清单）

**只有这 5 类留在 Python**，其余全部进 Skill / LLM：

| 模块 | 职责 | 为什么 Skill 做不到 |
|---|---|---|
| `ids.py` | sha256 内容寻址 ID | LLM 算不出 sha256；ID 错 = 图谱污染、幂等崩塌 |
| `spans.py` | SpanPatch 坐标映射 + 几何校验 | 逐字符精确计算，LLM 几乎必错且错得隐蔽 |
| `graph.py` | 建图 / 双时态 / 裁决 / 去重 | 幂等语义必须是确定性位运算级别的一致 |
| `validate.py` | JSON Schema 校验 + Parse 保真比对 | 长度/结构比对是机械判断，不该花 token |
| `retrieve.py` | 确定性检索 + Citation 组装 | 精确召回，不能靠 LLM 回忆图里有什么 |

**明确移交 Skill 的**（原 Python 硬编码逻辑）：
格式判断、版式识别、实体识别、关系抽取、伪实体过滤、谓词归并、
冲突语义判定、答案合成。

---

## 5. 主编排 Skill：`smini-pipeline`

**触发**：跑知识图谱流水线 / 从 X 建图 / semantica 流水线 / 全流程

**workflow**：
1. 创建 `runs/<run_id>/`（run_id = 时间戳 + source 摘要）
2. 按序 invoke：`smini-ingest` → `parse` → `normalize` → `extract`
   → `build-kg` → `qa` → `store` → `deliver`
3. 每步：写 `NN-*.json` → 调 `smini validate` 校验 → 失败则记 Degradation
4. 收尾：写 `manifest.json`（每步 mode / 耗时 / Degradation 清单 / 统计）
5. QA `passed=false` → **中止于 Store 之前**，报告未裁决冲突

---

## 6. 实施顺序（建议）

| 阶段 | 内容 | 价值 |
|---|---|---|
| **P0** | `contracts/*.schema.json` + Python 运行时瘦身（ids/spans/graph/validate/retrieve + cli 原子命令） | 契约先钉死，Skill 才有稳定接口 |
| **P1** | `smini-extract` Skill | 核心，最能体现「智能从硬代码挪进 Skill」 |
| **P2** | `smini-parse` Skill | 第二核心，版式规范可直接复用源码分析 |
| **P3** | `smini-ingest` Skill | 工具路由最复杂，需逐个对接宿主生态 |
| **P4** | normalize / build-kg / qa / store / deliver 五个 Skill | 规则均已就位，机械翻译 |
| **P5** | `smini-pipeline` 编排 + 端到端跑通 + 幂等验证 | 验收 |

**验收标准**：
- 用同一份文档连跑两次，`entity_id` 集合与 `edge_id` 集合**逐字节一致**（幂等）
- 每个 Skill 能独立触发、独立调试（不依赖上游内存状态）
- 任一 Skill 在 LLM 不可用时可降级到 Python fallback 并**显式登记 Degradation**

---

## 7. 已知边界

**已明确不处理**（用户 2026-09-03 裁定）：

| 边界 | 裁定 |
|---|---|
| 扫描版 PDF / 图片的 OCR | **不处理** —— Ingest 遇到无文本层的 PDF 直接显式降级，不做 OCR |
| 敏感文档送外部 LLM 的合规闸门 | **不存在敏感文档** —— 不设闸门，不提示确认 |
| 8 个 Skill 全 LLM 驱动的 token 成本 | **不考虑成本** —— 不提供 `--deterministic` 降本开关；确定性路径仅作 fallback 兜底 |

**仍需标注的边界**：

- **超长文档**：单次 LLM 上下文有限，extract Skill 中声明按 chunk 逐块抽取再合并
- **确定性 fallback 的定位**：不是"降本开关"，而是**模型不可用时的正确性兜底**，
  触发时必须登记 `Degradation`，绝不静默
