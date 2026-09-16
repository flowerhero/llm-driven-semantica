# smini — 实现说明（IMPLEMENTATION）

> 本体抽取（extract-only · 契约即规则）实现说明。
> 确定性、零外部依赖、内容寻址、宿主即 LLM。
>
> 配套文档：[`CONTRACTS.md`](CONTRACTS.md)（十一件套契约与数据结构）、
> [`semantica-8步流水线源码解析.md`](semantica-8步流水线源码解析.md)（原版源码解析）、
> [`SKILL-ARCHITECTURE.md`](SKILL-ARCHITECTURE.md)（Skill 架构与历史规划）。

---

## 1. 它是什么

把一段文本（或一批文件）按约定 JSON 契约抽取为**知识图谱**，并渲染自包含 HTML
查看器（`scripts/render_viewer.py`，12 Tab）。

**契约即规则（Rule-by-Contract）**：

```
ingest（读源原语）→ extract（宿主 LLM 契约抽取 ★）→ build_kg（惰性锚点消费层）
```

- **抽取的智能归宿主 agent（大模型）**：宿主读文档后按契约交卷十一件套
  `entities / relations / attributes / rules / processes / states / functions /
  temporal / actions / constraints / permissions`。
- **Python 只做确定性薄壳**：内容寻址 ID、Schema 校验、枚举兜底、惰性锚点、
  建图。**无任何确定性 Python 抽取规则**（已删除），**不配置任何
  `SMINI_LLM_*` 环境变量**，**不发起模型 HTTP 调用**（宿主即 LLM，方式 3）。
- **零依赖**：仅用 Python 标准库（`dataclasses` / `re` / `hashlib` / `unittest`）。

> 解析（parse）/ 归一化（normalize）/ 质检（qa）/ 存储（store）/ 交付（deliver）
> 步骤及其 Skill 已随冗余清理**删除**：摄入解读与归一化外包宿主，冲突检测、
> 落库与检索交付不属于抽取主链路。

---

## 2. 目录结构

```
smini/
├── __init__.py          # 公共 API：build_pipeline / make_context + 全部类型/步骤
├── types.py             # 全流水线统一数据契约（dataclass 层，含十一件套类型）
├── protocols.py         # PipelineStep 三段式契约 + Pipeline 编排 + RunContext
├── ids.py               # 确定性内容寻址 ID 生成器（sha256）
├── llm.py               # 「智能挪进大模型」唯一接缝：LLMProvider + EXTRACT_SCHEMA + 测试桩
├── sources.py           # 读源原语（text:// / file:// / inline；透传宿主 raw_docs）
├── pipeline_factory.py  # build_pipeline() / make_context() 组装 3 步
├── runtime.py           # 原子命令：ids / validate / graph build
├── cli.py               # `python -m smini.cli build ...` 命令行入口
├── __main__.py          # 转发到 cli.main()
└── steps/
    ├── extract.py       # ★ 宿主 LLM 契约抽取（薄壳映射 + 惰性锚点）
    ├── build_kg.py      # 实体-边图 + 属性双落位 + 六件套 count（消费层兜底）
    └── __init__.py
contracts/               # 唯一 JSON Schema：extraction.schema.json
skills/smini-extract/    # 宿主契约 Skill（SKILL.md / references/ / scripts/render_viewer.py）
tests/                   # 126 项测试（契约 + 端到端 + 运行时）
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

`execute()` 是模板方法：`_check_precondition`（reads 非空）→ `select` → `transform`
→ `commit`。编排层用 `reads`/`writes` 反推依赖顺序（`validate_step_order`）。

---

## 4. 各步职责（实现要点）

| 步 | 输入 → 输出 | 关键实现 |
|----|------------|---------|
| **ingest** | source spec → `list[RawDocument]` | 支持 `text://` / `file://` / 纯路径 / inline；`ctx.raw_docs` 非空时**透传宿主已摄入内容**（摄入/归一化外包宿主）；Web 源显式抛 `StepError`。 |
| **extract** | `NormalizedDocument` → `list[ExtractionResult]` | **★核心**：宿主按契约交卷（十一件套），薄壳映射 `_LITERAL_TYPES` 枚举兜底、span 尽力 `find` 定位（失败置 `char_start=-1`，ID 退化为 canonical）、去重。无 LLM → 空结果 + `Degradation`（零规则无兜底）。 |
| **build_kg** | `ExtractionResult` → `KnowledgeGraph` | 惰性锚点消费层：`object_literal` 由 `object_type` 派生；属性双落位（`entity.properties` + 字面量边）；规则/流程/状态机/函数/时态/动作/约束/授权六件套 count 登记进 `graph.metadata`；`entity_id` 内容寻址保证幂等，字面量宾语 → `edge_id(subj, pred, None, lit)`。 |

---

## 5. 关键设计决策

### 5.1 契约即规则（Rule-by-Contract）—— 智能从硬代码挪进大模型

抽取规则不写在 Python 里，而是以**声明式契约**交付给宿主大模型：
`contracts/extraction.schema.json` 定义十一件套的结构与枚举，
`skills/smini-extract/SKILL.md` 定义语义抽取规则（实体类型、属性值类型、
规则五模态、流程/状态机结构等），宿主按契约交卷即可。
Python 不再「实现规则」，只做**契约校验 + 确定性映射 + 枚举兜底**。

### 5.2 宿主即 LLM（方式 3 · 生产默认）

```python
from smini.llm import HostAgentLLMProvider
llm = HostAgentLLMProvider()          # 不读取 SMINI_LLM_*、不发起 HTTP
llm.inject({"entities": [...], ...})  # 宿主按契约交卷
```

- CLI：`python -m smini.cli build --sample --host-contract contract.json`
- 未交卷（未注入）→ `is_available() == False` → 空结果 + `Degradation(no_credential)`，
  **不静默、不伪造**（零规则架构没有确定性兜底可回退）。
- 生产默认实现即 `HostAgentLLMProvider`（`default_llm`），**天然无凭证依赖**。

### 5.3 内容寻址 ID（`ids.py`）

所有 ID 由内容 sha256 生成（字段间用 `\x1f` 分隔）：`stable_id` / `content_hash` /
`doc_id` / `chunk_id` / `mention_id` / `entity_id(type, canonical)` / `edge_id` /
`triplet_id` / `rule_id` / `process_id` / `step_id` / `flow_id` / `state_machine_id` /
`state_id` / `transition_id` / `function_id` / `temporal_id` / `action_id` /
`constraint_id` / `permission_id`。
**好处**：「合并」退化成一次 dict 键碰撞，重跑同一批数据天然幂等。

### 5.4 惰性锚点（消费层兜底，不造假）

宿主交卷**可省略**的派生量由 `build_kg` 消费层确定性补齐：

- `object_literal`：`object_type` ∈ 数值类（DATE/MONEY/PERCENT/QUANTITY）→ `true`，
  宾语类型透传（不降为 OTHER）；
- 属性双落位：`attributes[]` 同时写入 `entity.properties` 与字面量边；
- 六件套 count：rules/processes/states/functions/temporal/actions/constraints/
  permissions 数量登记进 `graph.metadata`（`rule_count` / `process_count` …）。

### 5.5 双时态（`BiTemporal` / `TimeInterval`，保留）

- `valid_time`：事实在现实世界哪段为真；`transaction_time`：系统哪段采信它。
- 边相同 `(subject,predicate,object)` 但**不同时态** → 追加为新版本；相同时态 → 合并置信度。
- 建图用固定事务时间 `FIXED_TX`（`build_kg.py`），重跑不追加新版本（幂等）。

### 5.6 降级显式化

任何「能力缺失」都必须登记 `Degradation`（`component` / `kind` / `reason` /
`fallback` / `impact`），绝不静默降级。extract 无 LLM → `extract.llm` / `no_credential`。

---

## 6. 运行方式

### 6.1 命令行（CLI）

```bash
# 内置样例（宿主未交卷 → 空抽取 + 明确提示，管线跑通）
python -m smini.cli build --sample

# 宿主 agent 充当 LLM：按契约交卷
python -m smini.cli build --sample --host-contract contract.json

# 自定义文本 / 文件
python -m smini.cli build "text://特斯拉公司成立于2003年，总部位于美国加州。"
python -m smini.cli build "file:///path/to/doc.txt"

# 只跑到某步（含），便于调试
python -m smini.cli build --sample --stop extract

# 把整条 state 导出为 JSON
python -m smini.cli build --sample --json state.json
```

确定性运行时原子命令（`python -m smini.cli <cmd>`，Skill 做不到的计算）：

```bash
python -m smini.cli ids      extract --in 04-extraction.json          # 补算 ID
python -m smini.cli validate extract --in 04-extraction.json          # Schema + 领域约束
python -m smini.cli graph    build --in 04-extraction.json --out 05-graph.json
```

> 注意：本实现**零第三方依赖**，任意 Python 3.9+ 环境即可直接 `python -m smini.cli`。

### 6.2 Python API

```python
from smini import build_pipeline, make_context, HostAgentLLMProvider

sources = ["特斯拉公司成立于2003年，总部位于美国加利福尼亚州。"]
llm = HostAgentLLMProvider().inject({...})   # 宿主按契约交卷
ctx = make_context(sources, llm=llm)
pipeline = build_pipeline(sources, llm=llm)
state, results = pipeline.run(ctx=ctx)

print(state.graph.stats)            # 图谱统计（实体/边/类型分布）
print(state.graph.metadata)         # 六件套 count 等
```

宿主已用自身工具摄入文档时，可经 `ctx.raw_docs` 直投（跳过本地读源）：

```python
ctx = make_context(raw_docs=[my_raw_document])
state, _ = build_pipeline(llm=llm).run(ctx=ctx)
```

---

## 7. 测试

```bash
# 一次跑全部（126 项）
python -m unittest discover -s tests -v
```

- `tests/test_contracts.py`：PipelineState 协议、类型契约（字段所有权/枚举）。
- `tests/test_pipeline.py`：3 步顺序、图谱构建、幂等、CLI 子进程（含
  `--host-contract` 宿主交卷路径与坏契约文件报错）。
- `tests/test_llm_path.py`：**证明「宿主即 LLM」链路正确且幂等**——类型透传
  （不降 OTHER）、字面量边派生、幂等；LLM 不可用 → 空结果 + Degradation；
  `HostAgentLLMProvider` 未注入不可用 / 注入后驱动薄壳 / 生产默认即宿主。
- `tests/test_skill_runtime.py`：`ids extract` 补算幂等、LLM 提议态 ≡ 薄壳产物
  （★核心契约）、Schema 校验拦错、revive 反序列化、两次构建逐字节一致。
- `tests/test_attributes.py` / `test_rules.py` / `test_processes.py` / `test_predict.py`：
  十一件套增量（属性双落位、规则五模态、流程/状态机、预测决策六件套）全链路。
- `tests/test_v7_borrow.py`：从 ontology-driven-dev 七模型借鉴的增量能力。

---

## 8. 已知边界

- **扫描版 PDF 的 OCR**：裸字节 PDF 无文本层时由宿主工具判定并降级，本项目不做 OCR。
- **敏感文档外联**：证券/法规类敏感文件送外部 LLM 前需用户同意；宿主 agent 负责合规闸门。
- **Web 抓取**：`resolve_source` 对 `http(s)://` 显式抛 `StepError`（避免静默外联）；
  需联网摄入时由宿主 agent 用自身浏览器/抓取工具完成。
- **抽取无确定性兜底**：宿主未交卷契约 → 空结果 + `Degradation(no_credential)`，
  这是「契约即规则」的必然代价——Python 不再具备回退抽取能力。
