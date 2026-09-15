# smini 契约规范 v0.1

> 知识图谱流水线精简实现的**接口契约与数据结构**。
> 对应 semantica 官方 docs 站口径的 8 步：
> `ingest → parse → normalize → extract → build_kg → qa → store → deliver`

**本版状态：契约已冻结，实现未开始。**
类型层 `smini/types.py` 与协议层 `smini/protocols.py` 已完成并通过 50 项自验证测试
（`python -m unittest discover -s tests`），但 8 个 step 的算法实现一行未写。

**代码即契约**：本文描述的所有不变量，都已在 `types.py` 的 `__post_init__` 与
`tests/test_contracts.py` 中强制执行。文档与代码冲突时，**以代码为准**。

---

## 0. 怎么用这份文档

| 你想做什么 | 看哪里 |
|---|---|
| 实现某个 step | §3 对应小节 + `protocols.py` 的 `PipelineStep` |
| 加一个新的数据结构 | §2 坐标系与 ID 约定 → §3 对应 step |
| 判断某个字段该放哪 | §1 五条顶层决策 |
| 知道为什么这么设计 | 每节末尾的「与原版的差异」 |
| 还有哪些没定 | §7 待决问题 |

---

## 1. 五条顶层决策

这五条是全部契约的地基。任何与它们冲突的"便利设计"都应当被拒绝。

### 决策 1 · ID 由内容决定，绝不用 `hash()`

```
原版  semantica/kg/graph_builder.py:608   entity_id = str(hash(str(item)))
本版  smini/ids.py                        entity_id = sha256(type ‖ name)
```

Python 的 `hash()` 对 str **带随机盐**（`PYTHONHASHSEED` 默认随机）。同一实体名
在两次进程里算出两个 ID，后果是增量构建时图谱无限膨胀、跨机器无法对齐、
测试不可复现。

代价：sha256 比 `hash()` 慢约一个数量级。对"ID 即身份"的知识图谱，这个交换
是唯一正确的选择。

已验证：`tests/test_contracts.py::test_cross_process_stability` 用
`PYTHONHASHSEED ∈ {0, 1, 12345, random}` 起 4 个子进程，断言输出完全一致；
并有 `test_hash_builtin_is_indeed_unstable` 反证内置 `hash()` 确实会变。

### 决策 2 · 原始内容以 `bytes` 承载，编码推断推迟到 Parse

`RawDocument.content: bytes`。Ingest 只做三件事：拿字节、算校验和、记出处。

原版在 ingest 阶段就解码成 str，猜错编码后全部失真且无从恢复。编码推断需要
格式线索（HTML 的 charset、PDF 的嵌入字体），只有 Parse 步才有。

### 决策 3 · 偏移必须可回溯到原文

从 Parse 起，所有 span 锚定到明确的坐标系；Normalize 造成的坐标漂移由
`SpanPatch` 记录，可经 `map_span_back()` 逆向映射回原文。

原版没有这个机制 —— Extract 出的实体无法回答"这句话出自原文哪里"，溯源自
第一步就断了。

### 决策 4 · 降级必须出声

能力缺失（没装模型、没 API key、额度耗尽）写进 `Degradation` 并附 `impact`；
需要硬失败时抛 `DependencyMissing`。

原版是静默降级（`semantic_extract/ner_extractor.py`）：spaCy 模型没装就退回正则，
用户以为正常跑了，图谱里却少了一半实体，且无从察觉。

### 决策 5 · 状态显式化，依赖从声明推导

步骤间数据用 `PipelineState`（有 schema 的 dataclass），不用 dict。
每步声明 `reads` / `writes`，执行顺序**由声明反推**并在构造期校验。

原版用单个 `current_data` dict 传数据（`pipeline/execution_engine.py`），拼错
key 只能运行时炸；DAG 依赖边手工维护，写错了也只能运行时发现。

---

## 2. 坐标系与 ID 约定

### 2.1 三套坐标系

| 坐标系 | 载体 | 谁产生 |
|---|---|---|
| bytes | `RawDocument.content` | Ingest |
| **orig** | `ParsedDocument.text` 的字符偏移 | Parse |
| **norm** | `NormalizedDocument.text` 的字符偏移 | Normalize |

**默认约定：`Provenance.char_span` 是 norm 坐标系。**
需要展示给用户时，用 `NormalizedDocument.to_original_span(span)` 换算回 orig，
再从 `ParsedDocument.text` 切片取出原文。

### 2.2 SpanPatch 的几何约束

`SpanPatch(orig_start, orig_end, new_start, new_end, ...)` 必须满足：

1. 在 new 坐标系下按 `new_start` 升序
2. 在 new 坐标系下区间不重叠
3. **`new_start == orig_start + 前面所有 patch 的 delta 之和`**

第 3 条最易出错。`validate_patches()` 在入口拦截 —— 违反时映射会静默偏移几个
字符，极难排查（写本文档配套的测试时就踩了两次）。

落在替换区内部的偏移无法精确还原，取**保守策略**：左端映射到区间起点、右端
映射到区间终点，保证映射回来的原文区间一定覆盖真实位置。

### 2.3 ID 生成规则

全部走 `smini/ids.py`，用 `\x1f`（ASCII Unit Separator）分隔字段，
避免 `("a:b","c")` 与 `("a","b:c")` 碰撞。

| 对象 | 构成 | 前缀 |
|---|---|---|
| 文档 | `sha256("doc" ‖ sha256(content))` | `d_` |
| 来源 | `sha256("src" ‖ uri)` | `s_` |
| Chunk | `sha256("chunk" ‖ doc_id ‖ start ‖ end)` | `c_` |
| Mention | `sha256("mention" ‖ doc_id ‖ start ‖ end ‖ surface)` | `m_` |
| **Entity** | `sha256("entity" ‖ type ‖ casefold(canonical_name))` | `e_` |
| **Edge** | `sha256("edge" ‖ subj ‖ pred ‖ obj_id ‖ obj_literal)` | `r_` |
| Conflict | `sha256("conflict" ‖ subj ‖ pred)` | `x_` |

两条最重要的推论：

* **文档 ID 是内容寻址的** —— 同一份 PDF 从两个路径进来是同一个文档。
* **实体 ID 只由 (类型, 规范名) 决定** —— 同一实体跨文档自动收敛为同一节点；
  同批数据重复 build 节点数不变；实体合并 = 共享 canonical_name 后按 ID 归并。

---

## 3. 八步契约

### 通用签名

```python
class PipelineStep(ABC, Generic[I, O]):
    name: str                      # 同时是 ctx.config 的键
    reads: tuple[str, ...]         # 读 PipelineState 的哪些字段
    writes: tuple[str, ...]        # 写哪些字段

    def select(self, state) -> I: ...          # 胶水
    def transform(self, inputs, ctx) -> O: ...  # 纯函数，全部业务逻辑
    def commit(self, state, output) -> None: ...# 胶水
```

`transform` 保持纯粹：不读写 state、不碰全局时钟、不产生随机性。
单测只需 `step.transform(fixture, ctx)`，不必跑通上游。

---

### Step 01 · Ingest

```
reads  = ()
writes = ("raw",)
transform(inputs: IngestRequest, ctx) -> list[RawDocument]
```

**输入** `IngestRequest`：`sources: list[SourceSpec]`，每个 spec 是一个 uri
（`file:///…` / `https://…` / `postgres://…` / `memory://…`）+ 可选 glob / 递归开关。

**输出** `RawDocument`

| 字段 | 类型 | 说明 |
|---|---|---|
| `doc_id` | str | 内容寻址，`d_` 前缀 |
| `source` | SourceRef | uri / source_type / checksum / fetched_at / locator |
| `media_type` | str | MIME，用于 Parse 路由 |
| `content` | **bytes** | 原始字节，永不解码 |
| `checksum` | str | sha256(content)，自动填充 |
| `fetched_at` | datetime | 必须来自 `ctx.now()` |

**不变量**
* `content` 非空时 `checksum` 与 `size_bytes` 自动填充
* 绝不因内容异常而抛错 —— Ingest 只搬运，不解读；解析失败是 Parse 的事

**与原版的差异**：原版 `content` 过早解码为 str（决策 2）；原版 SSRF 防护
（`ingest/ssrf.py`，753 行）在 web 路径逐跳校验，本版 web 源若纳入 v1
需移植该守卫。

---

### Step 02 · Parse

```
reads  = ("raw",)
writes = ("parsed",)
transform(inputs: list[RawDocument], ctx) -> list[ParsedDocument]
```

**输出** `ParsedDocument`：`text` + `blocks`（保序，每个带 orig 坐标系 span）
+ `parser` / `parser_version` + `warnings`。

`Block` 的关键字段：`kind`（段落/标题/表格/代码/列表/图注…）、`order`、
`char_start`/`char_end`、**`rows`**（表格结构化内容）、`language`（代码语言）。

**不变量**
* `blocks` 按 `order` 升序
* 所有 block 的 span 落在 `[0, len(text)]` 内
* block 的 `text` 是 `ParsedDocument.text` 的切片

**格式路由**：`DocumentFormat` 枚举 → 具体实现。v1 建议只做
`TEXT` / `MARKDOWN` / `JSON` / `CSV` / `HTML`，PDF 与 DOCX 列为可选
（原版 `pdfplumber` / `python-pptx` / `pytesseract` 都**没写进 `pyproject.toml`**
依赖，干净安装下运行时报错 —— 这是原版的坑，不是我们必须继承的）。

**与原版的差异**：新增 block 级 span（原版 parse 输出无偏移）；新增
`parser_version`，让"换了解析库导致抽取结果变化"可被追溯。

---

### Step 03 · Normalize

```
reads  = ("parsed",)
writes = ("normalized",)
transform(inputs: list[ParsedDocument], ctx) -> list[NormalizedDocument]
```

**输出** `NormalizedDocument`：`text`（norm 坐标系）+ `blocks`（span 已重映射）
+ **`patches`** + `mentions`（顺手识别的规则实体）+ `warnings`。

**必须产出 `patches`** —— 这是溯源链能闭合的唯一依据（决策 3）。

`mentions` 填的是**规则就能高精度拿到**的字面量实体：日期、金额、百分比、
数量（仅无 LLM 时）。实体/关系抽取已全部交给 Extract 步的 LLM（契约即规则），
此处不再承担 NER，也不为 entity-aware 分块提供依据（该切分已删除）。

**顺序**：Unicode NFC → 空白折叠 → 特殊字符 → 日期标准化（→ISO8601）→
数值标准化（`1.5K` → `1500`）→ 别名归一。
每步都产出 SpanPatch。

**与原版的差异**：原版四步链（`text_normalizer.py:120`）没有 patch 记录；
原版把实体归一化（`EntityNormalizer`，567 行）也放在这一步，本版**推迟到 QA**
—— 归一化和消歧都需要跨文档信息，放在单文档阶段做不了。

---

### Step 04 · Extract（含 Split）

```
reads  = ("normalized",)
writes = ("extractions",)
transform(inputs: list[NormalizedDocument], ctx) -> list[ExtractionResult]
```

**关于 Split 的归属**：官方 A 版 8 步没有独立 Split 步（B 版 README 才有）。
本版把分块作为 Extract 的**内部第一阶段**，不单独占一步 —— 分块的唯一消费者
是抽取，拆成独立步只会增加一次无谓的状态传递。

**输出** `ExtractionResult`

| 字段 | 说明 |
|---|---|
| `chunks` | 切分结果，含 `mention_ids` |
| `mentions` | 实体提及（未消歧），mention 级别 |
| `relations` | mention 之间的关系 |
| `triplets` | (s, p, o)，`object_literal` 标记宾语是否为字面量 |
| `degradations` | **必填语义**：能力缺失的显式声明 |
| `stats` | 各阶段计数 |

**Mention 与 Entity 的区分**（原版此处含混，导致去重与抽取逻辑纠缠）：

| | 定义 | ID 由什么决定 |
|---|---|---|
| `EntityMention` | 文本里的一处**出现** | (doc, span, 表面形式) |
| `Entity` | 图谱里的一个**节点** | (type, 规范名) |

一个 Entity 对应零到多个 Mention。

**分块策略（契约即规则）**：规则化 entity-aware 切分已删除。薄壳产出单个
覆盖全文的 chunk（仅用于统计/溯源）；超长文档的上下文控制由调用方在送
LLM 前自行处理。

**降级链（契约即规则）**：抽取语义全部交给 LLM，**不存在确定性兜底**。
无/不可用 LLM → 空结果 + `Degradation`（`no_credential` / `unavailable`），
绝不静默、绝不造假。

**与原版的差异**：原版降级静默；原版 `Triplet` 没有 `object_literal` 标记，
建图时无法判断该加边还是加属性。本版 `object_literal` 由 `object_type`
派生，并在 Build KG 消费层强制修正（惰性锚点）。

---

### Step 05 · Build KG

```
reads  = ("extractions",)
writes = ("graph",)
transform(inputs: list[ExtractionResult], ctx) -> KnowledgeGraph
```

**流程**：收集全部 mention → 归一为 canonical_name → 生成确定性 `entity_id`
→ 建节点 → 三元组转边（字面量宾语转成节点**属性**）→ `recompute_stats()`。

**输出** `KnowledgeGraph`：`entities: dict[str, Entity]` +
`edges: dict[str, list[KGEdge]]` + `stats: GraphStats`。

用 dict 而非 list 是刻意的选择：`entity_id` 的内容寻址特性让"合并"退化成
一次 dict 键碰撞，这是决策 1 带来的直接收益。

**双时态（v1 纳入）**。实体与边都可挂 `temporal: BiTemporal | None`，描述
`valid_time`（事实何时为真）与 `transaction_time`（系统何时采信）。实现要点：

* `edges` 的值是**版本列表**而非单条边 —— 同一条 `(s,p,o)` 在不同 valid_time
  下是不同事实版本，必须都留着，`at(valid_time)` 才能按时点回看历史。
* `add_edge` 的幂等规则与单时态不同：**temporal 相同 → 合并（置信度取最大、
  来源合并）；temporal 不同 → 追加为新版本**。这保证"重复 build 同一批数据"
  仍幂等，同时"事实演进"留下多版本。
* `Entity.temporal` 描述实体的存在期（如某公司 2020-2023 存续）；实体**身份**
  不随时间变，`entity_id` 不含时间维度。
* `at(valid_time, tx_time=None)`：返回该时点的图谱快照（新 `KnowledgeGraph`，
  只含成立且已被采信的内容）。`supersede()`：作废当前版本并追加新版本，**修正
  而非覆盖**，旧版本仍可经 `at()` 查到。

`GraphStats` **是契约的一部分，不是可选日志**：节点数突然腰斩 = 上游挂了，
没有统计就没法发现。`orphan_entity_count` 是抽取失败的重要信号。

**不变量**
* `KGEdge` 的 `object_id` 与 `object_literal` 至少一个非空（否则抛
  `ContractViolation`）
* `add_entity` 幂等：重复写入按策略合并；`add_edge` 按双时态规则合并/追加
* 合并时被合并方的 `canonical_name` **必须进 aliases**

**与原版的差异**：原版 `merge_entities` 默认 `False`（`graph_builder.py:57`），
前 7 步的去重结果默认不生效；原版合并时会丢别名；原版只有 `temporal_model.py`
的 `BiTemporalFact` 独立实体、未接入建图主路径。本版把双时态扁平化为可挂在
任意对象上的标记，且从类型上保证 QA 先于 Store。

---

### Step 06 · QA（冲突检测 + 去重）

```
reads  = ("graph",)
writes = ("qa",)
transform(inputs: KnowledgeGraph, ctx) -> QAResult
```

**QA 必须在 Store 之前。** 这是本版相对原版最重要的顺序修正：

```
原版 graph_builder.py
  :818  node_count = self.graph_store.add_nodes(resolved_entities)   ← 先落库
  :835  edge_count = self.graph_store.add_edges(formatted_edges)
  :844  detected_conflicts = self.conflict_detector.detect_conflicts(...)  ← 后 QA
```

原版先写库再跑冲突检测，而 `resolve_conflicts` 是就地改内存 —— **库里存的是
未消歧的数据**，冲突清单还不落盘。本版把 `graph`（已修复的图）作为 `QAResult`
的字段返回，从类型上就不可能再犯这个错。

**QA = 冲突检测 + 去重**，这两块正好是 B 版 README 的第 6、7 步
（`docs/getting-started.md:200` 明确 Quality Layer = `deduplication` + `conflicts`）。

**冲突检测**
* 5 类：`VALUE` / `TYPE` / `TEMPORAL` / `MUTEX` / `LOGIC`
* 判定：同一 `(subject, predicate)` 下候选值去重后 > 1 即冲突
* `conflict_id` 只由 (s, p) 决定 —— 所有候选值汇聚成**一个**冲突对象
* 裁决策略：`most_recent` / `highest_confidence` / `majority_vote` /
  `source_priority` / `keep_all` / `manual`
* **`Resolution` 必须带 `rationale`** —— 无法解释的自动裁决不可接受

**去重**
* 流程：Blocking → 多因子打分 → 阈值判定 → 聚类 → 合并
* 打分权重沿用原版验证过的配比：Jaro-Winkler 0.6 / 属性 0.2 / 关系 0.2，
  阈值 0.7（`deduplication/similarity_calculator.py:220`）
* 聚类用并查集
* 合并策略默认 `KEEP_MOST_COMPLETE`

**⚠️ 已知要修的原版缺陷**：dedup 的 blocking 默认只按**首字母**分块，
"IBM" 与 "International Business Machines" 会漏检。本版 blocking 键应改为
**首字母 + token 集合 + 类型**的复合键。

**闸门**：`QAResult.passed`（= `unresolved_count == 0`）。
**Store 步应当拒绝 `passed=False` 的图。**

---

### Step 07 · Store

```
reads  = ("qa",)
writes = ("receipt",)
transform(inputs: QAResult, ctx) -> StoreReceipt
```

**三个平行的 store，分工明确：**

| Store | 存什么 | 默认后端 |
|---|---|---|
| `GraphStore` | 图结构（节点/边） | memory（可选 neo4j 等 LPG） |
| `VectorStore` | 文本/实体向量 | memory（可选 faiss） |
| TripletStore | **不纳入 v1** | —— |

**为什么砍掉 TripletStore**：原版的 triplet store 只服务 RDF 系后端
（blazegraph / oxigraph），且 `compute_delta` 的 SPARQL 在闭合尖括号前多了个
空格（`triplet_store.py:680`），"新增"方向必然解析失败。v1 只做 LPG 语义。

**强制 upsert 语义**（原版 Neo4j 全用 `CREATE`，`MERGE` 零命中，
重复 build 重复插入）：
```python
def upsert_entities(self, entities) -> tuple[int, int]:  # (新增数, 更新数)
```
返回两个计数，让幂等性**可被验证**而不只是被声称。

**向量维度硬校验**：`VectorStore.__init__(dimension)` 声明维度，
`upsert()` 入口校验，不匹配抛 `DimensionMismatch`。

原版 `VectorStore` 默认 768 维而默认 embedder 是 384 维（两者天生不匹配）；
更糟的是 embedding 失败时**返回随机向量**只打一条 WARNING
（`vector_store.py:303-307`）—— 索引可检索但语义完全无意义，且极难排查。
本版绝不让脏数据进索引。

**`VectorRecord.payload` 必须带 `entity_id`** —— 向量库与图谱的连接点就在这里，
缺了它 RAG 只能返回无出处的文本。

---

### Step 08 · Deliver

```
reads  = ("qa", "receipt")
writes = ("delivered",)
transform(inputs: DeliveryRequest, ctx) -> ContextPackage
```

**输出** `ContextPackage`：`query` + `facts`(Triplet) + `entities` +
**`citations`** + `retrieval` 方式 + `degradation_notes`。

`Citation` 要求到**字符区间**并给出 `quoted_text` 原文片段 —— 原版的
provenance 只到"来自哪个文档"，本版要求到"原文哪一句"，让使用方能立刻校验。

**设计取舍：只做检索与组装，不做推理。**
原版 `ContextGraph` 的 `find_similar_decisions` / `analyze_decision_impact`
属于推理范畴，其"潜在因果"是启发式（共现实体 + 时间更早），并非真推理。
v1 不复制这条路径，留给上层应用自己决定。

**混合检索**：`rrf_fuse(*ranked_lists, k=60)`，
`score(d) = Σ 1/(k + rank)`。沿用原版 `hybrid_search.py:148-167`
的公式与默认 k —— 这是混合检索里验证最充分的融合方式，没有理由自创。

---

## 4. 编排契约

```python
Pipeline(steps=[...])                     # 构造期调 validate_step_order()
    .run(state=None, ctx=None,
         stop_after=None, fail_fast=True)
        -> (PipelineState, list[StepResult])
```

**依赖从声明推导**：某步 `reads` 的每个字段，必须由**在它之前**的某步 `writes`。
三类错误在**构造期**就报：
* 读了没人生产的字段
* 某字段被两步重复写入（后者会静默覆盖前者）
* 步骤未声明 `name`

**运行期前置检查**：`reads` 的字段若为 `None` 或空集合，立刻失败并指明
"上游哪一步产出 0 条"。宁可在这里崩，也不要让空数据静默流向下游。

**刻意不做 DAG 并行调度。**
原版有 `ParallelismManager` 和 Kahn 拓扑排序，但这 8 步是**严格线性**的 ——
每步都依赖上一步的全部产出，没有任何可并行分支。为天然线性的流程引入拓扑
排序只会增加复杂度。真正值得并行的是**步内**的文档级处理（1000 个 PDF 的解析
可以并行），那是各 step 实现自己的事。

**时间必须注入**：`RunContext.now()`。直接调 `datetime.now()` 会让流水线无法
复现 —— 三个月后重跑，双时态模型的 transaction_time 就不同。`ctx.seed` 同理。

---

## 5. 错误语义

| 异常 | 语义 | 该不该捕获 |
|---|---|---|
| `ContractViolation` | **编程错误**，不变量被破坏 | 不该，让它崩 |
| `StepError` | step 执行失败，带 step 名与 cause | 编排层捕获，决定是否重试 |
| `DependencyMissing` | 可选依赖/凭据缺失 | 调用方决定降级还是失败 |
| `DimensionMismatch` | 向量维度不匹配 | 不该，脏数据不能进索引 |

**默认应当失败而非静默降级。** 静默降级是知识图谱质量事故的头号来源 ——
原版正是这么做的，结果就是"跑了但结果不能用，且不知道为什么"。

---

## 6. 验证方式

```bash
python -m unittest discover -s tests -v
```

50 项测试，覆盖 8 条契约承诺：

1. **确定性 ID** —— 4 个不同 `PYTHONHASHSEED` 的子进程算出同一 ID；
   并有反证测试确认内置 `hash()` 确实不稳定
2. **溯源不变量** —— confidence 范围、char_span 合法性、来源合并去重
3. **坐标可逆** —— 变长/变短/多 patch 累积/保守包含/端到端往返；
   并验证 `validate_patches` 能抓到不自洽数据
4. **建图幂等** —— 重复 add 不膨胀、合并保留别名、边置信度取最大
5. **顺序校验** —— 缺生产者/重复写入/未命名/空上游/禁用步/注入时钟
6. **维度硬校验** —— 不匹配抛错、匹配通过、upsert 幂等
7. **降级出声 + QA 闸门** —— `is_degraded`、`passed`
8. **可序列化** —— enum/datetime/bytes/tuple-key 全部 JSON 友好

---

## 6.5 能力提供方契约（Embedder / LLMProvider）

这两个不是"步骤"，而是 Step 04（Extract）和 Step 07（Store）依赖的**能力**，
被单列为 ABC（`smini.protocols`）。实现层可注入任意后端（spaCy / 本地模型 /
远程 API），契约层不关心。唯一硬性规定：

> **能力缺失时抛 `DependencyMissing`，绝不返回垃圾结果。**

这是相对原版的核心修正 —— 原版 `ner_extractor.py` 在 spaCy 缺失时静默退回正则，
图谱里悄悄少了一半实体，出了问题无从排查。本版把"降级"显式化：能力方抛错，
调用方决定是登记 `Degradation` 还是失败，**默认应当失败**。

```
class Embedder(ABC):
    dimension: int                      # 构造时声明，写入前强制校验
    def embed(texts) -> list[tuple[float, ...]]   # 失败抛 DependencyMissing
    def _check_dim(vectors)             # 维度不符抛 DimensionMismatch

class LLMProvider(ABC):
    model: str
    def is_available() -> bool          # 构造期即可知能否用
    def extract(prompt, schema) -> dict # schema 约束输出结构；不可用抛 DependencyMissing
```

* `Embedder` 禁止返回随机向量（原版 `vector_store.py:303-307` 的坑）。
* `LLMProvider.extract` 接受 JSON Schema 约束输出，调用方负责把 dict 映射成
  `Entity`/`Relation`/`Triplet`，避免"LLM 自由发挥导致下游解析失败"。
* v1 默认**不接 LLM**（全确定性抽取），但接口必须在契约里就位，否则日后接入
  要改 Extract 的签名。

---

## 7. 待决问题

以下项已在本轮回合拍板并落到类型层；未决项仍待你确认。

| # | 问题 | 当前决定 | 状态 |
|---|---|---|---|
| 1 | **Split 是否独立成步** | 并入 Extract 内部（官方 A 版 8 步本无 Split） | ✅ 已决 |
| 2 | **双时态是否纳入 v1** | 纳入：`Entity`/`KGEdge` 挂 `temporal: BiTemporal`；`edges` 改版本列表；`at()` 快照已实现 | ✅ 已决·已实现 |
| 3 | **事件抽取是否做** | 否（v1 不做 EventDetector） | ⏳ 待决 |
| 4 | **共指消解是否做** | 否（原版是启发式，质量存疑） | ⏳ 待决 |
| 5 | **LLM 抽取是否纳入 v1** | 否，v1 全确定性；`LLMProvider` 抽象与降级链已留接口 | ✅ 已决 |

> 说明：第 2 项"双时态纳入 v1"会让 `Entity`/`KGEdge` 携带时间维度，属于
> 破坏性改动，**必须先于实现落定**（事后加是破坏性重构）。本回已先行补完。
> 若你其实想要"v1 不做双时态"，请告诉我，我会回退 `temporal` 字段与
> `edges` 多版本逻辑（约 120 行 + 7 个测试）。

仍待确认的范围问题：

* **PDF / DOCX 解析做不做** —— 原版这两个的依赖没写进 `pyproject.toml`，
  干净安装会运行时报错。做的话得自己声明依赖。
* **Web 源做不做** —— 做的话需要移植 SSRF 守卫（原版 753 行），不做的话
  ingress 只需 file + memory 两种。

另外两个需要你确认的范围问题：

* **PDF / DOCX 解析做不做** —— 原版这两个的依赖没写进 `pyproject.toml`，
  干净安装会运行时报错。做的话得自己声明依赖。
* **Web 源做不做** —— 做的话需要移植 SSRF 守卫（原版 753 行），不做的话
  ingress 只需 file + memory 两种。

---

## 附：文件清单

```
smini/
  __init__.py       导出面（契约的公开 API）
  ids.py            确定性 ID —— 决策 1 的落地点
  types.py          全部数据结构 + 不变量 + 坐标映射
  protocols.py      PipelineStep / PipelineState / RunContext / Store 抽象
tests/
  test_contracts.py 50 项自验证
```