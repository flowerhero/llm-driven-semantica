# smini — 实现说明（IMPLEMENTATION）

> 在 WorkBuddy 中可运行的 **semantica 8 步知识图谱流水线精简实现**。
> 确定性、零外部依赖、双时态、内容寻址。
>
> 配套文档：[`CONTRACTS.md`](CONTRACTS.md)（每一步的接口契约与数据结构）、
> [`semantica-8步流水线源码解析.md`](semantica-8步流水线源码解析.md)（原版源码解析）。

---

## 1. 它是什么

把一段文本（或一批文件）构建成一张**可查询的知识图谱**，并在最后一步按查询
组装成可直接喂给 Agent 的 `ContextPackage`（带出处引用）。

8 步（docs 站口径）：

```
ingest → parse → normalize → extract → build_kg → qa → store → deliver
```

- **确定性**：默认纯正则 / 规则抽取，同一输入产出逐字节相同的图谱（实体 ID 内容寻址）。
- **零依赖**：仅用 Python 标准库（`dataclasses` / `re` / `hashlib` / `unittest`）。
  不依赖 PyTorch / spaCy / Neo4j / 任何 LLM。
- **双时态**：每条事实带 `valid_time`（何时为真）+ `transaction_time`（何时被录入/作废）。
- **降级显式**：v1 走确定性路径，不假装有 LLM 抽取；若未来接 LLM 失败，必须显式登记 `Degradation`。

---

## 2. 目录结构

```
smini/
├── __init__.py          # 公共 API：build_pipeline / make_context + 全部类型/步骤
├── types.py             # 全流水线统一数据契约（dataclass 层）
├── protocols.py         # PipelineStep 三段式契约 + Pipeline 编排 + 抽象 backend
├── ids.py               # 确定性内容寻址 ID 生成器（sha256）
├── stores.py            # MemoryGraphStore（默认内存后端，upsert 语义）
├── llm.py               # 「智能挪进大模型」唯一接缝：LLMProvider + JSON Schema + 测试桩
├── ingestors.py         # 摄取可插拔抽象（LocalFileIngestor 兜底 + agent 工具直投）
├── pipeline_factory.py  # build_pipeline() / make_context() 组装标准 8 步
├── cli.py               # `python -m smini.cli build ...` 命令行入口
├── __main__.py          # 转发到 cli.main()
└── steps/
    ├── ingest.py        # Step 1 确定性原语（resolve_source 等；IngestStep 在 ..ingestors）
    ├── parse.py         # Step 2（LLM 结构化 + 版式，确定性兜底 + 保真校验）
    ├── normalize.py     # Step 3（LLM 接管时跳过规则实体）
    ├── extract.py       # Step 4（LLM NER + 类型化关系，确定性兜底 + Degradation）
    ├── build_kg.py      # Step 5（保留确定性）
    ├── qa.py            # Step 6（可选 LLM 谓词归一，确定性默认）
    ├── store.py         # Step 7（保留确定性）
    └── deliver.py       # Step 8（可选 LLM 答案合成，确定性检索默认）
```

---

## 3. 核心契约：三段式 PipelineStep

每个步骤都是 `PipelineStep[I, O]`，用三个方法表达「读什么 / 算什么 / 写什么」：

```python
class PipelineStep(Generic[I, O]):
    name: str                       # 步骤名（也是状态字段名）
    reads: tuple[str, ...]          # 声明从 state 读哪些字段
    writes: tuple[str, ...]         # 声明写回 state 哪些字段

    def select(self, state) -> I:   # 从 PipelineState 取出本步输入
    def transform(self, inputs, ctx) -> O:   # 纯函数：输入 → 输出
    def commit(self, state, output) -> None: # 把输出写回 state.writes
```

`execute()` 是模板方法：`_check_precondition`（reads 非空）→ `select` → `transform` → `commit`。
编排层用 `reads`/`writes` 反推依赖顺序（`validate_step_order`），构造期就能发现顺序错。

**为什么三段式**：把「取数」和「落数」与「计算」分开，计算保持纯函数，
便于测试、便于在冲突/去重时就地修复（QA 步直接改 `state.qa.graph`）。

---

## 4. 各步职责（实现要点）

| 步 | 输入 → 输出 | 关键实现 |
|----|------------|---------|
| **ingest** | source spec → `list[RawDocument]` | 支持 `text://` / `file://` / 纯路径 / inline；Web 源显式抛 `StepError`（不偷偷下载）。`RawDocument` 原样存二进制 + sha256。 |
| **parse** | `RawDocument` → `list[ParsedDocument]` | 多编码回退（utf-8→gb18030→latin-1）；行式 `Block` 扫描锚定 text 偏移；PDF/DOCX 等二进制降级为占位 `ParsedDocument(parser="unsupported")`。 |
| **normalize** | `ParsedDocument` → `list[NormalizedDocument]` | NFC 组合、控制符剥离、空格折叠；用 `SpanPatch` 记录坐标漂移，**溯源不丢**；规则抽取 DATE/MONEY/PERCENT/QUANTITY 实体。 |
| **extract** | `NormalizedDocument` → `list[ExtractionResult]` | 启发式 NER（PERSON/ORG/LOCATION/PRODUCT）+ 正则关系抽取。宾语若无现成 mention 则**就地合成 mention**，保证图谱长出边。 |
| **build_kg** | `ExtractionResult` → `KnowledgeGraph` | `entity_id` 内容寻址保证幂等；边用固定事务时间 `FIXED_TX`，重跑不追加新版本。字面量宾语 → `edge_id(subj, pred, None, lit)`。 |
| **qa** | `KnowledgeGraph` → `QAResult` | 冲突检测（同 `(subject,predicate)` 多值）→ `Conflict`；裁决 MAJORITY_VOTE 或 MOST_RECENT；落选边**双时态作废**（保留历史）。去重：别名/规范名相撞合并，重指边。 |
| **store** | `QAResult` → `StoreReceipt` | 默认 `MemoryGraphStore`，`upsert_entities/upsert_edges` 返回 `(新增,更新)`，验证幂等。可选注入 `Embedder`/`VectorStore`（缺省不造假向量）。 |
| **deliver** | `QAResult` + query → `ContextPackage` | **双向检索**：实体作主语与作宾语都召回；每条事实带 `Citation`（出处 + 原文区间 + 置信度）。无 query 时产出空包（交付是可选的最后一步）。 |

---

## 5. 关键设计决策

### 5.1 内容寻址 ID（`ids.py`）
所有 ID 由内容 sha256 生成（字段间用 `\x1f` 分隔）：
`stable_id` / `content_hash` / `doc_id` / `chunk_id` / `mention_id` /
`entity_id(type, canonical)` / `edge_id` / `triplet_id` / `conflict_id`。
**好处**：「合并」退化成一次 dict 键碰撞，重跑同一批数据天然幂等。

### 5.2 双时态（`BiTemporal` / `TimeInterval`）
- `valid_time`：事实在现实世界哪段为真。
- `transaction_time`：系统哪段采信它（`recorded_at` → `invalidated_at`）。
- 边相同 `(subject,predicate,object)` 但**不同时态** → 追加为新版本；相同时态 → 合并置信度。
- `KnowledgeGraph.at(valid_time, tx_time)` 给历史时点的图谱快照（快照内被采信的版本即「当前」，已修正 aliasing 污染）。

### 5.3 确定性抽取 + 降级显式化
v1 默认不接 LLM，纯规则抽取。任何「能力缺失」都**必须登记 `Degradation`**，
绝不在没有向量时伪造 `Embedder` 输出。这让调用方能区分「真没抽到」和「降级了」。

### 5.4 Deliver 双向检索
`KnowledgeGraph.neighbors(entity_id)` 同时返回实体作主语与作宾语的边。
查询「特斯拉」既能拿到 `特斯拉—[位于]→加州`（特斯拉是主语），也能拿到
`马斯克—[CEO]→特斯拉`（特斯拉是宾语）——单向 `out_edges` 会漏掉后者。

### 5.5 冲突裁决不删边
QA 落选边用 `temporal.invalidate(now)` 作废（保留历史），而非物理删除。
`current_edges()` 严格只返回未作废版本，被作废事实仍在 `all_edges()` 可查。

### 5.6 「宿主 agent + 大模型驱动」混合模式（智能从硬代码挪进 LLM）

本实现不是把 semantica 翻译成确定性 Python，而是**把智能尽量挪进大模型/skill，
确定性骨架只保留类型 / 协议 / 双时态 / 内容寻址 ID / 存储**。两种接法产出
**同一结构化 dict**，step 的映射函数不变——`LLMProvider.extract` 是唯一接缝。

**哪些步走了 LLM 路径**（注入 `llm` 且 `is_available()` 为真时自动启用）：

| 步 | LLM 承担什么 | 兜底 | 失败时 |
|----|------------|------|--------|
| **ingest** | 由宿主工具（browser / pdf / office / 资料库）产出 `RawDocument`，经 `ctx.raw_docs` 直投 | `LocalFileIngestor`（text/file/inline） | 无工具则读本地 / Web 源显式 `StepError` |
| **parse** | 抽「统一结构化文本 + 版式区块」（`llm.v1`） | 行式 `Block` 扫描（`smini.line.v1`） | 长度偏差 > 5% 抛 `StepError` → 回退确定性 |
| **normalize** | LLM 接管时跳过规则实体（避免重复） | 规则 DATE/MONEY/… | — |
| **extract** | NER + 类型化关系（`llm.ner.v1` / `llm.rel.v1`） | 启发式 NER + 正则关系 | 回退规则 + `Degradation(extract.llm)` |
| **build_kg** | —（保留确定性） | 内容寻址 ID | — |
| **qa** | 谓词同义归并（「成立于」「创立于」→「成立于」） | 恒等（不改确定性基线） | 失败回退恒等 |
| **store** | —（保留确定性） | upsert | — |
| **deliver** | 在确定性 facts 之上合成中文答案（`ContextPackage.answer`） | 仅确定性检索 | 失败返回 None |

**铁律（保证重跑幂等）**：LLM **只「提议」事实**（自由文本 surface + candidate
canonical + 关系三元组）；`entity_id(type, canonical)` / `edge_id` / `mention_id` /
`triplet_id` 的 sha256 哈希**必须由 Python 算**。`ExtractStep._mentions_from_llm` /
`_relations_from_llm` 把模型 dict 映射成 `EntityMention` / `Relation` / `Triplet` 后，
全部 ID 在 Python 侧由内容生成——所以同一批数据 + 温度 0 重跑，图谱严格不变。

**两种接线方式（接法 A / B 产同一 dict）**：

- **A. 宿主 agent（豆包）充当 LLM（方式 3 · 生产默认）**：`HostAgentLLMProvider`
  注入宿主按契约产出的 dict（`{entities, relations}`）。CLI 用 `--host-contract
  <file>` 传入契约 JSON；库调用用 `HostAgentLLMProvider().inject(contract)`。
  **不再读取任何 `SMINI_LLM_*` 环境变量，也不发起任何模型 HTTP 调用**——模型
  能力完全来自运行环境的宿主；未交卷则打印 stderr 提示并走空结果 + 登记
  `Degradation(no_credential)`（零规则无确定性兜底）。
- **B. agent 编排 skill 模式**：agent 用宿主生态工具（pdf / office / 资料库 /
  browser）把源变成 `RawDocument` 经 `ctx.raw_docs` 透传，或令宿主产出与
  `LLMProvider.extract` 同结构的 dict，喂给 step 映射函数——**无需改动 step**。

---

## 6. 运行方式

### 6.1 命令行（CLI）

```bash
# 内置样例，查询「特斯拉 总部」
python -m smini.cli build --sample -q "特斯拉 总部"

# 自定义文本
python -m smini.cli build "text://特斯拉公司成立于2003年，总部位于美国加州。"

# 文件源
python -m smini.cli build "file:///path/to/doc.txt" -q "关键词"

# 只跑到某步（含），便于调试
python -m smini.cli build --sample --stop extract

# 把整条 state 导出为 JSON
python -m smini.cli build --sample -q "特斯拉" --json state.json

# 不带 -q：建图+落库，deliver 产出空包（不报错）
python -m smini.cli build --sample
```

> 注意：本实现**零第三方依赖**，任意 Python 3.9+ 环境即可直接 `python -m smini.cli`。

### 6.2 Python API

```python
from smini import build_pipeline, make_context

sources = ["特斯拉公司成立于2003年，总部位于美国加利福尼亚州。"]
ctx = make_context(sources, query="特斯拉 总部")
pipeline = build_pipeline(sources, query="特斯拉 总部")
state, results = pipeline.run(ctx=ctx)

print(state.graph.stats)            # 图谱统计
print(state.qa.metrics)             # QA 指标
print(state.receipt)                # 存储回执（幂等）
print(state.delivered.render())     # 交付文本（可直接塞 prompt）
```

注入真实存储 / 向量库（可选）：

```python
from smini.stores import MemoryGraphStore
store = MemoryGraphStore()
pipeline = build_pipeline(sources, store=store, embedder=my_embedder,
                          vector_store=my_vector_store, query="...")
```

---

## 7. 测试

```bash
# 契约自验证（60 项，验证契约本身兑现承诺）
python -m unittest tests.test_contracts -v

# 端到端流水线（10 项，内存 text:// 源跑通 8 步，确定性默认）
python -m unittest tests.test_pipeline -v

# LLM / 大模型驱动路径（7 项，用 StubLLMProvider 验证接缝）
python -m unittest tests.test_llm_path -v

# 一次跑全部（共 77 项）
python -m unittest discover -s tests -v
```

- `tests/test_pipeline.py`：8 步顺序、中间字段非空、图谱实体/边数量、字面量边、
  QA 结构与默认无冲突、Store 幂等（同 store 二次 build `written=0`）、Deliver 双向
  检索（宾语视角召回 CEO 边）、确定性实体 ID、CLI 子进程、QA 冲突检测 + 最近裁决 + 落选作废。
- `tests/test_llm_path.py`：**证明「智能挪进 LLM」链路正确且幂等**——
  Extract 走 LLM 路径且实体类型透传到关系边（不降为 OTHER）、两次构建 ID 集合一致；
  LLM 抛 `DependencyMissing` 时回退规则并登记 `Degradation(extract.llm)`；
  Parse 保真校验（文本偏差过大 `StepError`）；全链路注入桩后 8 步跑通且图谱带
  ORG/PERSON/LOC 类型、幂等。

---

## 8. 已知边界（v1 不做 / 待补）

- **扫描版 PDF 的 OCR**：裸字节 PDF/DOCX/PPTX/XLSX 在 v1 仍降级为占位
  `parser="unsupported"`，需接入宿主平台的 `pdf` / OCR skill 才能走「摄取原生」。
- **敏感文档外联**：证券/法规类敏感文件送外部 LLM 前需用户同意，或走本地模型——
  接法 B（agent 编排）时由 agent 负责合规闸门。
- Deliver 默认只做图谱内关键词检索，未接向量/混合检索（接口已留 `Embedder`/`VectorStore`）。
- 无 Web 抓取（显式拒绝，避免静默外联）；只吃本地 `text://` / `file://` / inline。
- QA 冲突仅覆盖 VALUE 类（同主语同谓语多值）；语义冲突不在 v1 范围。
- LLM 关系抽取若模型未给出主语/宾语类型，兜底为 `OTHER`（已在 `_relations_from_llm`
  用 surface 反查已声明实体类型缓解，但全新实体仍可能落到 OTHER）。
