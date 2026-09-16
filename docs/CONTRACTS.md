# smini — 接口契约（CONTRACTS）

> extract-only · 契约即规则（Rule-by-Contract）。
> **唯一线格式**：`contracts/extraction.schema.json`（十一件套契约）。
> 宿主 agent（大模型）按此契约交卷，Python 薄壳校验 + 确定性映射。

---

## 1. 契约全景

```
宿主（大模型）按契约交卷 JSON
        │  python -m smini.cli build --host-contract <file>
        ▼
ExtractionResult（十一件套）
        │  ids extract（sha256 内容寻址补算）
        │  validate extract（Schema + 领域约束）
        ▼
build_kg（惰性锚点消费层）→ KnowledgeGraph
```

| 层 | 契约载体 | 责任方 |
|----|---------|--------|
| 抽取语义 | `skills/smini-extract/SKILL.md` + `references/extraction-rules.md` | 宿主大模型 |
| 线格式 | `contracts/extraction.schema.json` | Python 校验 |
| 数据类 | `smini/types.py`（dataclass） | Python |
| 步骤协议 | `smini/protocols.py`（PipelineStep / Pipeline / RunContext） | Python |

---

## 2. ExtractionResult —— 十一件套

顶层字段（`extraction.schema.json`）：

| 字段 | 含义 | 必填 |
|------|------|------|
| `doc_id` | 文档 ID（Python `doc_id()` 内容寻址） | ✓ |
| `mentions[]` | 实体提及（surface → canonical） | ✓ |
| `relations[]` | 关系提议（subject_ref/object_ref 引用 mentions） | ✓ |
| `triplets[]` | 归一化三元组（subject/predicate/object + 类型） | ✓ |
| `chunks[]` | 文本分块（span 锚点） | — |
| `rules[]` | 业务规则（五模态） | — |
| `processes[]` | 业务流程（steps + flows） | — |
| `states[]` | 状态机（states + transitions） | — |
| `functions[]` | 函数/指标 | — |
| `temporal[]` | 时态事实 | — |
| `actions[]` | 业务动作 | — |
| `constraints[]` | 约束 | — |
| `permissions[]` | 授权 | — |
| `degradations[]` | 降级记录（五要素） | — |
| `stats` | 统计（mode: llm / deterministic） | — |
| `document_meta` | 文档元信息（domain/source/version/doc_type） | — |

### 2.1 mentions[]（实体提及）

```
mention_id / doc_id / text / normalized / entity_type / char_start / char_end
/ chunk_id / sentence_id / confidence / extractor / provenance / metadata
```

- required：`text` / `normalized` / `entity_type`
- **契约即规则**：宿主不必给 `mention_id`（`ids extract` 补算）；span 尽力
  `find` 定位，失败置 `char_start=-1`（ID 退化为 canonical 稳定生成，validate 放行）

### 2.2 relations[]（关系提议）与 triplets[]（归一化三元组）

```
relations: relation_id / subject_ref / object_ref / predicate / prop_kind / strength
           / evidence_span / evidence_text / confidence / ...
triplets:  triplet_id / subject / subject_type / predicate / object
           / object_type / object_literal / value_type / prop_kind / strength / ...
```

- required（relations）：`subject_ref` / `object_ref` / `predicate`
- required（triplets）：`subject` / `subject_type` / `predicate` / `object`
- `object_literal`：**惰性锚点** —— 由 `object_type` ∈ 数值类
  （DATE/MONEY/PERCENT/QUANTITY）派生，宿主可省略
- `value_type`：属性值类型（12 类，见 `extraction-rules.md`）
- `prop_kind` / `strength`：关系语义（`RelationPropKind`：fact/attribute/rule/…）

### 2.3 增量六件套（v4–v7 扩展）

| 集合 | 必填字段 | 说明 |
|------|---------|------|
| `rules[]` | rule_id / doc_id / subject / condition / action / modality | 五模态（must/should/may/prohibited/conditional），`rule_type` / `certainty` / `reused_by` / `output_type` |
| `processes[]` | process_id / doc_id / name / steps | steps + flows（from/to 下标）；`preconditions` / `postconditions` |
| `states[]` | sm_id / doc_id / object / name / states | 状态机；states + transitions（from/to 下标） |
| `functions[]` | function_id / doc_id / name / formula | 输入输出、output_type |
| `temporal[]` | temporal_id / doc_id / subject / kind / value | kind 枚举（成立/失效/周期/…） |
| `actions[]` | action_id / doc_id / name / level | level 枚举（组织级/流程级/…） |
| `constraints[]` | constraint_id / doc_id / subject / type / description | type 枚举 |
| `permissions[]` | permission_id / doc_id / actor / action / effect | effect 枚举（allow/deny），`role` / `actor_type` |

---

## 3. 数据类（`smini/types.py`）

全流水线统一 dataclass 契约，与 schema 一一对应：

- **文档**：`RawDocument` / `ParsedDocument` / `NormalizedDocument` / `SourceRef`
- **抽取**：`ExtractionResult` / `Chunk` / `EntityMention` / `Relation` / `Triplet`
- **图谱**：`Entity` / `KGEdge` / `KnowledgeGraph` / `GraphStats` / `Provenance`
- **规则**：`Rule`（`RuleModality` / `RuleType` / `Certainty`）
- **流程**：`Process` / `ProcessStep` / `ProcessFlow`（`StepKind` / `FlowType`）
- **六件套**：`StateMachine` / `StateDef` / `TransitionDef` / `Function` /
  `Temporal` / `Action` / `Constraint` / `Permission`
- **枚举**：`EntityType`（12 类封闭 + 金融扩展）/ `AttributeValueType`（12 类）/
  `BlockKind` / `DocumentFormat` / `ConflictKind` / `ResolutionStrategy` /
  `DegradationKind` / `RelationPropKind` / `TemporalKind` / `ActionLevel` /
  `ConstraintType` / `PermissionEffect`
- **降级**：`Degradation`（component / kind / reason / fallback / impact / recoverable）
- **协议层**：`PipelineState`（全字段，见下）/ `PipelineStep` / `Pipeline` / `RunContext`

### 3.1 PipelineState 全字段

```
raw            RawDocument[]        ingest 产出（透传宿主或本地读源）
parsed         ParsedDocument[]     （保留字段，extract-only 下不使用）
normalized     NormalizedDocument[] extract 输入（薄壳从 raw 构造）
extractions    ExtractionResult[]   extract 产出（十一件套）
graph          KnowledgeGraph | None  build_kg 产出
qa             QAResult | None       （保留字段，旧链路已删，恒为 None）
receipt        StoreReceipt | None   （保留字段，旧链路已删，恒为 None）
delivered      ContextPackage | None （保留字段，旧链路已删，恒为 None）
FIELD_OWNER    dict[str, str]        字段 → 步骤名映射（协议校验用）
```

> 注：`parsed/qa/receipt/delivered` 为兼容协议测试保留全字段，extract-only
> 主链路不写入。

---

## 4. 领域约束（Schema 表达不了，`validate extract` 执行）

| 检查 | 规则 |
|------|------|
| mention span | `char_start >= 0` 时须 `char_end > char_start`（-1 未定位放行） |
| 字面量一致性 | `object_type` ∈ 数值类且 `object_literal != true` → 报错 |
| ID 非空 | mentions / rules / processes / steps / flows / states / transitions / functions / temporal / actions / constraints / permissions 的 ID 为空 → 报错（应先跑 `ids extract`） |
| label 非空 | process.steps / state.states 的 label 为空 → 报错 |
| 下标越界 | process.flows 与 state.transitions 的 from/to 越界 → 报错 |
| 软引用（⚠） | rules.reused_by / steps.sub_process_ref / permissions.actor / actions.actor 未匹配 → 警告不阻断 |

---

## 5. 校验器（`validate_against_schema`）

轻量 JSON Schema 校验器（`runtime.py`，纯标准库）：

- 类型检查（string / number / integer / boolean / array / object）
- required 检查、enum 检查、`$ref` 解析（definitions）
- `additionalProperties` 宽松（不拒绝多余字段）
- 数组逐元素校验，路径定位到 `$[i].field`

---

## 6. 测试口径

| 文件 | 覆盖 |
|------|------|
| `test_contracts.py` | PipelineState 协议、字段所有权、类型契约 |
| `test_skill_runtime.py` | ids 幂等 / LLM 提议 ≡ 薄壳 / validate 拦错 / revive / 幂等 |
| `test_llm_path.py` | 宿主即 LLM 接缝、类型透传、字面量派生、降级 |
| `test_pipeline.py` | 3 步顺序、图谱、CLI（含 --host-contract） |
| `test_attributes/rules/processes/predict.py` | 十一件套增量全链路 |
| `test_v7_borrow.py` | 七模型借鉴增量 |

共 **126 项**，`python -m unittest discover -s tests` 全绿。
