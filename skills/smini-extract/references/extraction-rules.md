# 抽取契约与提示词指引（契约即规则版）

> 原文件是「规则出处（逐条来自 semantica 源码提炼）」。零规则改造后，
> 抽取语义全部按约定契约交卷（契约即规则），本文件改为**契约说明
> + 提示词指引**：哪些是硬约束（schema/枚举）、哪些是给宿主的原则（判定
> 语义）、哪些是消费层兜底（惰性锚点）。不再有可执行代码规则。
>
> 说明：下文「LLM」均指**宿主 agent**——Python 不再自调任何模型
> API、不再读 `SMINI_LLM_*` 环境变量；宿主按契约交卷（`inject` 或 CLI
> `--host-contract`），薄壳只做确定性映射。

---

## §1 实体类型：19 类封闭枚举（硬约束，schema 校验）

### 1.1 通用 12 类（12 通用 = MUC-6/7 全 7 类 + OntoNotes 5.0 3 类 + 自定义 2 类）

| 枚举值 | 含义 | 给 LLM 的判定原则 | 来源 |
|---|---|---|---|
| `PERSON` | 人名 | 专名，如 马斯克 / 山姆·奥特曼 | MUC-6/7 |
| `ORGANIZATION` | 组织 | 公司/集团/大学/机构等，如 特斯拉 | MUC-6/7 |
| `LOCATION` | 地点 | 省/市/国/州/路等，如 美国、旧金山 | MUC-6/7 |
| `DATE` | 日期 | 2003年 / 2023年6月 | MUC-6/7 |
| `TIME` | 时刻 | 时分秒表达 | MUC-6/7 |
| `MONEY` | 金额 | 967亿美元 / 1万亿美元 | MUC-6/7 |
| `PERCENT` | 百分比 | 30% | MUC-6/7 |
| `QUANTITY` | 度量 | 5吨 / 10公里 | OntoNotes 5.0 |
| `PRODUCT` | 产品 | 品牌/型号（实物商品/服务） | OntoNotes 5.0 |
| `EVENT` | 事件 | 会议/赛事/发布会 | OntoNotes 5.0 |
| `CONCEPT` | 概念 | 抽象术语（本体抽取场景自定义） | 自定义（本体场景） |
| `OTHER` | 其他 | **逃生口**：判不出就用它，不得自创类型 | 逃生口 |

> **权威出处**：前 7 类（PERSON/ORGANIZATION/LOCATION/DATE/TIME/MONEY/PERCENT）
> 与 MUC-6/7 经典 NER 标签集逐字一致（NIST MUC 评测，Grishman & Sundheim 1996、
> Chinchor 1998）；PRODUCT/EVENT/QUANTITY 取自 OntoNotes 5.0 的 18 类
> （LDC2013T19，Weischedel et al. 2013）。OntoNotes 其余类做粗粒度归并：
> GPE→LOCATION、ORDINAL/CARDINAL→字面量（走 attributes）、
> NORP/FAC/WORK_OF_ART/LAW/LANGUAGE→CONCEPT/OTHER。

### 1.2 金融领域扩展 7 类（金融文本专用）

> 依据金融 NER 学术 taxonomy（SciPublication JACS 2024：FIN_INST/REG_ID/KPI/
> LEGAL/TRANS/RISK 等 12 主类）与金融领域惯例。**分界交给 LLM 语义判断，
> Python 只在 schema 层校验并兜底 `OTHER`**。非金融文本建议不用（继续 CONCEPT/OTHER）。

| 枚举值 | 含义 | 给 LLM 的判定原则（分界） | 对应学术类别 |
|---|---|---|---|
| `FINANCIAL_INSTRUMENT` | 金融工具/证券 | 股票、债券、基金、期货、期权、理财、资管产品、存款、贷款、可转债等**金融契约/投资标的**；与 `PRODUCT` 分界：金融契约→本类，实物商品/服务→`PRODUCT` | FIN_INST |
| `REGULATION` | 监管法规/规范性文件 | 具名的法律、行政法规、部门规章、自律规则、指引/办法（如《证券法》《指引》）；与 `CONCEPT` 分界：具名法规文件→本类，纯抽象术语→`CONCEPT` | LEGAL / REG_ID |
| `FINANCIAL_INDICATOR` | 财务/业务指标 | 市盈率、ROE、杠杆率、风险承受能力等级、风险等级、利率、汇率、准备金率等**可量化/可分级的金融指标**；与 `CONCEPT` 分界：金融领域量化/分级指标→本类，一般抽象概念→`CONCEPT` | KPI |
| `MARKET` | 市场/交易场所 | 上交所、深交所、北交所、新三板、场内/场外市场、衍生品市场等**交易场所/市场载体**；与 `ORGANIZATION` 分界：交易场所/市场→本类，一般公司/机构→`ORGANIZATION` | —（金融惯例） |
| `RISK` | 风险类别 | 信用风险、市场风险、操作风险、流动性风险、声誉风险、合规风险、系统性风险等**具名风险类型**；与 `FINANCIAL_INDICATOR` 分界：风险等级/度量（如"高风险"）→`FINANCIAL_INDICATOR`，具体风险类型→本类 | RISK |
| `TRANS` | 交易/业务类型 | 开户、申购、赎回、销售、代销、转让、质押、融资融券、做市、承销等**金融业务/交易活动**；与 `EVENT` 分界：金融业务/交易动作→本类，一般事件（会议/发布会）→`EVENT` | TRANS |
| `SECURITY_CODE` | 证券代码 | 600519.SH、300750.SZ、000001、SH600519 等**证券/产品代码**；作为实体引用时用本类（如"300750.SZ 上涨"）；与 attributes 分界：被引用为实体的代码→本类，仅作值出现→字面量 | —（金融惯例，TICKER） |

> 原正则判定线索（中文后缀、英文大小写等）已删除，不再作为可执行规则。
> 类型判断是 LLM 的语义职责；Python 只在 schema 层校验并兜底 `OTHER`。

## §2 伪实体原则（从黑名单升级为提示词约束）

原版用 `_BAD_SUBSTR` / `_ORG_BLACKSTART` 子串黑名单拦截「把句子片段当
实体名」的错误（原版踩坑：「特斯拉是一家人工智能公司」被抽成 ORGANIZATION
=「是一家人工智能公司」）。零规则后黑名单删除，改由提示词约束：

> 实体必须是专有名词性成分；不要抽句子片段或指代词（如「该公司」「这个」）。

LLM 泛化能力覆盖原黑名单的语义，且能处理未被枚举的伪实体。

## §3 实体属性（DatatypeProperty）：与关系的唯一硬分界

`attributes[]` 通道抽取实体的**字面量数据属性**（本体论
DatatypeProperty），与关系（ObjectProperty）形成互补：

| | 关系 `relations[]` | 属性 `attributes[]` |
|---|---|---|
| 宾语 | 独立实体（可单独成节点） | **字面量**（不是实体） |
| 对应本体论 | ObjectProperty | DatatypeProperty |
| 建图 | 加边（指向实体节点） | 加字面量边 + `Entity.properties` |
| 示例 | 特斯拉 总部位于 美国加州 | 特斯拉 成立年份=2003年 / 员工数=12万 / 上市=是 |

**唯一硬分界（写进提示词，LLM 语义判断，无代码规则）**：
> 宾语是独立实体 → `relations`；宾语是字面量（日期、金额、数字、百分比、
> 程度词、真/假等，不是一个实体）→ `attributes`。

**value_type：属性值类型独立 10 类枚举**（不复用实体类型，schema 校验）：

| 枚举值 | 含义 | 示例 |
|---|---|---|
| `DATE` | 日期 | 2003年 / 2023年6月 |
| `TIME` | 时刻 | 14:30 |
| `MONEY` | 金额 | 10亿元 / 967亿美元 |
| `PERCENT` | 百分比 | 30% |
| `QUANTITY` | 带单位度量 | 5吨 / 12万（人） |
| `NUMBER` | 纯数字 | 3家 / 5名 |
| `BOOLEAN` | 是/否、有/无 | 是 / 已实施 |
| `ENUM` | 枚举值 | 高/中/低、合格/不合格 |
| `STRING` | 普通字符串 | 地址、名称片段 |
| `OTHER` | 其他 | **逃生口**：判不出就用它 |

- `value` 必须**逐字取自原文**；`entity` 用实体的 `canonical`（须与
  `entities` 中某条一致）；`name` 是归一化属性名（如「注册资本」）。
- **双轨向后兼容**：`relations` 里的字面量宾语（DATE/MONEY/PERCENT/QUANTITY
  → object_literal）**不迁移**到 `attributes`，新产出优先走 `attributes`。
  同一事实关系版与属性版并存时，消费层以字面量边为权威，属性视图聚合。
- **属性归属允许跨实体**：归属判断交给 LLM 提示词；`entity` 匹配不到
  `entities[]` 时，薄壳合成 mention（兜底 `OTHER` 实体）并保留属性，不丢。

## §4 object_literal：由 object_type 派生（惰性锚点）

```
object_type ∈ {DATE, MONEY, PERCENT, QUANTITY}  →  object_literal = true
```

- **LLM 不输出 object_literal**，只输出 `object_type`。
- 薄壳映射时从 `object_type` 派生一次；`build_kg.py` 消费时再强制修正一次
  （上游漏标/标错也能兜底）。
- 为什么必须落在消费层：建图时 `KGEdge` 的 `object_id` 与 `object_literal`
  互斥填充——字面量加属性，实体加边。

## §5 span 与 evidence：溯源降级为「片段引用」

- `char_start/char_end`：薄壳对 surface 尽力 `find`；找不到给 `(-1,-1)`，
  由「未定位」处理（mention_id 退化为 `stable_id("mention", canonical)`）。
  validate 对 `-1` 放行。
- `evidence`：LLM 提供支撑关系的**原文短引用**（`evidence_text`），薄壳尽力
  定位成 `evidence_span`，找不到为 `null`。精确字符区间降级为片段引用。

## §6 切分：简化为单 chunk

原 entity-aware 切分（300 字符、绝不从实体中间下刀）已删除。零规则下
chunk 仅用于统计/溯源，薄壳产出单个覆盖全文的 chunk。超长文档的上下文
控制由调用方在送 LLM 前自行处理。

## §7 宿主未充当 LLM（未交卷）时的行为（不造假）

| 触发条件 | 结果 | Degradation.kind |
|---|---|---|
| 宿主未交卷（`HostAgentLLMProvider` 未 `inject` / CLI 未传 `--host-contract`） | 空结果 + 降级登记，`stats.mode="empty"` | `no_credential` |
| 宿主交卷但调用失败 | 同上 | `unavailable` |

> 零规则架构**没有确定性兜底**：`smini fallback extract` 已删除。需要抽取
> 就必须由**宿主 agent 交卷**或走 Skill。降级必须出声，绝不静默。


## §9 渲染 HTML 版（配套查看器）

> 每次产出契约 JSON / `04-extraction.json` 后，**同步渲染一个自包含 HTML 查看器**，
> 供人直接浏览，避免裸 JSON 难读。脚本：`scripts/render_viewer.py`（零依赖，Python 3 标准库）。

**命令**：

```bash
python scripts/render_viewer.py --in <contract.json> --out <contract.html> [--title "…"]
```

- `--in`：契约 JSON（`{entities, relations, attributes, rules, processes}`）**或** `04-extraction.json`
  （`{mentions, triplets, rules, processes}`，自动从 mentions 转实体、triplets 转关系并回填 evidence；
  属性从 `value_type` 非空的三元组自动归入属性清单；规则直接取自 `rules`；流程直接取自 `processes`）
- `--out`：缺省 = 输入路径换 `.html`
- `--title`：页面标题（缺省 = `smini 契约查看器 · <输入文件名>`）
- 退出码：0 成功 / 2 输入缺失、JSON 非法或无实体关系数据

**产物形态**（单文件、内嵌数据、离线可用、无外部库）：

| 区块 | 内容 |
|---|---|
| 顶部统计 | 实体数 / 关系数 / **属性数** / **规则数** / **流程数** / 类型数 / 字面量数 + 类型占比分布条 |
| 关系图谱 | **力导向布局**（节点按关系聚团、度大者居中偏大）+ **交互**（拖拽节点/滚轮缩放/拖空白平移/点击聚焦邻域、悬停高亮）+ **按子图拆分**（连通分量小图）+ 边按**谓词着色**（有向箭头、平行边曲率错开）+ 类型/谓词图例 |
| 实体清单 | `surface / canonical / type` 表格，可搜索 + 按类型筛选 |
| 关系清单 | 主语 / 谓词 / 宾语 / object_type / evidence（原文引用）五列表格 |
| **属性清单** | 实体 / 属性名 / 值 / **value_type** / evidence 五列表格，可搜索 + 按值类型筛选 |
| **规则清单** | 主体 / 条件（IF）/ 动作（THEN）/ **modality** / evidence 五列表格，可搜索 + 按模态筛选 |
| **流程清单** | 每个流程：SVG 线性流程图（实线=顺序、虚线=并行、带条件=排他分支、回环=循环）+ 步骤表（#/步骤/kind/执行者/evidence）+ 控制流表（from/to/type/条件/evidence） |

**在 Skill 流程中的位置**：Workflow 第 5 步（薄壳落盘 `04-extraction.json` 后立即调用）；
自检清单 A 最后一条要求确认已生成同名 `.html`。

## §10 业务规则抽取（条件-动作/结论 + 道义模态）

`rules[]` 通道，抽取输入文件中的**业务规则**（规范性/条件性知识）。
规则是「条件-动作/结论」结构，对齐 SWRL（Body/IF → Head/THEN）骨架 + 道义
模态，作为 ExtractionResult 独立产出。

### 10.1 判定原则（写进提示词，无代码规则）

> 含**道义动词**（应当/必须/应/须/不得/禁止/严禁/可以/有权/允许）或
> **条件-动作结构**（如果/当…时…则…）→ 抽为 `rules`；
> 纯事实陈述（宾语是实体 → relations；宾语是字面量 → attributes）**不**进 rules。
> 四件套并行不互斥（同一句可同时产出关系与规则）。
> **不抽**定义、标题、序言、交叉引用表等非规则段落（防规则集膨胀——De Jure 做法）。

**规则四层分级判定顺序（2026-09-20 借鉴 sharptoolbox v9，写进提示词）**：
规则按「生效位置」分四层，抽取出时按下列顺序**逐层下沉**，避免规则集膨胀：

| 层 | 判定 | 放哪 | 示例 |
|---|---|---|---|
| L1 属性内置 | 值域/必录是**单个属性的取值约束** | `attributes[].required` / `value_type`（必要时 `constraints[]`） | "问卷应当包含风险承受能力评估" → required=true |
| L2 refRules | 规则是**实体-实体间的引用/关联约束**（引用另一实体才成立） | `relations[]`（必要时 `rules[]` 标注） | "匹配与其风险承受能力等级相适应的产品或服务" → 匹配关系 |
| L3 invariants | 规则是**跨属性的不变量/整体一致性**（不依赖具体对象） | `constraints[]`（SHACL 式） | "风险等级由低至高至少划分为五级" → ENUM 约束 |
| L4 业务规则 | 规则含**道义动词或条件-动作结构**，且不属于 L1-L3 | `rules[]`（完整条件-动作+模态） | "应当进行特别的书面风险警示" → OBLIGATION |

> 同一句可能命中多层（如"应当按不低于 10% 回访"既是 L3 值域也是 L4 义务），
> 允许并存；但**先判 L1-L3，能下沉就不堆进 rules[]**。判不出层级仍按 L4 处理。

### 10.2 规则结构与五字段

| 字段 | 含义 | 约束 |
|---|---|---|
| `subject` | 规则主体（谁承担义务/被约束者） | 用实体 canonical；**可空**（纯派生/全局规则） |
| `condition` | 触发条件（前件 IF） | **可空**（无条件义务直接"应当…"） |
| `action` | 动作/结论（后件 THEN） | **必有** |
| `modality` | 规则模态（独立 5 类枚举） | 见下表 |
| `evidence` | 支撑本规则的原文短引用 | **逐字取自原文** |

### 10.3 modality：独立 5 类枚举（不复用实体/属性值类型）

| 枚举值 | 含义 | 中文触发词（给 LLM 的判定原则） |
|---|---|---|
| `OBLIGATION` | 义务 | 应当 / 必须 / 应 / 须 |
| `PROHIBITION` | 禁止 | 不得 / 禁止 / 严禁 / 不可 |
| `PERMISSION` | 许可 | 可以 / 有权 / 允许 / 可 |
| `CONDITIONAL` | 条件派生（非道义） | 如果/当…时…则…，无模态词 |
| `OTHER` | 其他 | **逃生口**：判不出就用它，不自创类型 |

> 模态映射对齐 Defeasible Deontic Logic（DDL）惯例：may→PERMISSION、
> shall not/must not→FORBIDDEN（PROHIBITION）、must→OBLIGATION。

### 10.4 惰性锚点

| 锚点 | 规则 | 位置 |
|---|---|---|
| rule_id | `sha256(subject, condition, action, modality)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| modality | 薄壳校验，非法 → `OTHER` 兜底 | 薄壳 |
| evidence span | evidence 尽力 `find`，找不到为 None | 薄壳 |
| 去重 | 同 (subject, condition, action, modality) 即同规则，按 rule_id 合并 | 薄壳 |
| 入图 | **不入实体-边图**（规则不是实体间事实）；`build_kg` metadata 登记 `rule_count` | `build_kg.py` |

### 10.5 与三件套的分界（核心）

| | 关系 `relations[]` | 属性 `attributes[]` | 规则 `rules[]` |
|---|---|---|---|
| 语义 | 实体间事实（ObjectProperty） | 实体字面量属性（DatatypeProperty） | **条件-动作/结论（规范/派生）** |
| 句式 | 主语-谓词-宾语（陈述） | 实体.属性=值（陈述） | **如果…则… / 应当… / 不得… / 可以…** |
| 判定线索 | 宾语是实体 | 宾语是字面量 | **道义动词或条件-动作结构** |
| 入图 | 加边 | 加字面量边 + 属性视图 | **独立产出，不入图** |


## §11 业务流程抽取（有序活动 + 控制流）

`processes[]` 通道，抽取输入文件中的**业务流程**（有序活动 + 控制流）。

### 11.1 本体论依据（为什么要单独抽流程）

流程在过程本体中是 **perdurant（持续体）**——在时间中延展的存在物，
区别于实体（**endurant**，在时间中持续存在的对象）：

- **DOLCE**：endurant（对象/实体）vs perdurant（过程/事件/活动）是顶层区分。
  本 skill 的 entities/attributes 抽 endurant 的静态面，processes 抽 perdurant 的动态面。
- **PSL（ISO 18629）**：Activity 是最小执行单元；activity-occurrence 关联
  timepoint；object 参与 activity。
- **BPMN 元模型**：Process / Activity / SubProcess / Event / Gateway / Actor /
  Artifact——本 skill 的 `kind ∈ {TASK, GATEWAY, EVENT}` 对齐这三类，
  其余（泳道、工件）超出抽取粒度。
- **Workflow Patterns（van der Aalst）**：基本控制模式 = Sequence / Parallel
  Split(AND) / Exclusive Choice(XOR) / Loop——本 skill 的
  `flow.type ∈ {SEQUENCE, PARALLEL, CONDITIONAL, LOOP}` 一一对应。

### 11.2 判定原则（写进提示词，无代码规则）

> 含**顺序/阶段/流程性描述**（顺序词「先/再/然后/最后」、阶段词、
> 「流程/步骤/生命周期」）→ 抽为 `processes`；纯事实（relations/attributes）
> 与纯规范（rules）**不**因流程抽取而重复——五件套并行不互斥（同一句可同时
> 是关系与流程步骤）。

### 11.3 流程结构：steps + flows

```
processes: [{ name, description, flow_type?, approval_chain?, steps:[{label, kind, actor, evidence, lane?, reject_to?, approval_outcome?, sub_process_ref?}],
              flows:[{from, to, type, condition, evidence, on_reject?}], preconditions?, postconditions? }]
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `name` | 流程名 | 短名，如「投资者适当性管理流程」（**内容寻址锚**） |
| `description` | 流程说明 | 可空 |
| `flow_type` | 流程类型（v9 M6 借鉴） | `COLLABORATION APPROVAL`；**审批/审核/复核/会签流程 → `APPROVAL`**；判不出 `COLLABORATION` |
| `approval_chain` | 审批链路摘要 | 角色/岗位名数组（如 `["初审岗","复核岗","终审岗"]`）；可空，多级审批以 steps 顺序为准 |
| `preconditions` / `postconditions` | 流程级前后置条件 | 字符串数组，自然语言，可空 |
| `steps[].label` | 步骤内容（做什么） | **必有** |
| `steps[].kind` | 步骤类型（独立 6 类枚举） | `TASK GATEWAY EVENT START END SYSTEM_TASK`；判不出用 `TASK`，不自创类型 |
| `steps[].actor` | 执行者 | 用实体 canonical（不强建新实体）；**可空** |
| `steps[].lane` | 泳道（v9 M6 借鉴） | 步骤所属角色/部门（软引用，可空） |
| `steps[].approval_outcome` | 审批结果（v9 M6 借鉴） | `APPROVE REJECT RETURN`；仅审批类步骤用；判不出省略或 `OTHER` |
| `steps[].reject_to` | 驳回目标（v9 M6 借鉴） | **整数下标**引用 steps；仅 `RETURN` 时填（回到修改/补正步骤）；越界由薄壳丢弃 |
| `steps[].sub_process_ref` | 子流程引用 | 引用另一流程 `name`；可空 |
| `flows[].from/to` | **整数下标**引用 steps（0-based） | LLM 输出下标，Python 越界校验丢弃 |
| `flows[].type` | 控制流类型（独立 4 类枚举） | 见下表 |
| `flows[].condition` | 分支条件 | 仅 CONDITIONAL 用，可空 |
| `flows[].on_reject` | 驳回边条件（v9 M6 借鉴） | 自然语言补充（如"退回补充材料"），可空；与步骤级 `reject_to` 并存时以步骤级为权威 |

### 11.4 kind / flow.type：两个独立枚举（不复用实体/属性/规则类型）

| kind | 含义 | 对齐 BPMN | 判不出 |
|---|---|---|---|
| `TASK` | 活动/任务：填表、评估、签署、回访 | Activity | **默认** |
| `GATEWAY` | 决策/分支点：判定、选择、匹配 | Gateway | |
| `EVENT` | 事件：开始/结束/触发 | Event | |

| flow.type | 含义 | 对齐 Workflow Pattern | 中文线索 | 判不出 |
|---|---|---|---|---|
| `SEQUENCE` | 顺序 | Sequence | 先…再…然后…最后；依次 | **默认** |
| `PARALLEL` | 并行（AND-split） | Parallel Split | 同时；同步；一并 | |
| `CONDITIONAL` | 排他分支（XOR-split） | Exclusive Choice | 如果…则…否则…；当…时 | |
| `LOOP` | 循环 | Loop | 重复；直到；每次…都 | |

| approval_outcome | 含义 | 中文线索 | 配合 |
|---|---|---|---|
| `APPROVE` | 通过 | 同意 / 批准 / 审核通过 | 无 |
| `REJECT` | 否决（流程终止） | 不予批准 / 否决 / 驳回申请 | 通常无 reject_to |
| `RETURN` | 退回（修改后重报） | 退回修改 / 退回重报 / 补正材料 | **必有 reject_to**（指向修改步骤） |
| `OTHER` | 逃生口 | 判不出就用它 | |

> flow_type / approval_outcome 是**流程级与步骤级的正交枚举**：flow_type 回答
> 「整个流程是不是审批流」，approval_outcome 回答「某个审批步骤的结果是什么」。

### 11.5 惰性锚点

| 锚点 | 规则 | 位置 |
|---|---|---|
| process_id | `sha256(name)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| step_id | `sha256(process_id, index, label)` 内容寻址 | `smini/ids.py` |
| flow_id | `sha256(process_id, from, to, type, condition)` 内容寻址 | `smini/ids.py` |
| kind | 薄壳校验，非法 → `TASK` 兜底 | 薄壳 |
| flow.type | 薄壳校验，非法 → `SEQUENCE` 兜底 | 薄壳 |
| flow_type | 薄壳校验，非法 → `COLLABORATION` 兜底 | 薄壳 |
| approval_outcome | 薄壳校验，非法 → `OTHER` 兜底 | 薄壳 |
| reject_to | 下标越界（<0 或 ≥len(steps)）→ **置 None**（同 flow 下标处理） | 薄壳 |
| flow 下标 | from/to 越界（<0 或 ≥len(steps)）→ **丢弃该 flow**（LLM 下标不可信） | 薄壳 |
| 去重 | 同 name 即同流程，按 process_id 合并 | 薄壳 |
| 入图 | **不入实体-边图**（流程是持续体 perdurant，不是实体间事实）；`build_kg` metadata 登记 `process_count` | `build_kg.py` |

### 11.6 与四件套的分界（核心）

| | 关系 `relations[]` | 属性 `attributes[]` | 规则 `rules[]` | 流程 `processes[]` |
|---|---|---|---|---|
| 本体论 | ObjectProperty | DatatypeProperty | 规范（SWRL 骨架+道义模态） | **perdurant 持续体** |
| 语义 | 实体间事实 | 实体字面量属性 | 该不该做（规范） | **谁→按什么顺序→做什么** |
| 句式 | 主语-谓词-宾语 | 实体.属性=值 | 如果…则…/应当…/不得… | **先…再…然后…最后 / 流程/步骤** |
| 判定线索 | 宾语是实体 | 宾语是字面量 | 道义动词或条件-动作 | **顺序/阶段/流程性描述** |
| 入图 | 加边 | 加字面量边 + 属性视图 | 独立产出，不入图 | **独立产出，不入图** |

> 流程与规则正交：规则是「条件→动作」的**规范**（该不该做），流程是
> 「步骤+控制流」的**执行结构**（怎么走）。同一句可同时是规则与流程步骤
> （如「评估完成后应当匹配适当产品」→ 既是 CONDITIONAL 规则又是
> GATEWAY 流程步骤）。

## §12 七模型字段级借鉴（保持能力范围与实现机制不变）

> 借鉴 ontology-driven-dev 的 M1/M2/M3/M5/M6 模型，为**已有通道**增补字段。
> 不加新通道（queries 待定）、不引入执行引擎/代码生成（simpleeval/flow_engine
> 明确不借鉴）。全部新字段走既有接缝：宿主契约交卷 → 薄壳枚举校验+非法兜底
> → evidence 溯源 → validate 可验证；**ID 锚点键不变**（新字段不参与寻址）。
> 设计文档：`docs/七模型借鉴增量设计方案.md`。

### 12.1 A 组字段增强（6 项）

| 通道 | 新字段 | 含义 | 枚举/约束（薄壳兜底） |
|---|---|---|---|
| `rules` | `rule_type` | 规则用途（与 modality 正交：该不该做 vs 什么用途） | `VALIDATION DERIVATION TRIGGER OTHER`；非法 → `OTHER` |
| `rules` | `output_type` | 规则输出取值形态 | 复用函数输出 7 类枚举；非法 → `OTHER`；空 → 空串（未标注） |
| `rules` | `reused_by` | 本规则被哪些流程/函数引用 | 字符串数组（引用 `name`）；单字符串自动清洗成数组 |
| `rules` | `certainty` | 内容确定性分级 | `GENERAL ENTERPRISE OTHER`；非法 → `OTHER` |
| `processes` | `preconditions` / `postconditions` | 流程级前后置条件 | 字符串数组，自然语言，可空 |
| `processes.steps` | `sub_process_ref` | 子流程引用（M6 SUB_FLOW_CALL） | 引用另一流程 `name`；仅软校验，不展开执行 |
| `processes.steps` | `kind` 扩展 | 步骤角色细分 | `START END SYSTEM_TASK` 新增；非法 → `TASK` |
| `actions` | `precondition` / `postcondition` | 动作前后置条件 | 自然语言，可空 |
| `permissions` | `role` | 角色中间层（RBAC 双层） | 角色名，可空 |
| `permissions` | `actor_type` | 主体类型 | `HUMAN SYSTEM OTHER`；非法 → `OTHER` |
| `attributes` | `required` | 属性必录性（M1 required） | 布尔；字符串 "是/必填" 也可识别；缺省 `False` |
| `attributes` | `value_type` 扩展 | 字典/实体引用 | `DICT_REF ENTITY_REF` 新增；非法 → `OTHER` |
| `permissions` | `data_scope` | 数据可见范围（v9 M5 dataScope 借鉴） | `ALL OWN DEPT CUSTOM OTHER`；非法 → `OTHER`；金融数据隔离：本人 OWN / 本部门 DEPT / 全机构 ALL |
| `processes` | `flow_type` | 流程类型（v9 M6 flowType 借鉴） | `COLLABORATION APPROVAL`；非法 → `COLLABORATION` |
| `processes` | `approval_chain` | 审批链路摘要（v9 M6 借鉴） | 角色/岗位名数组，可空；多级审批以 steps 顺序为准 |
| `processes.steps` | `lane` | 泳道（v9 M6 借鉴） | 角色/部门名（软引用），可空 |
| `processes.steps` | `approval_outcome` | 审批结果三态（v9 M6 approvalOutcomes 借鉴） | `APPROVE REJECT RETURN OTHER`；非法 → `OTHER` |
| `processes.steps` | `reject_to` | 驳回目标（v9 M6 借鉴） | 整数下标引用 steps；RETURN 必有；越界 → None |
| `processes.flows` | `on_reject` | 驳回边条件（v9 M6 借鉴） | 自然语言补充，可空 |

### 12.2 B 组结构增强（3 项）

| 项 | 内容 | 说明 |
|---|---|---|
| `document_meta` | 顶层输出头 `{domain, source, version, doc_type}` | `source` **由薄壳强制取输入文档 URI**（宿主不可控）；domain/version/doc_type 宿主可选交卷 |
| 软引用检查 | `smini validate` 对 `reused_by`/`sub_process_ref`/`actor`/`role` 做可解析性检查 | 找不到引用 → ⚠ **warning 不阻断**（读端引用本就是宽松语义，与对方"8 阶段硬暂停"的关键区别） |
| 渲染器 | `render_viewer.py` 增 v7 列 | 规则表增 用途类型/输出/复用方/确定性 4 列；属性表增 必录列；动作表增 前后置条件 2 列；授权表增 角色/主体类型 2 列；流程显示前后置条件与子流程引用；顶部显示 document_meta |


### 12.4 判定原则（写进提示词，无代码规则）

> - `rule_type`：规则在系统里怎么用——**校验**（前置条件/资格判定）、**推导**
>   （由输入算出结论，如"风险等级由低至高至少五级"→ 五级划分）、**触发**
>   （条件满足触发动作/流转）。判不出省略或 `OTHER`。
> - `certainty`：值是不是**本企业写死**的参数（阈值/时限/费率，如"≤3000元
>   自动核赔""14天内自动立案"）→ `ENTERPRISE`（消费方需人工复核）；监管/
>   行业惯例 → `GENERAL`。
> - `sub_process_ref`：步骤是**调用另一流程**（如"执行产品匹配子流程"）时，
>   填被调流程的 `name`；不是把步骤拆开重写。
> - `role` vs `actor`：`actor` 是具体主体（人/系统/岗位实例），`role` 是
>   中间层角色（如"适当性管理岗"）；文本只给角色时填 `role`，`actor` 可空。
> - `data_scope`：主体执行动作时**能看到哪一层数据**——仅本人数据 → `OWN`，
>   本部门/本单位 → `DEPT`，全机构 → `ALL`，需原文限定（如"仅限所管客户"）
>   无法归入前三类 → `CUSTOM`（配合 scope 描述）。判不出省略或 `OTHER`。
> - `flow_type`：流程是否**审批流**（含审批/审核/复核/批准/会签步骤）→
>   `APPROVAL`；一般端到端流程 → `COLLABORATION`。
> - `approval_outcome` / `reject_to`：审批步骤的结果——通过 `APPROVE`；
>   否决 `REJECT`（流程终止，通常无 reject_to）；退回 `RETURN`（**必有
>   reject_to** 指向被退回修改的步骤下标）。文本只给"退回重新提交"时，
>   reject_to 指向"提交"步骤。
> - `required`：属性是否**必录**（"应当包含""必须填写"→ true）。
> - 所有新增字段**可空**：拿不准就不输出，薄壳兜底，不自创值。

## §12 预测决策本体六件套（状态机/函数指标/时序时态/动作处置/约束/授权）

六件套通道，把业务文档从「描述事实的静态本体」升级为「可预测、
可决策的动态本体」：静态本体回答「它是什么」，动态本体回答「**它将如何
变化**」。设计文档：`docs/预测决策本体增量设计方案.md`（7 决策点已确认）。

### 12.1 本体论依据（为什么六件套独立于实体-边图）

| 通道 | 本体论映射 | 语义 | 入图 |
|---|---|---|---|
| `states[]` | FSM/状态机本体（对象生命周期拓扑） | 预测=对象在时间窗内从状态 A 演化到状态 B | **不入** |
| `functions[]` | 推演算子（可计算定义，如压力函数） | 把业务经验沉淀为可解释推演输入 | **不入** |
| `temporal[]` | OWL-Time（时间本体） | 把生效/频率/时间窗/触发时点绑定到主体 | **不入** |
| `actions[]` | 处置动作元信息（参考 Palantir ActionType） | 怎么做/什么级别/什么代价/何时触发 | **不入** |
| `constraints[]` | SHACL 约束（结构完整性） | 什么合法，保证预测不基于非法状态 | **不入** |
| `permissions[]` | RBAC/ABAC 授权 | 谁能/不能执行什么动作（决策自动化治理） | **不入** |

> 六件套均不是「实体间事实」：状态迁移、计算定义、时态绑定、处置动作、
> 结构约束、授权三元都装不进 KGEdge 的三元形态，故独立产出；
> 仅 `relations` 的 `prop_kind`/`strength` 作为**边属性**入图（传导增强）。

### 12.2 契约格式（十一件套中新增的六件）

```
states:      [{ object, name?, states:[{label, initial?}],
                transitions:[{from, to, event?, condition?, action?}] }]
functions:   [{ name, subject?, formula, inputs?, output_type, evidence }]
temporal:    [{ subject, kind, value, anchor?, evidence }]
actions:     [{ name, actor?, level, target?, side_effect?, trigger?, precondition?, postcondition?, evidence }]
constraints: [{ subject, type, description, evidence }]
permissions: [{ actor, action, effect, scope?, role?, actor_type?, data_scope?, evidence }]
processes:   [{ name, description, flow_type?, approval_chain?, steps, flows, preconditions?, postconditions? }]
```

### 12.3 六个独立枚举（不复用实体/属性/规则/流程类型）

| 字段 | 枚举 | 判定原则（给 LLM） | 判不出 |
|---|---|---|---|
| `functions[].output_type` | `NUMBER PERCENT RANK BOOLEAN ENUM STRING OTHER` | 输出值的形态 | `OTHER` |
| `temporal[].kind` | `VALIDITY FREQUENCY WINDOW TRIGGER` | 生效期/频率/时间窗/触发时点 | `VALIDITY` |
| `actions[].level` | `OBSERVE WARN INTERVENE` | 观察→警示→干预 | `OBSERVE` |
| `constraints[].type` | `CARDINALITY VALUE_RANGE ENUM DISJOINT REQUIRED CONSISTENCY` | 基数/值域/枚举/不相交/必填/一致性 | `CONSISTENCY` |
| `permissions[].effect` | `PERMIT DENY` | 允许/禁止（默认禁止=最小权限） | `DENY` |
| `permissions[].data_scope` | `ALL OWN DEPT CUSTOM OTHER` | 数据可见范围（v9 M5 dataScope 借鉴） | `OTHER` |
| `relations[].prop_kind` | `FACTUAL DEPENDENCY CAUSAL TRIGGER` | 事实/依赖/因果/触发 | 省略=普通事实 |

### 12.4 判定原则（写进提示词，无代码规则）

> 六件套与五件套并行不互斥（同一句可同时是关系与状态迁移）：
> - **states**：对象有**离散状态**且状态间存在**迁移**（如风险承受能力等级
>   C1-C5 经评估逐级变化、投资者类别经申请转化）→ 抽为状态机。与 processes
>   分界：processes 是执行步骤序列，states 是对象生命周期状态拓扑。
> - **functions**：出现**可量化的经验/阈值/定义式**（如「回访率 ≥ 10%」、
>   匹配原则）→ 抽为函数指标。与 attributes 分界：attributes 存**字面量值**
>   （注册资本=10亿元），functions 存**计算定义**（怎么算出来）；`formula`
>   用声明式自然语言/运算符描述，**不强制可执行数学表达式**。
> - **temporal**：出现**时间限定**（生效/实施日期、频率「每年」、时间窗
>   「六个月内」、触发时点）且绑定到主体 → 抽为时态。与 DATE 实体分界：
>   DATE 是图中节点，temporal 是把时间语义绑定到主体。
> - **actions**：出现**处置动作及其级别/代价/触发**（书面警示、暂停交易、
>   惩戒）→ 抽为动作处置。与 rules 分界：rules 是「应当/不得」规范，
>   actions 是具体处置元信息。
> - **constraints**：出现**结构约束**（至少划分为五级、不低于 10%、应当包含、
>   不得并存）→ 抽为约束。与 rules 分界：rules 是「该做什么」的规范，
>   constraints 是「什么合法」的结构约束。
> - **permissions**：出现**谁能/不能执行什么动作**（普通投资者可以申请转化、
>   专业投资者可以购买所有风险等级）→ 抽为授权。与 rules 的 PERMISSION
>   模态分界：授权聚焦 actor-action-effect 三元+范围，规则保留完整条件-动作结构。
> - states 的 transitions 用**整数下标**引用 states，不要自造 ID。

### 12.5 惰性锚点

| 锚点 | 规则 | 位置 |
|---|---|---|
| sm_id / state_id / transition_id | `sha256(object,name)` / `sha256(sm_id,下标,label)` / `sha256(sm_id,from,to,event,condition)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| transition 下标 | from/to 越界（<0 或 ≥len(states)）→ **丢弃该迁移**（LLM 下标不可信） | 薄壳 |
| function_id | `sha256(name, subject, formula)` | `smini/ids.py` |
| temporal_id | `sha256(subject, kind, value)` | `smini/ids.py` |
| action_id | `sha256(name, actor, level, trigger)` | `smini/ids.py` |
| constraint_id | `sha256(subject, type, description)` | `smini/ids.py` |
| permission_id | `sha256(actor, action, effect)` | `smini/ids.py` |
| 六件套枚举兜底 | output_type→`OTHER`、kind→`VALIDITY`、level→`OBSERVE`、constraint.type→`CONSISTENCY`、effect→`DENY`、data_scope→`OTHER` | 薄壳 |
| 流程审批字段兜底 | flow_type→`COLLABORATION`、approval_outcome→`OTHER`、reject_to 越界→None | 薄壳 |
| prop_kind / strength | 薄壳校验，非法/缺失 → 空（普通事实 FACTUAL） | 薄壳 |
| 去重 | 按各 ID 内容寻址合并 | 薄壳 |
| 入图 | **不入实体-边图**；`build_kg` metadata 登记六件套各自 count；`prop_kind`/`strength` 作为边属性入图 | `build_kg.py` |

### 12.6 与五件套的分界（核心）

| | 关系/属性/规则/流程 | 六件套 |
|---|---|---|
| 回答的问题 | 它是什么 / 它该做什么 | **它将如何变化 / 如何处置 / 什么合法 / 谁能做** |
| 语义 | 实体间事实、字面量、规范、执行步骤 | 生命周期状态、计算定义、时态绑定、处置元信息、结构约束、授权 |
| 入图 | 关系/属性入图；规则/流程独立 | **全部独立产出，不入图** |
| 分界 | 宾语是实体→关系；字面量→属性；道义动词→规则；顺序词→流程 | 离散状态+迁移→states；可量化经验→functions；时间限定→temporal；处置动作→actions；结构约束→constraints；谁能做→permissions |

