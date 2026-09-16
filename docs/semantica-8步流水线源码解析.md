# Semantica Full Pipeline 八阶段源码解析

> 分析对象：`https://github.com/semantica-agi/semantica`（main 分支，1204 个文件）
> 所有结论均来自真实源码，证据格式为 `文件路径:行号`。
> **v2 修订**：官方存在**两套不同的"8 步"定义**，本文已按 docs 站口径（权威版）重构，并保留 README 口径的完整分析。

---

## 0. 最重要的发现：项目里有两套"8 步"

这不是笔误，是两个文件里各写各的：

| 口径 | 出处 | 8 步内容 |
|---|---|---|
| **A · docs 站（权威）** | `docs/architecture.md:87` 原文明写 *"Semantica 8-step pipeline"*；同图出现在 `docs/quickstart.md:44`、`docs/reference/pipeline.md:43` | Ingest → Parse → Normalize → Extract → **Build KG → QA → Store → Deliver** |
| **B · README / ARCHITECTURE.md** | 仓库根 `ARCHITECTURE.md:11-48` 的 mermaid 图 | Ingest → Parse → Normalize → **Split** → Extract → **Conflict Detection → Deduplication** → **KG Construction** |

**为什么以 A 为准**：A 在文档里被显式标注为 "8-step pipeline"，出现在 3 个页面，且配套的 `docs/assets/img/diagrams/pipeline-flow.svg` 给每一步都标了代表类。B 只是 README 里一张 mermaid 图，从未自称"8 步"。

**A 版每一步的代表类**（从 SVG 里提取的 `<text>` 节点）：

| # | 步骤 | 代表类 |
|---|---|---|
| 01 | Ingest | `FileIngestor` |
| 02 | Parse | `DocumentParser` |
| 03 | Normalize | `TextNormalizer` |
| 04 | Extract | `NERExtractor` |
| 05 | Build KG | `GraphBuilder` |
| 06 | QA | `ConflictDetector` |
| 07 | Store | `VectorStore` |
| 08 | Deliver | `AgentContext` |

### 两套怎么对齐

官方 prose 里其实给了答案。`docs/getting-started.md:200`：

> **[Quality Layer]** — Validate and deduplicate. Modules: `deduplication`, `conflicts`

所以 **QA = 冲突检测 + 去重**，正好是 B 版的第 6、7 步。两套的完整映射：

| A（docs，端到端） | B（README，进图之前） | 说明 |
|---|---|---|
| 1 Ingest | 1 Ingest | 相同 |
| 2 Parse | 2 Parse | 相同 |
| 3 Normalize | 3 Normalize | 相同 |
| 4 Extract | 4 Split + 5 Extract | A 把"分块"合并进了 Extract；B 把 Split 单列 |
| 5 Build KG | 8 KG Construction | 相同 |
| 6 QA | 6 Conflict Detection + 7 Deduplication | A 合并为"质量层" |
| 7 Store | — | B 版没覆盖（进图之后的存储） |
| 8 Deliver | — | B 版没覆盖（交付与消费） |

**结论：A 是 B 的超集。B 讲的是"怎么把数据变成图"，A 讲的是"从原始数据到交付给 Agent 的完整链路"。** 下文按 A 版 8 步展开，B 版独有的 Split 在 §4 里补。

### 还有第三个口径

`docs/quickstart.md:138-155` 的**实操教程只有 6 步**：Ingest → Parse → Extract → Build KG → Visualize → Export。它跳过了 Normalize（快速上手不需要）、QA（默认自动）、Store（默认内存）。三个口径不冲突，但新手容易对不上号。

---

## 全局结论（三个，先看这个）

### 结论一：这 8 步是"架构定义"，不是"一个内置函数"

源码里**没有任何内置函数或模板把这 8 步串成开箱即用的流程**：

- 顶层 `semantica/__init__.py:196` 的 `__all__ = []`，`from .core import Semantica` 是被注释掉的（`:23`）。顶层只有 `_ModuleProxy` 懒加载代理（`:48-219`），只能 `semantica.kg` 这样按子模块访问。
- 真正的主类在 `core/orchestrator.py:38` 的 `Semantica`，需显式 `from semantica.core import Semantica`。它的 `build_knowledge_base()`（`:281`）不硬编码 8 步——`pipeline_config` 为空时只加一个占位步（`:760-761`）：
  ```python
  if not pipeline_config:
      builder.add_step("default_step", "default")
      return builder.build("default_pipeline")
  ```
- `pipeline_templates.py` 的 4 个模板全是 8 步的**近似子集**，无一完整覆盖。最接近的是 `kg_construction`（6 步）。

**直接调 `build_knowledge_base()` 产出的是空图。必须自己组装。**

### 结论二：8 步之间**没有统一的数据契约**

| 阶段 | 输出类型 | 有规范类？ |
|---|---|---|
| 1 Ingest | 各自 dataclass（`FileObject` / `WebContent` / ...） | ❌ 无统一 `RawDocument` |
| 2 Parse | 纯 `dict`（各 parser 结构还不一样） | ❌ 无 `ParsedDocument` |
| 3 Normalize | 标量或 `List[Dict]` | ❌ |
| 4 Extract | `Entity` / `Relation` / `Triplet` dataclass | ✅ `semantic_extract/types.py` |
| 5 Build KG | 纯 `dict`：`{"entities":[], "relationships":[], "metadata":{}}` | ❌ |
| 6/7/8 QA/Store/Deliver | 各自为政 | ❌ |

`kg/schemas.py` **不存在**（`kg/schemas/` 目录下只有一个 `temporal_snapshot_v1.json`）。跨阶段靠字段隐式耦合（parse 读 `FileObject.path`，见 `document_parser.py:126`）。

### 结论三：全链路确定性可用，"不用 LLM" 是真的

每个抽取器都是 `规则 → ML(spaCy/HF) → LLM → fallback` 四级降级，provider 不可用时**静默返回原数据**。建图、QA、Store、Deliver 全是确定性算法。

---

## 1 · Ingest（`semantica/ingest`）

### 入口与路由

**没有统一的 `Ingestor` 门面类。** 统一入口是函数 `ingest()`（`ingest/methods.py:1491`）：

1. **自动判别 source_type**（`:1544-1601`）：`http(s)://` → web/feed；db 连接串 → db；含 `github.com`/`gitlab.com` → repo；`.ttl/.owl/.rdf` → ontology；`.parquet` → parquet；`.xml` → xml；否则 → file
2. **if/elif 硬路由**到 20+ 个 ingestor（`:1604-1644`）

覆盖 `FileIngestor`、`WebIngestor`、`FeedIngestor`、`StreamIngestor`（Kafka/RabbitMQ/Kinesis/Pulsar）、`RepoIngestor`、`EmailIngestor`、`DBIngestor`（PG/MySQL/SQLite/Oracle/MSSQL）、`XMLIngestor`、`ParquetIngestor`、`ArrowIngestor`、`SnowflakeIngestor`、`DatabricksIngestor`、`SAPIngestor`、`SalesforceIngestor`、`MCPIngestor` 等。全部通过 `ingest/__init__.py:166-298` 的懒加载 `_LAZY_EXPORTS` + `__getattr__` 按需 import。

### 核心算法

**文件类型检测三级优先**（`FileTypeDetector.detect_type`，`file_ingestor.py:118`）：

1. **扩展名**（`:140-143`）— 命中即返回
2. **MIME**（`mimetypes.guess_type`，`:146-156`）— 仅当扩展名为空
3. **magic number**（`:170-208`）— **仅当传入 `content` 字节时才执行**

> ⚠️ 扩展名优先级最高 → 改名文件会被误判；magic 检测默认不跑。

**SSRF 防护**（`ingest/ssrf.py`，753 行，这块最扎实）：
- `_ip_is_blocked`（`:152`）：屏蔽 private/loopback/link_local/reserved/multicast + `BLOCKED_NETWORKS`
- `validate_url_for_request`（`:205`）：只允许 http/https；域名做 DNS 解析后校验，**带超时线程池且 fail-closed**（`:187-192`）
- `request_with_ssrf_guard`（`:478`）：**禁用 requests 自动重定向**，逐跳重校验 `Location`；跨域重定向时剥离 `Authorization`/`auth`/`trust_env`（`:508-543`）

**web 其它机制**（均在 `web_ingestor.py`）：`RobotsChecker`（`:106`，缺失时默认允许）、`RateLimiter`（`:68`）、`SitemapCrawler`（`:321`，支持 index 递归 `:411`）、`crawl_domain`（`:764`）。

### 数据结构与溯源

- `FileObject`（`file_ingestor.py:44-78`）：`path, name, size, file_type, mime_type, content: Optional[bytes], metadata, ingested_at`。`content` 是 **bytes**，`.text` 按 `utf-8 → latin-1 → ""` 解码
- `WebContent`（`web_ingestor.py:55-65`）：`url, title, text, html, metadata, links, fetched_at, status_code`
- **溯源**：checksum 用 SHA-256（`provenance/integrity.py:27`）并链入 `previous_checksum`（防篡改链），**故意排除 `entity_id`** 避免重命名产生假阳性断链（`:45-55`）

---

## 2 · Parse（`semantica/parse`）

### 入口与路由

**没有单一 `DocumentParser` 统管所有格式**，按数据族并列：`DocumentParser`（`document_parser.py:140`）、`WebParser`、`StructuredDataParser`（`:43`）、`EmailParser`、`CodeParser`（`:68`）、`MediaParser`，以及格式专属的 `PDFParser`/`DOCXParser`/`PPTXParser`/`HTMLParser`/`JSONParser`/`CSVParser`/`XMLParser`/`ImageParser`/`ExcelParser`/`DoclingParser`。

文档路由：`_detect_file_type` 按扩展名映射（`:357-360`）→ 分发到对应 parser（`:140`）。

### 底层库

| 格式 | 库 | 说明 |
|---|---|---|
| PDF | `pdfplumber` | 懒导入（`:120`），逐页抽 tables/metadata/images |
| DOCX | `python-docx` | paragraphs + core_properties |
| PPTX | `python-pptx` | 懒导入 |
| HTML/Web | `BeautifulSoup` | `soup.get_text` |
| XML | `lxml` / `ElementTree` | 命名空间 + XPath |
| Excel | `openpyxl` + `pandas` | |
| 图片 OCR | `PIL` + `pytesseract` | 懒导入（`image_parser.py:44`） |
| 全格式兜底 | `docling` | 可选 extra `parse-docling` |

**CodeParser 双路径**（`code_parser.py`）：Python 走 AST（`ast.parse` `:270` → `ast.walk` 抓 FunctionDef/ClassDef `:295-310` → 抓 Import 做依赖分析 `:541`）；其他语言走正则（`:335`）。

> ⚠️ **依赖声明缺口**：`pyproject.toml:74-77` 只声明了 `beautifulsoup4`、`lxml`、`python-docx`、`openpyxl`。`pdfplumber`、`python-pptx`、`pytesseract`、`Pillow` **均未写入依赖**，仅函数内懒导入。干净安装下 PDF/PPTX/OCR 会运行时 `ImportError`。

### 数据结构

各 `parse()` 返回**纯 dict，无统一类**。如 `PDFParser.parse` 返回 `{metadata, pages: [...], full_text, ...}`（`:175-178`）。quickstart 里用的是 `parsed["text"]`。

---

## 3 · Normalize（`semantica/normalize`）

### 入口

无 `Normalizer` 门面。协调类直接是 `TextNormalizer` / `EntityNormalizer` / `DateNormalizer` / `NumberNormalizer` / `DataCleaner`，外加 `UnicodeNormalizer`、`AliasResolver`、`NameVariantHandler`、`TimeZoneNormalizer`、`RelativeDateProcessor`、`UnitConverter`、`CurrencyNormalizer`、`LanguageDetector`、`EncodingHandler` 等（`normalize/__init__.py:122-177`）。

### 核心算法

**TextNormalizer**（`normalize_text`，`text_normalizer.py:120`）——顺序固定四步：

1. `normalize_unicode(form="NFC")` 默认（`:167/289`）
2. `normalize_whitespace`（`:172`）
3. `process_special_chars(normalize_diacritics=False)` 默认关（`:177`）
4. case：默认 `"preserve"`，可选 lower/upper/title（`:182-187`），末 `.strip()`

**EntityNormalizer**（`entity_normalizer.py:89`）：strip → 压缩空白 → `entity_type == "Person"` 则 `.title()`（`:123`）→ `alias_resolver.resolve_aliases`（默认开，`:126`）→ `normalize_name_format("standard")`（`:134`，处理 Dr./Mr. 等尊称）

> ⚠️ **消歧器是桩**：`EntityDisambiguator.calculate_confidence` 对所有候选**硬编码返回 0.8**（`:439`）。

**DateNormalizer**（`date_normalizer.py:102`）：`dateutil.parser.parse` → 失败回退 `fromisoformat`（`:138`）→ 再失败走 `RelativeDateProcessor`（"yesterday"/"3 days ago"，`:141`）。时区**默认转 UTC**（`:153`）。无显式格式表，全靠 dateutil 启发式。

**NumberNormalizer**（`number_normalizer.py:91`）：去逗号空格 → 去货币符号 → `%` 转 `/100` → 科学计数 → **后缀 K/M/B/T 放大**（`:140-151`，k=1e3 … t=1e12）。`UnitConverter` 用 `unit_factors` 字典（`:291-330`）；`CurrencyNormalizer` 默认 `default_currency="USD"`（`:229/657`）。

**DataCleaner**（`data_cleaner.py:130`）：`handle_missing`（默认 `"remove"`）→ `validate` → `remove_duplicates`（保留首个，`:180-194`）。去重用**字符级 Jaccard**（`:479-482`），阈值默认 **0.8**（`:318`）——"apple"/"apples" 相似度高，易误删。

---

## 4 · Extract（`semantica/semantic_extract`）+ B 版独有的 Split（`semantica/split`）

docs 的 A 版把 Split 并进了 Extract。两者是上下游关系：Split 把长文本切成 chunk，Extract 从 chunk 里抽实体关系。

### 4a · Split（`semantica/split`）

统一入口 `TextSplitter`（`split/splitter.py`），默认 `method="recursive", chunk_size=1000, chunk_overlap=200`（`:63-69`）。`split()`（`:115`）按 `self.methods` 顺序调用，**任一方法返回非空即返回**——fallback chain。

共 **22 个方法**（`methods.py:1647-1673`）：标准 10 个（recursive/token/sentence/paragraph/character/word/semantic_transformer/llm/huggingface/nltk）+ KG 向 10 个（entity_aware/relation_aware/graph_based/ontology_aware/embedding_semantic/hierarchical/community_detection/centrality_based/subgraph/topic_based）+ 专用 2 个（structural/sliding_window）。

**主打的 `entity_aware`（`methods.py:852-914`）**：先跑 NER 收集实体的起止字符偏移，再逐句累加，**只在"超 size 且当前句不含实体边界"处落 chunk**：

```python
ner_extractor = NERExtractor(method=ner_method, **kwargs)
entities = ner_extractor.extract(text)
entity_boundaries = set()
for entity in entities:
    entity_boundaries.add(entity.start_char)
    entity_boundaries.add(entity.end_char)

for sentence in sentences:
    has_entity_boundary = any(
        sentence_start <= boundary <= sentence_end
        for boundary in entity_boundaries
    )
    if current_size + sentence_size > chunk_size and current_chunk:
        if preserve_entities and has_entity_boundary:
            pass                       # 不在此切断，避免切碎实体
        else:
            chunks.append(Chunk(...))
```

其它关键策略：`semantic_transformer`（`:588`，all-MiniLM-L6-v2 句向量，相邻句相似度 < **0.7** 则切）、`graph_based`（`:1150`，networkx + louvain/centrality，取实体 ±100 字符）、`hierarchical`（`:1403`，`levels=["section","paragraph","sentence"]` + `chunk_sizes=[2000,1000,500]`）。

```python
@dataclass
class Chunk:                            # split/semantic_chunker.py:43-50
    text: str
    start_index: int
    end_index: int
    metadata: Dict[str, Any] = field(default_factory=dict)
    id: Optional[str] = None
```

> ⚠️ 三点：
> 1. `ontology_aware`（`:1345`）**名实不符**——直接复用 `split_entity_aware`，没真正接入本体推理。
> 2. `entity_aware`/`graph_based` 依赖 `semantic_extract` 模块，不可用时静默回退 `split_recursive`（`:872`）。
> 3. **除 `semantic_transformer`/`graph_based` 外，overlap 是纯字符/句级滑动，不是语义重叠。**

### 4b · Extract（`semantica/semantic_extract`）

任务分发在 `methods.py`：`get_entity_method`（`:2623`）/ `get_relation_method`（`:2650`）/ `get_triplet_method`（`:2679`）。

| 任务 | 可用方法 |
|---|---|
| entity | `pattern, regex, rules, ml/spacy, huggingface, llm` |
| relation | `pattern, regex, cooccurrence, similarity, dependency, huggingface, llm` |
| triplet | `pattern, rules, huggingface, llm` |

**NER — `NERExtractor`**（`ner_extractor.py:87`，`min_confidence=0.5`）
`extract_entities`（`:313`）：遍历 methods → 按 min_confidence 过滤 → 返回首个成功 → **全失败走 `_extract_fallback`（`:540`，pattern + 大写词启发式，conf 0.5–0.7）**。
后端置信度：pattern 0.7（`:656`）/ regex 0.75（`:683`）/ rules 0.6（`:715`）/ ml=spaCy（`:755`）/ huggingface 默认 `dslim/bert-base-NER`（`:816`）。

**Relation — `RelationExtractor`**（`relation_extractor.py:83`）
默认 `method="pattern"`, `confidence_threshold=0.6`, `max_distance=50`。方法：pattern 0.7 / cooccurrence（距离<100字符，`related_to`，0.6）/ dependency（spaCy 依存，0.8）。
兜底链：`_extract_with_patterns` → `_extract_last_resort_relations`（相邻实体连 `related_to`，conf **0.3**）。

**Event — `EventDetector`**（`event_detector.py:88`，默认 `method="llm"`）
`event_patterns` 默认含 acquisition/partnership/launch/investment/legal（`:116`）。`detect_events`（`:277`）正则匹配 + ±50 字符 context。

**Triplet — `TripletExtractor`**（`triplet_extractor.py:87`，`min_confidence=0.5`）
pattern 0.7 / rules / huggingface（REBEL 模型，**0.9**）/ llm。`TripletValidator.validate_triplet`（`:699`）校验三要素非空 + confidence ≥ min。

**Coreference — `CoreferenceResolver`**（`coreference_resolver.py:95`）
`resolve_coreferences`（`:125`）四步：正则提代词 → NER 实体作 mention → `PronounResolver`（`:462`，找最近前置兼容类型实体）→ `CoreferenceChainBuilder`（`:591`）建链。

**LLM 路径**：`LLMExtraction`（`llm_extraction.py:87`，默认 `provider="openai"`）+ `providers.py`。`BaseProvider.generate_typed`（`:232`）优先 instructor，`Mode.TOOLS` 失败回退 `Mode.JSON` 再 repair loop，`max_retries=3`。Provider：`OpenAI`（`:563`）/ `Gemini`（`:664`）/ `Groq`（`:794`）/ `Anthropic`（`:890`）/ `Ollama`（`:978`）。**provider 不可用时静默返回原数据**（这是"无 LLM 可用"的关键实现）。

### 数据结构（`semantic_extract/types.py`）

```python
@dataclass Entity:   text, label, start_char, end_char, confidence=1.0, metadata
@dataclass Relation: subject: Entity, predicate: str, object: Entity, confidence=1.0, context, metadata
@dataclass Triplet:  subject: str,  predicate: str, object: str,  confidence=1.0, metadata
@dataclass Event:    text, event_type, start_char, end_char, participants, location, time, confidence, metadata
```

> ⚠️ `Triplet.subject/object` 是**字符串**，而 `Relation` 持有的是 **`Entity` 对象**——下游组图需要类型转换。另外 Pydantic schema 里 `EntityOut.confidence` 默认 **0.9**，而 dataclass 默认 **1.0**。

---

## 5 · Build KG（`semantica/kg`）

### `GraphBuilder.build()` 主流程（`graph_builder.py:422`）

1. **源归一化**（`:422-535`）
2. **实体收集**（fast path `:595-644`）；**ID 生成在 `:608`**：
   ```python
   if "id" not in entity_dict and "entity_id" not in entity_dict:
       entity_dict["id"] = (entity_dict.get("name")
                            or entity_dict.get("text")
                            or str(hash(str(item))))
   ```
3. **关系收集**（`:648-722`）：字段映射 `source_id→source`、`subject→source`、`target_id→target`、`object→target`
4. **实体解析/去重**（`:748-773`）：仅当 `merge_entities=True` 时 `self.entity_resolver` 才存在（`:118-129`）
5. **关系端点重映射**（`:777-783`）：仅在出现 `merged_from` 实体时调用
6. **组装 graph dict**（`:792-805`）：`{"entities":..., "relationships":..., "metadata":{...}}`
7. **可选持久化**（`:809-842`）：调 `graph_store.add_nodes()` / `add_edges()`
8. **可选冲突检测**（`:844-867`）

### 关键默认值（`graph_builder.py:57-67`）

```python
merge_entities=False,                    # ⚠️ 默认不去重
entity_resolution_strategy="fuzzy",
resolve_conflicts=True,
enable_temporal=False,
temporal_granularity="day",
track_history=False,
version_snapshots=False,
graph_store=None,
```

### 双时态模型（`temporal_model.py`）

`BiTemporalFact`（`:27-66`）两维度：**valid time**（`valid_from`/`valid_until`）+ **transaction time**（`recorded_at`/`superseded_at`）。`TemporalBound.OPEN`（`:17-20`）表开区间。
point-in-time 查询：`temporal_query.py` 的 `query_at_time()`（`:107`）→ `reconstruct_at_time()`（`:170`）→ `_relationship_active_at_time()`（`:795`）。`time_axis` 支持 `valid`/`transaction`/`both`。
注意 `build()` 不自动加时态边，需显式调 `add_temporal_edge()`（`:930`）/ `create_temporal_snapshot()`（`:1013`）。

### 图分析

| 能力 | 文件 | 算法 |
|---|---|---|
| 中心性 | `centrality_calculator.py` | degree / betweenness / closeness / eigenvector（幂迭代 `max_iter=100, tol=1e-6`）/ **PageRank**（`damping=0.85`，`:582-672`） |
| 社区发现 | `community_detector.py` | louvain / leiden（**实际委托 louvain**，`:247-275` 注释说完整 Leiden 需 igraph）/ overlapping(k-clique) / label_propagation |
| 链接预测 | `link_predictor.py` | common_neighbors / jaccard / adamic_adar / resource_allocation / preferential_attachment |
| 路径 | `path_finder.py` | dijkstra(默认) / astar / bfs / **Yen's K 最短路**（`:500-557`） |

---

## 6 · QA（示意图标 `ConflictDetector`）

### 官方定义

`docs/getting-started.md:200` 明确：**Quality Layer = `deduplication` + `conflicts`**。示意图把这一步标为 `ConflictDetector`。

但源码里，**QA 不是一个独立的第 6 步，而是被拆散编织进了多处**。这是 A 版 8 步里与代码落差最大的一步。

### 四个组件的真实触发方式

| 组件 | 位置 | 触发方式 | 效果 |
|---|---|---|---|
| `ConflictDetector` | `conflicts/conflict_detector.py` | **自动**（`resolve_conflicts=True` 默认，`graph_builder.py:61`） | 在 `build()` 末尾跑（`:844-867`），**结果只 logger 输出** |
| `EntityResolver` | `kg/entity_resolver.py` | **自动但默认关闭**（`merge_entities=False`） | quickstart 的 `GraphBuilder(merge_entities=True)` 走的就是它 |
| `DuplicateDetector` | `deduplication/duplicate_detector.py` | **显式**（独立管道，可接成 pipeline step） | 完整去重能力，默认不参与建图 |
| `ExtractionValidator` | `semantic_extract/extraction_validator.py` | **自动**（Extract 步内，`triplet_extractor.py:514/561/601`） | 置信度过滤 + precision/recall/F1 质量分 |
| `GraphValidator` / SHACL / `evals` | `kg/graph_validator.py`、`ontology/`、`evals/` | **显式** | 独立门禁，不在 8 步线性流中 |

### ⚠️ 最重要的发现：顺序是反的

docs 画的是 `QA(06) → Store(07)`，但 `build()` 里实测：

```
:809  # Persist to GraphStore if available
:818  node_count = self.graph_store.add_nodes(resolved_entities)
:835  edge_count = self.graph_store.add_edges(formatted_edges)
:844  # Detect and resolve conflicts if conflict detector is available
:848  detected_conflicts = self.conflict_detector.detect_conflicts(graph["entities"])
:857  resolution_result = self.conflict_detector.resolve_conflicts(detected_conflicts)
```

**先落库（818/835），后 QA（844）。** 而且 `resolve_conflicts` 是**就地修改 entities**——意味着裁决后的干净版本留在内存里，但**已经写进库的是裁决前的版本**，冲突清单本身也不落盘、不回写。生产环境如果接了持久化 graph_store，库里存的是未消歧的数据。

### 冲突检测算法（B 版第 6 步的完整内容）

**5 类冲突**（`conflict_detector.py:68-75`）：`VALUE / TYPE / RELATIONSHIP / TEMPORAL / LOGICAL`。

**判定核心**（`:271`）——**精确字符串集合判等，无数值容差、无模糊匹配、无 embedding**：

```python
unique_values = list(set(str(v) for v in values if v is not None))
if len(unique_values) > 1:
    conflict = Conflict(...)
```

- **Severity**（`:575`）：关键字段 `[id, name, type, founded_year, revenue]` → `critical`；数值差 `> 1000` → `high`；否则 `medium`
- **Confidence**（`:558`）：`min(1.0, avg_confidence * (1 + value_diversity))`（`:573`）
- **LOGICAL**：`incompatible_types` 字典（`:1048`）定义 Person/Organization/Company/Location 的互斥组合（`:1149`）
- **不是双时态**：`detect_temporal_conflicts`（`:818`）只用正则抽 4 位年份（`:954`）做字符串比较，无 valid-time 区间重叠判定

**解决策略**（`conflict_resolver.py:70-79`，默认 `voting`）：

| 策略 | 公式 | confidence |
|---|---|---|
| voting（默认） | `Counter` 多数票（`:355`） | `count / total_votes` |
| credibility_weighted | `weight = source_confidence × credibility`，`argmax Σweight`（`:385`） | `winning_weight / total_weight`（`:417-421`） |
| most_recent | 取 timestamp 最大 | 固定 0.8 |
| first_seen | 取首个 | 0.7 |

**来源可信度**（`source_tracker.py:98`）：默认 **0.5**，手动设定或注册时给定，**不是按历史准确率自适应学习**（`:379/397`）。

### 去重算法（B 版第 7 步的完整内容）

`DuplicateDetector.detect_duplicates`（`duplicate_detector.py:186`）四段式：

**① Blocking**（`similarity_calculator.py:651`）：`legacy` 默认用名字首字母 `name[0]`（`:666`）；`blocking_v2` 用 token 前 4 字符 + 可选 soundex（`:685-687`）。

**② Scoring**（`calculate_similarity`，`:220`）——权重自动归一化，embedding 仅当 `score > 0` 才计入（`:333-342`）：

```python
embedding_weight: float = 0.0,     # ⚠️ 默认关闭
string_weight: float = 0.6,
property_weight: float = 0.2,
relationship_weight: float = 0.2,
similarity_threshold: float = 0.7,
```

string 用 Jaro-Winkler（`:549`，`jaro + min(prefix_len,4)*0.1*(1-jaro)`）；property 逐键比较，缺失记 0.5 中性分（`:395`）；relationship 用 Jaccard（`:444`）；embedding 用 cosine 后 `(cos+1)/2`（`:487`）。

**③ Classification**：≥ similarity_threshold 的对再过 `confidence_threshold`（默认 **0.6**）。candidate confidence（`:746`）= `similarity + 0.1*exact_name_match + 0.05*prop_matches + 0.05*same_type`，封顶 1.0；type mismatch 直接 0.0。

**④ Clustering**（`:833`）：**并查集求连通分量**。`ClusterBuilder`（`cluster_builder.py`）另提供层次聚类（`:360`，O(n²)，默认关闭）。

**合并策略**（`merge_strategy.py:54-62`，默认 `KEEP_MOST_COMPLETE`）：canonical id 取 `max(len(properties)+len(relationships))`（`entity_merger.py:383`）；属性冲突默认 keep_first（`:456`）；溯源保留在 `metadata.provenance.merged_from`（`:475`）。

> ⚠️ `kg/entity_resolver.py` 与 `deduplication` **共用同一引擎**（`:26` 直接 `from ..deduplication.entity_merger import EntityMerger`），前者只是 KG 视角的适配层，没有两套算法。

---

## 7 · Store（示意图标 `VectorStore`）

### ⚠️ 先纠正示意图：构建链路上真正承担 Store 的是 `GraphStore`

示意图第 07 步标 `VectorStore`，但 `GraphBuilder` **只有 `graph_store` 参数**（`graph_builder.py:66`），**没有 `vector_store` 参数**。整个 `semantica/kg/` 对 `VectorStore` **零引用**。

即：Build KG → Store 的自动化写入**只通向 GraphStore**。VectorStore 和 TripletStore 是需要手工驱动的独立存储栈。

### 三个平行的 Store（无共同抽象）

| | VectorStore | GraphStore | TripletStore |
|---|---|---|---|
| 入口 | `vector_store.py:100` | `graph_store.py:525` | `triplet_store.py:40` |
| 数据模型 | 稠密向量 + metadata | LPG（label + properties） | RDF 三元组 + named graph |
| 查询 | ANN / 混合检索 | Cypher / openCypher / Gremlin | SPARQL |
| 默认后端 | `faiss`，`dimension=768` | `neo4j` | `blazegraph` |
| 自动写入者 | 无 | `GraphBuilder.build()` | 无 |

**关键事实：三个 store 里没有任何 ABC / abstractmethod / Protocol。** 可替换性靠的是 **if/elif 工厂 + `hasattr()` 鸭子探测**，不是接口契约。例如 `VectorStore.store_vectors`（`vector_store.py:507-529`）：

```python
if hasattr(self._backend_store, 'add'):
    return self._backend_store.add(vectors, metadata, **options)
elif hasattr(self._backend_store, 'add_vectors'):
    ...
    add_vectors_params = inspect.signature(self._backend_store.add_vectors).parameters
    supports_metadata = 'metadata' in add_vectors_params or ...
else:
    raise NotImplementedError(f"Backend store ... does not have add or add_vectors method")
```

`search_vectors`(`:709`)、`update_vectors`(`:768`) 等 6 个方法是同一套模式。文档宣称的 "swap backend with no API changes"（`docs/architecture.md:184`）是鸭子类型撑起来的，不是继承。

### 后端清单

**VectorStore（8 个）**：`faiss`(默认，核心依赖 `pyproject.toml:81`)、`qdrant`、`weaviate`、`milvus`、`pinecone`、`pgvector`、`sqlite`、`inmemory`。后 7 个需各自 `vectorstore-*` extra。

**GraphStore（4 个，仅 LPG）**：`neo4j`(默认)、`falkordb`、`neptune`、`age`。全部需 `graph-*` extra。⚠️ **RDF 系（Oxigraph/Blazegraph/Jena/RDF4J）不在 GraphStore，全在 TripletStore**。

**TripletStore（5 个，仅 RDF）**：`blazegraph`(默认)、`jena`、`rdf4j`、`anzo`、`oxigraph`。前四个是 HTTP SPARQL 客户端，靠核心依赖 `requests`+`rdflib`，开箱可 import。

### 混合检索与 RRF（`hybrid_search.py:148-167`）

```python
def reciprocal_rank_fusion(self, results, k: int = 60):
    scores: Dict[str, float] = {}
    for result_list in results:
        for rank, result in enumerate(result_list, start=1):
            result_id = result.get("id", str(id(result)))
            score = 1.0 / (k + rank)
            scores[result_id] = scores.get(result_id, 0.0) + score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
```

即 `score(d) = Σ_lists 1/(60 + rank_list(d))`。默认策略 `reciprocal_rank_fusion`（`:271-273`）。

### embedding 在哪生成

不在 Build KG。`VectorStore` 是**双模**的：
- **主动模式**：`add_documents(documents)`（`vector_store.py:334-442`）接收纯文本，内部 `ThreadPoolExecutor(max_workers=6)` 并行分批（`batch_size=32`）调 `embed_batch()`，最后 `store_vectors()`
- **被动模式**：`store_vectors()` 直接收 `List[np.ndarray]`

链路：`VectorStore.embed_batch` → `EmbeddingGenerator` → `TextEmbedder`，默认模型 `BAAI/bge-small-en-v1.5`（`text_embedder.py:75`）。

### ⚠️ Store 步的坑（都很致命）

1. **维度默认不匹配**：`VectorStore()` 的 `dimension=768`，但默认 embedder `BAAI/bge-small-en-v1.5` 是 **384 维**（`text_embedder.py:145` 的 FastEmbed 探测失败回落值也是 384）。`VectorStore().add_documents([...])` 会在 FAISS index add 处维度冲突。**必须显式 `dimension=384` 或换模型。**
2. **embedding 静默随机化**（`vector_store.py:303-307`）：加载失败时**返回随机向量而非抛错**——
   ```python
   self.logger.warning("Using random fallback embedding")
   return np.random.rand(self.dimension).astype(np.float32)
   ```
   生产环境若 fastembed 加载失败，得到的是可检索但完全无意义的索引，只有一条 WARNING。
3. **无 upsert，重复 build 会重复插入**：Neo4j 用 `CREATE` 而非 `MERGE`（`neo4j_store.py:371` 等 5 处，全文件 `MERGE` 零命中）。FAISS 虽有 id 去重（`faiss_store.py:462-466`），但 `VectorStore.store_vectors` **从不传 `ids`**（`:526`），第二次写入的 id 是 `vec_N...`，与首次 `vec_0...` 不碰撞 → 去重永不触发。
4. **`add_edges` 静默丢边**（`graph_store.py:938-941`）：异常只 warning 后跳过，返回计数变小是唯一信号。
5. **`compute_delta` 的 SPARQL 有语法错误**（`triplet_store.py:680`）：`GRAPH <{new_graph_uri} >` 在闭合尖括号前多一个空格，IRIREF 不允许空格 → "新增"方向的差分在任何合规端点上都会解析失败。
6. **默认 graph backend 需未安装的 extra**：`GraphStore()` 默认 neo4j，但 `neo4j>=5.0.0` 只在 `graph-neo4j` extra 里，裸装后直接 ImportError。
7. **`metric` 实际是 L2 不是 cosine**：config 声明 `"metric": "cosine"`（`vector_store/config.py:154`），但 `FAISSStore.create_index` 签名默认 `"L2"`（`faiss_store.py:433`），且 config 不参与建索引。

---

## 8 · Deliver（示意图标 `AgentContext`）

### 为什么是 AgentContext

`docs/architecture.md:69-76` 把 Layer 4 Application / Delivery 定义为 `context`、`reasoning`、`export`、`visualization`、`explorer`、`pipeline`。其中 GraphRAG 和 Decision tracking 都映射到 `context.AgentContext`。

**ContextGraph vs AgentContext**（均在 `semantica/context/`）：
- `ContextGraph`（`context_graph.py:514`）：内存版图引擎 + 分析内核，持有 `nodes: Dict`/`edges: List`，集成 KG 算法并记录决策——**是存储与计算引擎**（对应 "Agent memory"）
- `AgentContext`（`agent_context.py:90`）：**编排门面**，包裹 `vector_store`（必填）+ `knowledge_graph`（可选）+ `AgentMemory` + `ContextRetriever` + 决策组件，自动判别 RAG vs GraphRAG

`AgentContext` 构造默认值（`agent_context.py:124-184`）：
```python
vector_store, knowledge_graph=None, retention_days=30, max_memories=10000,
graph_expansion=True, max_expansion_hops=2, hybrid_alpha=0.5,
decision_tracking=False,      # ⚠️ 默认关闭
advanced_analytics=True, kg_algorithms=True, vector_store_features=True
```

### 决策在图里怎么建模（`_add_decision_to_graph`，`context_graph.py:4710-4816`）

决策是 `node_type="decision"` 的节点，自动挂接三类边：
- `decision →(involves)→ entity`
- `decision →(belongs_to)→ category_*`
- `decision →(made_by)→ maker_*`

**因果边定义**（`context_graph.py:494-511`）：
```python
_CAUSAL_EDGE_TYPES = ("CAUSED", "INFLUENCED", "PRECEDENT_FOR")
_CAUSAL_EDGE_ALIASES = {"CAUSES": "CAUSED", "INFLUENCES": "INFLUENCED",
                        "PRECEDES": "PRECEDENT_FOR"}
_CAUSAL_TRAVERSAL_TYPES = frozenset(_CAUSAL_EDGE_ALIASES) | {"LEADS_TO", "SUPPORTS"}
```

**决策链遍历**（`get_causal_chain`，`:3725-3807`）：对因果边做 **BFS**，按 `downstream`/`upstream` 沿 `_CAUSAL_TRAVERSAL_TYPES` 扩展，带 `max_depth` 限制，给命中决策打 `causal_distance`。

**`trace_decision_causality`**（`:4503-4652`）更完整：先沿显式因果边递归（含环检测、`max_chains` 截断），再补**启发式潜在因果**——共享 `entities` 且 `timestamp` 更早：
```python
for entity in current_decision["entities"]:
    for other in self._entity_index.get(entity, set()):
        if other != cur and other not in explicit_cause_ids:
            if other_dec["timestamp"] < current["timestamp"]:
                potential_causes.append(other)
```

> ⚠️ 这个"潜在因果"是启发式（共现实体 + 时间序），不是推理。

**相似度检索**：`find_precedents_by_scenario`（`:4277-4353`）= `0.7 * content_sim + 0.3 * structural_sim`（结构相似度需 `advanced_analytics`）。

### 其余 Deliver 组件

**export**（`semantica/export`，`__init__.py:173-229`）：RDF Turtle/RDF-XML/JSON-LD/N-Triples/N3（`RDFExporter`）、JSON、CSV、Neo4j 批量 CSV、Parquet、GraphML/GEXF/DOT、YAML、OWL、Vector（JSON/NumPy/FAISS）、Cypher（`LPGExporter`）、AQL（`ArangoAQLExporter`）、HTML 报告（`ReportGenerator`）、Arrow。

**visualization**：`KGVisualizer`（`kg_visualizer.py:79`），layout 支持 `force`(默认)/`hierarchical`/`circular`，**渲染库是 Plotly**（`plotly.express/graph_objects`，`:46-52`），HTML 经 `fig.write_html()` 写出。`plotly` 是核心依赖（`pyproject.toml:67`），默认可用。

**reasoning**：前向链接 `Reasoner.forward_chain()`（`reasoner.py:582`）、`ReteEngine`（含 AlphaNode/BetaNode/TerminalNode）、`SPARQLReasoner`、`DatalogReasoner`，另有 `GraphReasoner`/`AbductiveReasoner`/`TemporalReasoningEngine`/`ExplanationGenerator`。`rdflib` 是核心依赖，默认可用。

**explorer**：Web UI 仪表盘，`fastapi`+`uvicorn` 起服务并自动开浏览器（`explorer/__init__.py:24-117`），同时是 Ontology Hub / SHACL Studio。需 `explorer` extra。

> 关于 `pipeline` 被归入 Delivery：`pipeline` 本质是**编排/控制面**（ExecutionEngine/PipelineBuilder/...），驱动 1–8 步运行，并非 KG 的消费方。归到 Delivery 是松散归类，更准确应属 cross-cutting（与 `core` 同级）。它出现在这一层大概只因 `run_pipeline()` 是端到端"交付最终产出"的顶层入口。

### Provenance 落地（`semantica/provenance`）

- `ProvenanceManager.track_entity()`（`manager.py:268`）：SQLite 事务（`BEGIN IMMEDIATE`，`:310`）写入 `ProvenanceEntry`，含历史归档、derived_from 链路、哈希链
- `get_all_sources()`（`:990`）：`storage.trace_lineage(entity_id)` → `{source_document, location, timestamp, confidence, metadata}`
- **W3C PROV-O RDF 映射**（`export_prov`，`:1238-1298`）：用 `rdflib`，entry → `PROV.Entity` + `generatedAtTime`，agent → `PROV.Agent` + `wasAttributedTo` + qualified `Association`(hadRole)，以及 `wasDerivedFrom`/`used`
- 决策审计导出：PROV-O 走 `ProvenanceManager.export_prov(format=...)`；**没有独立的"决策→CSV"专用导出器**，CSV/JSON 审计依赖图 JSON dump 或 explorer 的 export_import 路由

### ⚠️ Deliver 步的坑

1. `decision_tracking=False` 默认，**未开启时 `record_decision`/`find_precedents` 直接抛 RuntimeError**（`agent_context.py:1680-1681`）
2. Parquet/Arrow 导出在无 `pyarrow` 时被 **Mock 类替换**（`export/__init__.py:177-222`），调用静默返回 `"Mock ..."` 字符串
3. 双后端分叉：`graph_store`（需 `knowledge_graph.execute_query`）vs `context_graph`；若既无 `execute_query` 也无 `record_decision`，决策方法抛 RuntimeError（`:1714-1715`）
4. `vector_store/__init__.py:159,255` 把 `filter` 导出到包命名空间，`from semantica.vector_store import *` 会**遮蔽 Python 内置 `filter`**

---

## 编排层：8 步怎么跑起来

`semantica/pipeline/` 六组件：`PipelineBuilder`（`pipeline_builder.py:83`，DSL 组装）、`ExecutionEngine`（拓扑+并发+重试）、`ParallelismManager`、`FailureHandler`、`ResourceScheduler`、`PipelineValidator`。

**拓扑排序用 Kahn 算法**（`execution_engine.py:697-740`）：

```python
in_degree = {step.name: len(step.dependencies) for step in steps}
queue = [step for step in steps if in_degree[step.name] == 0]
while queue:
    step = queue.pop(0); sorted_steps.append(step)
    for other_step in steps:
        if step.name in other_step.dependencies:
            in_degree[other_step.name] -= 1
            if in_degree[other_step.name] == 0:
                queue.append(other_step)
if len(sorted_steps) != len(steps):
    raise ValidationError("Circular dependency detected in pipeline")
```

**调度**：`_execute_steps()`（`:254`）按依赖层分组。只有 `parallelism > 1` **且**该层 step 的 `parallel_safe=True` **且**输入为 dict 时才并发，否则顺序。

**数据传递**：单个 `current_data` dict 在步骤间流动，`step.handler(data, **step.config)`（`:692`）。

**重试**：默认 `RetryPolicy(max_retries=3, backoff_factor=2.0, initial_delay=1.0, max_delay=60.0, strategy=EXPONENTIAL)`（`failure_handler.py:68-75`）。并行层返回非 dict 抛 `_ParallelResultContractError`，**这类契约错误不重试**（`:398`）。

---

## 横向结论

### 降级链是最值得学的部分

每个抽取器都是 `规则 → ML → LLM → fallback` 四级链，provider 不可用静默返回原数据。这让整条流水线离线也能跑通。QA/Store/Deliver 全是确定性算法。这是它宣称 "deterministic infrastructure" 的实质。

### 文档与代码的落差清单（按严重程度）

| 严重度 | 落差 | 证据 |
|---|---|---|
| 🔴 | **先落库后 QA**，冲突裁决结果不回写 | `graph_builder.py:818/835` vs `:844` |
| 🔴 | 实体 ID 用 `hash()`，**跨进程不幂等** | `graph_builder.py:608` |
| 🔴 | embedding 失败**返回随机向量**而非抛错 | `vector_store.py:303-307` |
| 🔴 | 默认维度 768 vs embedder 384 **不匹配** | `vector_store.py:118` vs `text_embedder.py:75` |
| 🔴 | 无 upsert，重复 build 重复插入 | `neo4j_store.py`（MERGE 零命中） |
| 🟠 | `merge_entities` 默认 False，去重不生效 | `graph_builder.py:57` |
| 🟠 | 默认 pipeline 是空占位步，产出空图 | `orchestrator.py:760-761` |
| 🟠 | 示意图标 Store=VectorStore，实际是 GraphStore | `graph_builder.py:66`（无 vector_store 参数） |
| 🟠 | 三个 store 无任何 ABC，靠 hasattr 鸭子探测 | 全目录无 abstractmethod |
| 🟠 | `compute_delta` 的 SPARQL 语法错误 | `triplet_store.py:680` |
| 🟡 | 冲突判定用 `set(str(v))`，格式差异即误判 | `conflict_detector.py:271` |
| 🟡 | Blocking 默认只按首字母，缩写 vs 全称漏检 | `similarity_calculator.py:666` |
| 🟡 | embedding 权重默认 0.0，"语义去重"退化 | `similarity_calculator.py:96` |
| 🟡 | `ontology_aware` 分块名实不符 | `split/methods.py:1345` |
| 🟡 | Parquet/Arrow 导出被 Mock 桩替换 | `export/__init__.py:177-222` |
| 🟡 | `pdfplumber`/`python-pptx`/`pytesseract` 未写入依赖 | `pyproject.toml:74-77` |
| ⚫ | `deduplication_provenance.py:31` 导入不存在模块 | 该 wrapper 完全不可用 |

### 二次开发建议

1. **显式传实体 ID**，别依赖 `hash()` 兜底
2. **建图开 `merge_entities=True`**，否则 QA 步的去重不生效
3. **自己组装 8 步 pipeline**，用 `PipelineBuilder` 显式声明依赖
4. **接了 graph_store 时注意**：库里存的是 QA 之前的数据，需要自己调整顺序或在 build() 后重新落库
5. **用 VectorStore 必须显式 `dimension=384`**（或换 embedder）
6. **生产环境开 `decision_tracking=True`**，否则 Deliver 的决策 API 直接抛错
7. 抽取结果用 Pydantic schema 校验（`semantic_extract/schemas.py`），别直接吃 LLM 输出

---

## 附：关键文件速查

| 步 | 入口 | 核心实现 |
|---|---|---|
| 1 Ingest | `ingest/methods.py:1491` | `file_ingestor.py:118`(类型检测) `ssrf.py:205`(SSRF) `ingest_provenance.py:21` |
| 2 Parse | `parse/document_parser.py:140` | `pdf_parser.py:120` `code_parser.py:270`(AST) `structured_data_parser.py:43` |
| 3 Normalize | `normalize/text_normalizer.py:120` | `entity_normalizer.py:89` `date_normalizer.py:102` `number_normalizer.py:91` `data_cleaner.py:130` |
| 4 Extract | `semantic_extract/methods.py:2623` | `ner_extractor.py:313` `relation_extractor.py:83` `triplet_extractor.py:298` `providers.py:232`；Split：`split/splitter.py:115`、`methods.py:852` |
| 5 Build KG | `kg/graph_builder.py:422` | `entity_resolver.py:93` `temporal_model.py:27` `graph_validator.py:108` |
| 6 QA | `conflicts/methods.py:130` | `conflict_detector.py:271` `conflict_resolver.py:385` `duplicate_detector.py:186` `similarity_calculator.py:220` |
| 7 Store | `vector_store/vector_store.py:100` `graph_store/graph_store.py:525` `triplet_store/triplet_store.py:40` | `hybrid_search.py:148`(RRF) `faiss_store.py:400` `neo4j_store.py:222` |
| 8 Deliver | `context/agent_context.py:90` | `context_graph.py:3725`(BFS 因果链) `hybrid_search.py` `export/__init__.py:173` `kg_visualizer.py:79` `provenance/manager.py:1238` |
| 编排 | `core/orchestrator.py:281` | `pipeline/execution_engine.py:697`(拓扑) `:254`(调度) `failure_handler.py:68`(重试) |
