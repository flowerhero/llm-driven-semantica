# semantica 简易版 · 技能化架构（Skill-First · extract-only）

> 目标：**应用主体 = md 格式的 Skill**。语义抽取逻辑由 Skill 的声明式契约承载，
> Python 只做 Skill 确实做不到的确定性计算。
>
> 状态：**已收敛为 extract-only**（用户 2026-09 批准冗余清理：整体删除 8 个旧
> skill、按 extract-only 瘦身、文件级冗余清理、docs 同步更新）。
> 原 9-skill 规划见文末「历史归档」。

---

## 1. 当前架构（2026-09-16 起）

```
宿主 agent（豆包）读取文档（摄入/归一化外包宿主，无确定性 Python 解析）
        │  按 skills/smini-extract/SKILL.md 的契约交卷十一件套 JSON
        ▼
python -m smini.cli build --host-contract contract.json
        │  smini/steps/extract.py 薄壳映射 + 惰性锚点
        ▼
ids extract（sha256 内容寻址）→ validate extract（Schema + 领域约束）
        ▼
graph build（build_kg 消费层）→ KnowledgeGraph + HTML 查看器
```

| 组件 | 角色 | 责任方 |
|------|------|--------|
| `skills/smini-extract/SKILL.md` | 抽取契约（语义规则 + 自检清单） | 宿主大模型 |
| `references/extraction-rules.md` | 规则细则（12 类实体、12 类值类型、规则五模态、流程/状态机等） | 宿主大模型 |
| `references/host-contract.example.json` | 宿主交卷示例 | 宿主大模型 |
| `scripts/render_viewer.py` | JSON 产物 → 自包含 HTML 查看器（12 Tab） | Python |
| `smini/`（薄壳） | ID / 校验 / 建图 / CLI | Python |
| `contracts/extraction.schema.json` | 唯一线格式 | Python |

### 分工铁律

| 谁 | 做什么 |
|----|--------|
| **宿主（LLM）** | 语义抽取（实体、关系、属性、规则、流程、状态机、函数、时态、动作、约束、授权）；只交「提议」不交 ID/偏移 |
| **Python** | 内容寻址 ID（sha256）、Schema 校验、枚举兜底、惰性锚点（object_literal 派生、属性双落位、六件套 count）、建图、渲染 |

> **契约即规则（Rule-by-Contract）**：抽取规则是**声明式**的（写在 SKILL.md 与
> references），不写确定性 Python 实现。宿主交卷格式违规由 `validate extract`
> 出声（ID 为空 / 下标越界 / 字面量标志缺失 → 报错；软引用 → 警告）。

---

## 2. Python 薄壳（`smini/`）

```
smini/
├── sources.py         # 读源原语：text:// / file:// / inline；透传宿主 raw_docs
├── steps/extract.py   # ★ 宿主契约抽取（薄壳映射 + 惰性锚点）
├── steps/build_kg.py  # 实体-边图 + 属性双落位 + 六件套 count
├── llm.py             # HostAgentLLMProvider（宿主即 LLM，零凭证）+ EXTRACT_SCHEMA
├── ids.py             # sha256 内容寻址 ID（20 种）
├── runtime.py         # 原子命令：ids / validate / graph build
├── pipeline_factory.py# build_pipeline() / make_context() 组装 3 步
├── cli.py             # python -m smini.cli
└── types.py           # 数据契约（dataclass + 枚举）
```

原子命令（Skill 做不到的计算，由 Python 接管）：

| 命令 | 用途 |
|------|------|
| `ids extract` | 补算 mention/relation/triplet/rule/process/state/function/temporal/action/constraint/permission ID |
| `validate extract` | Schema 校验 + 领域约束 + 软引用警告 |
| `graph build` | 建图（惰性锚点消费层；支持 disambiguation 消歧建议） |

---

## 3. 十一件套契约（宿主交卷内容）

`entities（mentions）→ relations → triplets` 为必填核心；
`attributes / rules / processes / states / functions / temporal / actions /
constraints / permissions` 为增量扩展（v4–v7）。

详见 [`CONTRACTS.md`](CONTRACTS.md) 与 `references/extraction-rules.md`。

---

## 4. HTML 查看器

每次抽取产物（04-extraction.json / 05-graph.json）由 `scripts/render_viewer.py`
同步渲染为自包含 HTML（12 Tab：概览 / 实体 / 关系 / 属性 / 规则 / 流程 / 状态机 /
函数 / 时态 / 动作 / 约束 / 授权）。单一 HTML 文件，无外部依赖，浏览器直开。

---

## 5. 测试（126 项全绿）

```
python -m unittest discover -s tests
```

覆盖：契约协议（test_contracts）、运行时（test_skill_runtime）、宿主即 LLM
（test_llm_path）、端到端 CLI（test_pipeline）、十一件套增量
（test_attributes / test_rules / test_processes / test_predict）、七模型借鉴
（test_v7_borrow）。

---

## 6. 历史归档（原 9-skill 规划，2026-09-16 废弃）

早期规划将流水线拆为 9 个 step Skill：

```
smini-ingest → smini-parse → smini-normalize → smini-extract → smini-build-kg
→ smini-qa → smini-store → smini-deliver + smini-pipeline（主编排）
```

**废弃原因**（用户主导的「契约即规则」零规则化改造 + 冗余清理）：

1. **抽取是唯一核心能力**：parse/normalize/qa/store/deliver 的确定性实现与
   Skill 均已删除——摄入与归一化外包宿主，冲突检测/落库/检索交付不属于抽取主链路。
2. **宿主即 LLM 取代 5 类确定性原语**：spans（坐标补算）、graph QA（谓词归并/
   冲突裁决）、store（后端选择）、retrieve（检索）等不再需要确定性 Python——
   语义由宿主承担，薄壳只留 ID / 校验 / 建图。
3. **8 步中间产物（01–08 落盘）废弃**：extract-only 只需
   `04-extraction.json → 05-graph.json` 两级产物（或直接内存流）。
4. 8 个旧 Skill 目录、`smini/steps/{ingest,parse,normalize,qa,store,deliver}.py`、
   `ingestors.py` / `stores.py`、4 份旧 schema（raw/parsed/normalized/graph）、
   旧链路测试（test_pipeline 旧版 / test_skill_runtime 旧版 / test_llm_path 旧版）
   均已删除；完整历史见备份 `llm-driven-semantica-backup-20260915`。
