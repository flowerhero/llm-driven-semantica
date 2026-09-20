---
name: semantica-extract
description: "从归一化文档中抽取本体（ontology）所需的实体-关系三元组、实体属性（DatatypeProperty）、业务规则、业务流程与预测决策本体（六件套：状态机/函数指标/时序时态/动作处置/约束/授权），产出 ExtractionResult。契约即规则架构：语义抽取按约定 JSON 契约交卷（19 类实体枚举含 7 金融扩展 + 开放关系（含传导类型/强度） + 实体属性 + 业务规则 + 业务流程 + 六件套），只要求按约定 JSON 格式交卷（entities + relations + attributes + rules + processes + states + functions + temporal + actions + constraints + permissions）；字符偏移/ID/object_literal 一律不输出，由 Python 惰性锚点补算。确定性抽取规则（正则 NER、8 条关系模式、伪实体黑名单）已全部删除。Use when the user says 抽取实体/抽取关系/抽取属性/抽取规则/抽取流程/抽取状态机/抽取指标函数/抽取时态/抽取动作处置/抽取约束/抽取授权/预测决策本体/识别命名实体/跑 S4/extract entities/find relations/提取属性/业务规则/流程, or after normalize."
---

# semantica-extract · 抽取（Step 4 ★核心 · 契约即规则）

> 把干净文本变成结构化图谱素材：命名实体识别 + 类型化关系抽取 + **实体属性抽取**
> + **业务规则抽取** + **业务流程抽取**。
>
> 本 Skill 采用「**契约即规则**」架构：删除全部确定性抽取
> 规则（正则 NER、8 条关系模式、伪实体黑名单、entity-aware 切分、
> 确定性 fallback）。
> 只要求按约定格式交卷。正确性锚点惰性化到消费层（`smini/steps/build_kg.py`）。
> Python 不再自调任何模型 API、不再读 `SMINI_LLM_*` 环境变量。
>
>
## 运行时要求（Python 版本）

- **Python ≥ 3.10**（代码使用 `X | None` 注解与 `types.UnionType`，3.9 不支持）。
  推荐 **Python 3.12**：`uv run --python 3.12 -m smini.cli …`（uv 会自动匹配；
  若本地无 3.12 解释器，先 `uv python install 3.12` 或改用本机 ≥3.10 的解释器）。
- 全项目零第三方依赖，`python -m smini.cli …` 直接可跑（无需 pip install）。


## 契约（唯一约定，写进提示词）

**宿主交卷只输出十一件事（+可选 document_meta 头），不含偏移、不含 ID、不含 object_literal：**

```
document_meta?: { domain?, source?, version?, doc_type? }
entities:    [{ surface, canonical, type }]
relations:   [{ subject, predicate, object, object_type, evidence, prop_kind?, strength? }]
attributes:  [{ entity, name, value, value_type, evidence, required?, }]
rules:       [{ subject, condition, action, modality, evidence, rule_type?, output_type?, reused_by?, certainty? }]
processes:   [{ name, description, flow_type?, approval_chain?, steps, flows, preconditions?, postconditions? }]
states:      [{ object, name?, states:[{label, initial?}], transitions:[{from, to, event?, condition?, action?}] }]
functions:   [{ name, subject?, formula, inputs?, output_type, evidence }]
temporal:    [{ subject, kind, value, anchor?, evidence }]
actions:     [{ name, actor?, level, target?, side_effect?, trigger?, precondition?, postcondition?, evidence }]
constraints: [{ subject, type, description, evidence }]
permissions: [{ actor, action, effect, scope?, role?, actor_type?, data_scope?, evidence }]
```

| 字段 | 含义 | 约束 |
|---|---|---|
| `surface` | 原文表面形式 | 逐字取自原文 |
| `canonical` | 规范名 = **实体唯一身份** | 同一实体跨文档必须同一 canonical（如「特斯拉」「Tesla」都归「特斯拉」） |
| `type` / `object_type` | 19 类封闭枚举之一（12 通用 + 7 金融扩展） | 通用：`PERSON ORGANIZATION LOCATION DATE TIME MONEY PERCENT QUANTITY PRODUCT EVENT CONCEPT OTHER`；金融扩展：`FINANCIAL_INSTRUMENT REGULATION FINANCIAL_INDICATOR MARKET RISK TRANS SECURITY_CODE`；判不出用 `OTHER` |
| `object_type` | 宾语类型 | 宾语是日期/金额/百分比/数量时必须标 `DATE/MONEY/PERCENT/QUANTITY`；给不出可省略 |
| `evidence` | 支撑本关系/属性/规则的原文短引用 | 逐字取自原文，用于溯源 |
| `entity`（attributes） | 属性所属实体 | 用实体的 `canonical`（须与 `entities` 中某条一致） |
| `name`（attributes） | 归一化属性名 | 如「注册资本」「风险等级」「成立年份」 |
| `value`（attributes） | 属性值（字面量） | **逐字取自原文** |
| `value_type`（attributes） | 属性值类型，独立 12 类枚举 | `DATE TIME MONEY PERCENT QUANTITY NUMBER BOOLEAN ENUM STRING DICT_REF ENTITY_REF OTHER`；判不出用 `OTHER`，不自创类型 |
| `required`（attributes，可选） | 属性必录性 | 布尔；`true`=必录（对齐 M1 required），缺省 `false` |
| `subject`（rules） | 规则主体 | 用实体 `canonical`；**可空**（纯派生/全局规则） |
| `condition`（rules） | 触发条件（前件 IF） | **可空**（无条件义务直接"应当…"） |
| `action`（rules） | 动作/结论（后件 THEN） | **必有** |
| `modality`（rules） | 规则模态，独立 5 类枚举 | `OBLIGATION PROHIBITION PERMISSION CONDITIONAL OTHER`；判不出用 `OTHER`，不自创类型 |
| `rule_type`（rules，可选） | 规则用途类型，独立 4 类枚举（与 modality 正交） | `VALIDATION DERIVATION TRIGGER OTHER`；校验/推导/触发；判不出用 `OTHER`，不自创类型 |
| `output_type`（rules，可选） | 规则输出取值形态（复用函数输出枚举） | `NUMBER PERCENT RANK BOOLEAN ENUM STRING OTHER`；判不出省略（空=未标注） |
| `reused_by`（rules，可选） | 本规则被哪些流程/函数引用 | 字符串数组，引用流程/函数的 `name`；找不到只软警告不阻断 |
| `certainty`（rules，可选） | 内容确定性分级 | `GENERAL`（行业通用）/`ENTERPRISE`（企业专属参数，需人工复核）/`OTHER`；判不出用 `OTHER` |
| `name`（processes） | 流程名 | 短名，如「投资者适当性管理流程」（内容寻址锚） |
| `flow_type`（processes，可选） | 流程类型，独立 2 类枚举 | `COLLABORATION APPROVAL`；含审批/审核/复核/会签步骤 → `APPROVAL`；判不出用 `COLLABORATION` |
| `approval_chain`（processes，可选） | 审批链路摘要 | 角色/岗位名数组（如 `["初审岗","复核岗"]`）；多级审批以 steps 顺序为准，可空 |
| `steps`（processes） | 按执行顺序的步骤列表 | 每步 `label`（做什么，**必有**）+ `kind`（独立 6 类枚举）+ `actor`（执行者，用实体 canonical，**可空**）+ `sub_process_ref?`（子流程引用，v7）+ `lane?`/`reject_to?`/`approval_outcome?`（审批流增强，2026-09-20）+ `evidence` |
| `kind`（steps） | 步骤类型，独立 6 类枚举 | `TASK GATEWAY EVENT START END SYSTEM_TASK`；判不出用 `TASK`，不自创类型 |
| `sub_process_ref`（steps，可选） | 子流程引用 | 引用另一流程的 `name`（对齐 M6 SUB_FLOW_CALL）；仅软校验存在性，不展开执行 |
| `lane`（steps，可选） | 泳道 | 步骤所属角色/部门（软引用，如「风控审核岗」），可空 |
| `approval_outcome`（steps，可选） | 审批结果，独立 4 类枚举 | `APPROVE REJECT RETURN OTHER`；通过/否决（流程终止）/退回（配合 reject_to）/判不出 `OTHER`；仅审批类步骤用 |
| `reject_to`（steps，可选） | 驳回目标 | **整数下标**引用 steps；仅 `RETURN` 时填（回到修改/补正步骤）；越界由薄壳丢弃 |
| `preconditions` / `postconditions`（processes，可选） | 流程级前后置条件 | 字符串数组，自然语言表达（进入流程前必须成立 / 结束后必然成立） |
| `flows`（processes） | 控制流列表 | 每流 `from`/`to`（**整数下标**引用 steps）+ `type`（独立 4 类枚举）+ `condition`（仅分支用）+ `on_reject?`（驳回边条件）+ `evidence` |
| `type`（flows） | 控制流类型，独立 4 类枚举 | `SEQUENCE PARALLEL CONDITIONAL LOOP`；判不出用 `SEQUENCE`，不自创类型 |
| `prop_kind`（relations，可选） | 关系传导类型，独立 4 类枚举 | `FACTUAL DEPENDENCY CAUSAL TRIGGER`；普通事实省略即 `FACTUAL`，不自创类型 |
| `strength`（relations，可选） | 传导强度 | 如 `HIGH MEDIUM LOW`；判不出省略 |
| `object`（states） | 状态机所属对象 | 用实体 `canonical` |
| `name`（states） | 状态机名 | 短名，如「风险承受能力等级状态机」（内容寻址锚） |
| `states`（states） | 状态列表 | 每项 `label`（状态名，**必有**）+ `initial`（是否初始态，布尔，可省）+ `evidence` |
| `transitions`（states） | 迁移列表 | 每项 `from`/`to`（**整数下标**引用 states）+ `event`/`condition`/`action`（全可空）+ `evidence` |
| `name`（functions） | 指标函数名 | 短名，如「适当性匹配函数」（内容寻址锚） |
| `formula`（functions） | 声明式计算定义 | **自然语言/运算符描述，不强制可执行数学表达式**（如「投资者风险承受能力等级 ≥ 产品或服务风险等级」） |
| `inputs`（functions） | 依赖因子 | 数组；用实体 canonical 或字面量名，可空 |
| `output_type`（functions） | 输出类型，独立 7 类枚举 | `NUMBER PERCENT RANK BOOLEAN ENUM STRING OTHER`；判不出用 `OTHER`，不自创类型 |
| `kind`（temporal） | 时态类型，独立 4 类枚举 | `VALIDITY FREQUENCY WINDOW TRIGGER`；判不出用 `VALIDITY`，不自创类型 |
| `value`（temporal） | 时间限定描述 | 逐字取自原文（如「自2017年7月1日起实施」「每年」） |
| `anchor`（temporal） | 时间锚点 | 计算基准，可空 |
| `name`（actions） | 处置动作名 | 短名，如「书面风险警示」（内容寻址锚） |
| `level`（actions） | 动作级别，独立 3 类枚举 | `OBSERVE WARN INTERVENE`；判不出用 `OBSERVE`，不自创类型 |
| `side_effect` / `trigger`（actions） | 副作用代价 / 触发情形 | 可空 |
| `precondition` / `postcondition`（actions，可选） | 动作执行前后置条件 | 自然语言（做什么之前必须成立 / 执行后必然改变什么），可空 |
| `type`（constraints） | 约束类型，独立 6 类枚举 | `CARDINALITY VALUE_RANGE ENUM DISJOINT REQUIRED CONSISTENCY`；判不出用 `CONSISTENCY`，不自创类型 |
| `description`（constraints） | 约束内容 | 声明式描述 |
| `effect`（permissions） | 授权效果，独立 2 类枚举 | `PERMIT DENY`；判不出用 `DENY`（最小权限原则），不自创类型 |
| `scope`（permissions） | 授权范围 | 可空 |
| `role`（permissions，可选） | 角色中间层 | 权限经角色授予（RBAC 双层，对齐 M5 roles）；用角色名，可空 |
| `actor_type`（permissions，可选） | 主体类型，独立 3 类枚举 | `HUMAN SYSTEM OTHER`；人工/系统自动；判不出用 `OTHER`，不自创类型 |
| `data_scope`（permissions，可选） | 数据可见范围，独立 5 类枚举 | `ALL OWN DEPT CUSTOM OTHER`；全机构/本人/本部门/自定义；判不出用 `OTHER`，不自创类型 |

> **实体 vs 属性唯一硬分界**（写进提示词）：
> 宾语是独立实体（可单独成节点）→ `relations`；宾语是字面量（日期、金额、
> 数字、百分比、程度词、真/假等，不是一个实体）→ `attributes`。
> 同一事实关系版与属性版并存属**双轨**，向后兼容：`relations` 里的字面量
> 宾语不迁移到 `attributes`，新产出优先走 `attributes`。
>
> **规则判定原则**（写进提示词，无代码规则）：
> 含道义动词（应当/必须/应/须/不得/禁止/严禁/可以/有权/允许）或条件-动作
> 结构（如果/当…时…则…）→ 抽为 `rules`；纯事实陈述（宾语是实体 →
> relations；宾语是字面量 → attributes）**不**进 `rules`。四件套并行不互斥
> （同一句可同时产出关系与规则）。**不抽**定义、标题、序言、交叉引用表等
> 非规则段落（防规则集膨胀）。
>
> **流程判定原则**（写进提示词，无代码规则）：
> 含顺序/阶段/流程性描述（顺序词「先/再/然后/最后」、阶段词、流程/步骤/
> 生命周期）→ 抽为 `processes`；纯事实（relations/attributes）与纯规范
> （rules）不因流程抽取而重复——**五件套并行不互斥**（同一句可同时是
> 关系与流程步骤）。steps 的 `kind` 判不出用 `TASK`；flows 的 `type` 判不出
> 用 `SEQUENCE`；from/to 用整数下标引用 steps，不要自造 ID。
> 含**审批/审核/复核/会签/批准**步骤的流程 → `flow_type="APPROVAL"`；
> 审批步骤的结果（通过/否决/退回）标 `approval_outcome`，退回（RETURN）
> 必须给 `reject_to` 指向被退回修改的步骤下标。
>
> **规则四层分级**（写进提示词，无代码规则）：
> 规则按生效位置**逐层下沉**，能下沉就不堆进 `rules[]`：L1 单属性取值约束
> → `attributes[].required`/`value_type`；L2 实体间引用/关联约束 →
> `relations[]`；L3 跨属性不变量/整体一致性 → `constraints[]`；L4 道义动词
> 或条件-动作结构且不属于 L1-L3 → `rules[]`（完整条件-动作+模态）。
>
> **预测决策六件套判定原则**（写进提示词，无代码规则）：
> - **states**：对象存在**离散状态**（如风险承受能力等级 C1-C5、投资者类别
>   普通/专业、流程阶段）且状态间存在**迁移关系**（评估变化/申请转化）→ 抽为
>   状态机。与 processes 分界：processes 是执行步骤序列，states 是对象生命周期
>   状态拓扑。
> - **functions**：业务文档中出现**可量化的经验/阈值/定义式**（如「回访率 ≥
>   10%」「匹配原则」）→ 抽为函数指标。与 attributes 分界：attributes 存**字面量
>   值**（注册资本=10亿元），functions 存**计算定义**（怎么算出来）。
> - **temporal**：出现**时间限定**（生效/实施日期、频率「每年」、时间窗「六个月内」、
>   触发时点）且绑定到某主体 → 抽为时态。与 DATE 实体分界：DATE 是图中节点，
>   temporal 是把时间语义绑定到主体。
> - **actions**：出现**处置动作及其级别/代价/触发**（书面警示、暂停交易、惩戒）→
>   抽为动作处置。与 rules 分界：rules 是"应当/不得"规范，actions 是具体处置
>   元信息（怎么做/什么级别/何时触发）。
> - **constraints**：出现**结构约束**（至少划分为五级、不低于 10%、应当包含、
>   不得并存）→ 抽为约束。与 rules 分界：rules 是"该做什么"的规范，constraints
>   是"什么合法"的结构约束。
> - **permissions**：出现**谁能/不能执行什么动作**（普通投资者可以申请转化、
>   专业投资者可以购买所有风险等级）→ 抽为授权。与 rules 的 PERMISSION 模态
>   分界：授权聚焦 actor-action-effect 三元 + 范围，规则保留完整条件-动作结构。
> - 六件套与五件套并行不互斥（同一句可同时是关系与状态迁移）；六件套均**不入
>   实体-边图**，`metadata` 只登记各自 count。states 的 transitions 用整数下标
>   引用 states，不要自造 ID。

## 惰性锚点（消费层 build_kg 兜底，不用 LLM 操心）

| 锚点 | 规则 | 位置 |
|---|---|---|
| ID | `sha256(type, canonical)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| span | surface/evidence 尽力 `find`，找不到给 -1 / None | 薄壳 + `smini ids` |
| object_literal | 由 `object_type` 派生（数值类 → true），消费层强制修正 | `build_kg.py` |
| value_type | 属性值类型：薄壳校验，非法 → `OTHER` 兜底 | 薄壳 |
| 属性双落位 | 字面量边为**权威事实**（带 evidence/双时态）；`Entity.properties[name]=value` 仅聚合视图 | `build_kg.py` |
| rule_id | `sha256(subject, condition, action, modality)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| modality | 规则模态：薄壳校验，非法 → `OTHER` 兜底 | 薄壳 |
| 规则入图 | **不入实体-边图**；仅 `metadata.rule_count` 登记 | `build_kg.py` |
| process_id / step_id / flow_id | 由 `name` / `(process, index, label)` / `(process, from, to, type, condition)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| kind / flow.type | 步骤类型 / 控制流类型：薄壳校验，非法 → `TASK` / `SEQUENCE` 兜底 | 薄壳 |
| flow_type / approval_outcome | 流程类型 / 审批结果：薄壳校验，非法 → `COLLABORATION` / `OTHER` 兜底 | 薄壳 |
| reject_to | 驳回目标：下标越界（<0 或 ≥len(steps)）→ **置 None**（同 flow 下标处理） | 薄壳 |
| flow 下标 | from/to 越界（<0 或 ≥len(steps)）→ **丢弃该 flow**（LLM 下标不可信） | 薄壳 |
| 流程入图 | **不入实体-边图**；仅 `metadata.process_count` 登记 | `build_kg.py` |
| state_machine_id | `sha256(object, name)` 内容寻址，跨进程幂等 | `smini/ids.py` |
| state_id / transition_id | 由 `(sm_id, 下标, label)` / `(sm_id, from, to, event, condition)` 内容寻址 | `smini/ids.py` |
| transition 下标 | from/to 越界（<0 或 ≥len(states)）→ **丢弃该迁移**（LLM 下标不可信） | 薄壳 |
| function_id | `sha256(name, subject, formula)` 内容寻址 | `smini/ids.py` |
| output_type | 输出类型：薄壳校验，非法 → `OTHER` 兜底 | 薄壳 |
| temporal_id | `sha256(subject, kind, value)` 内容寻址 | `smini/ids.py` |
| kind（temporal） | 时态类型：薄壳校验，非法 → `VALIDITY` 兜底 | 薄壳 |
| action_id | `sha256(name, actor, level, trigger)` 内容寻址 | `smini/ids.py` |
| level | 动作级别：薄壳校验，非法 → `OBSERVE` 兜底 | 薄壳 |
| constraint_id | `sha256(subject, type, description)` 内容寻址 | `smini/ids.py` |
| type（constraints） | 约束类型：薄壳校验，非法 → `CONSISTENCY` 兜底 | 薄壳 |
| permission_id | `sha256(actor, action, effect)` 内容寻址 | `smini/ids.py` |
| effect | 授权效果：薄壳校验，非法 → `DENY` 兜底（最小权限） | 薄壳 |
| data_scope | 数据可见范围：薄壳校验，非法 → `OTHER` 兜底；不参与寻址 | 薄壳 |
| prop_kind / strength | 关系传导：薄壳校验，非法/缺失 → 空（普通事实 FACTUAL） | 薄壳 |
| rule_type / certainty / actor_type | 规则用途 / 确定性 / 主体类型：薄壳校验，非法 → `OTHER` 兜底 | 薄壳 |
| output_type（rules） | 规则输出形态：薄壳校验，非法 → `OTHER`；空 → 空串（未标注，schema 允许） | 薄壳 |
| required（attributes） | 必录性：布尔/字符串清洗，兜底 `False` | 薄壳 |
| 软引用 | `reused_by`/`sub_process_ref`/`actor`/`role` 找不到引用 → **warning 不阻断**（读端引用宽松语义） | `smini validate` |
| document_meta | `source` 强制取输入文档 URI；domain/version/doc_type 宿主可选交卷 | 薄壳 |
| 六件套入图 | **不入实体-边图**；仅 `metadata.state_count / function_count / temporal_count / action_count / constraint_count / permission_count` 登记；`prop_kind`/`strength` 作为边属性入图 | `build_kg.py` |
| 去重 | 同 object+name 即同状态机，按 sm_id 合并；同 (name,subject,formula) 即同函数，按 function_id 合并；同 (subject,kind,value) 即同时态；同 (name,actor,level,trigger) 即同动作；同 (subject,type,description) 即同约束；同 (actor,action,effect) 即同授权 | `build_kg.py` / 薄壳 |
| 去重 | 同 canonical 即同实体，按 ID 合并；同 (subject,condition,action,modality) 即同规则，按 rule_id 合并；同 name 即同流程，按 process_id 合并 | `build_kg.py` / 薄壳 |

## Workflow（声明式）

### 1. 读输入（归一化文本由宿主 agent 提供）
本 skill **不负责摄入与归一化**源文件，也**不调用 `smini-ingest`**——摄入/归一化是已有大量成熟方案的环节，直接外包给宿主 agent：由宿主（如 WorkBuddy）自行寻找合适的工具或技能将源文件摄入并归一化为干净文本，产出 `runs/<run_id>/03-normalized.json`（`NormalizedDocument`）。本步仅**读取该已归一化文本**并取 `text` 送后续抽取。

### 2. 实体 + 关系 + 属性 + 规则 + 流程 + 六件套抽取（按契约一次交卷）
对每篇文档，按上述契约输出
`entities[]` + `relations[]` + `attributes[]` + `rules[]` + `processes[]` +
`states[]` + `functions[]` + `temporal[]` + `actions[]` + `constraints[]` +
`permissions[]`（这是宿主与 Python 的唯一接缝：`LLMProvider.extract` /
`HostAgentLLMProvider.inject`）。
**语义全部按契约交卷**：伪实体（「是一家人工智能公司」）、句式识别、
实体类型判断、实体/属性分界、规则判定、流程判定、六件套判定都不需要代码
规则——提示词里用原则约束：
> 实体必须是专有名词性成分；不要抽句子片段或指代词（如「该公司」）。
> 宾语是独立实体 → relations；宾语是字面量 → attributes。
> 含道义动词或条件-动作结构 → rules。
> 含顺序/阶段/流程性描述 → processes（steps + flows）。
> 对象有离散状态且存在迁移 → states；有可量化经验/阈值/定义式 → functions；
> 有时间限定绑定主体 → temporal；有处置动作级别/代价/触发 → actions；
> 有结构约束 → constraints；有谁能/不能做什么 → permissions。

宿主交卷方式二选一：
- **库调用**：`HostAgentLLMProvider().inject(contract)` → `ExtractStep`
- **CLI**：`python -m smini.cli build <src> --host-contract <contract.json> --extract-out runs/<run_id>/04-extraction.json`
  （`--extract-out` 把抽取结果落盘为单篇 `04-extraction.json`；多篇文档时仅导出第一篇，其余用 `--json state.json` 导出后取 `extractions[]`）

> 契约模板：`references/host-contract.example.json`（基于真实监管文本的完整示例，
> 含五件套，完全符合 A 清单）。宿主产出契约后，用该模板对照检查结构即可。

### 3. 薄壳映射 + 落盘
- Python 把契约 dict 映射为 `ExtractionResult`（`smini/steps/extract.py`，
  零业务判定）：
  - `mentions[]`：span 尽力 `find`；`mention_id` 由 sha256 算
  - `relations[]`：`evidence_text` 存原文引用，`evidence_span` 尽力定位
  - `triplets[]`：`object_literal` 由 `object_type` 派生，`triplet_id` 由 sha256 算
  - `attributes[]` → 属性三元组（`extractor="llm.attr.v1"`，`object_literal=True`，
    `value_type` 独立枚举，非法兜底 `OTHER`）；属性所属实体按 canonical 反查，
    匹配不到则合成 mention（兜底 `OTHER` 实体，不丢属性）
  - `rules[]` → `Rule`（`extractor="llm.rule.v1"`，`modality` 独立枚举，非法兜底
    `OTHER`；`rule_id` 由 sha256 内容寻址；action 缺失跳过；按 rule_id 去重）
  - `processes[]` → `Process`（`extractor="llm.proc.v1"`；`step_id`/`flow_id` 由
    sha256 内容寻址；`kind` 非法兜底 `TASK`、`flow.type` 非法兜底 `SEQUENCE`；
    flows 下标越界**丢弃**；按 process_id 去重）
  - `states[]` → `StateMachine`（`extractor="llm.sm.v1"`；`sm_id`/`state_id`/
    `transition_id` 由 sha256 内容寻址；transitions 下标越界**丢弃**；按 sm_id 去重）
  - `functions[]` → `Function`（`extractor="llm.fn.v1"`；`function_id` 由 sha256
    内容寻址；`output_type` 独立枚举，非法兜底 `OTHER`；按 function_id 去重）
  - `temporal[]` → `Temporal`（`extractor="llm.tp.v1"`；`temporal_id` 由 sha256
    内容寻址；`kind` 独立枚举，非法兜底 `VALIDITY`；按 temporal_id 去重）
  - `actions[]` → `Action`（`extractor="llm.ac.v1"`；`action_id` 由 sha256
    内容寻址；`level` 独立枚举，非法兜底 `OBSERVE`；按 action_id 去重）
  - `constraints[]` → `Constraint`（`extractor="llm.cn.v1"`；`constraint_id` 由
    sha256 内容寻址；`type` 独立枚举，非法兜底 `CONSISTENCY`；按 constraint_id 去重）
  - `permissions[]` → `Permission`（`extractor="llm.pm.v1"`；`permission_id` 由
    sha256 内容寻址；`effect` 独立枚举，非法兜底 `DENY`；按 permission_id 去重）
  - `relations[].prop_kind/strength` 透传到 `Relation` 与 `Triplet`（薄壳校验，
    非法/缺失 → 空 = 普通事实 FACTUAL）
  - 字段透传（薄壳校验 + 非法兜底，不参与 ID 寻址）：
    `rules[].rule_type/output_type/reused_by/certainty`、
    `processes[].preconditions/postconditions/flow_type/approval_chain`、
    `steps[].sub_process_ref/lane/reject_to/approval_outcome`、
    `flows[].on_reject`、
    `actions[].precondition/postcondition`、`permissions[].role/actor_type/data_scope`、
    `attributes[].required`（bool 化）；`document_meta.source` 强制取输入 URI
- 写 `runs/<run_id>/04-extraction.json`，遵循 `contracts/extraction.schema.json`。
  **落盘主体（两条路径，任选其一）**：
  - **库调用路径**：宿主 `inject(contract)` 后由**宿主 agent 自己写文件**（薄壳 `ExtractStep`
    已在内存产出 `ExtractionResult`，宿主 `to_dict` 后落盘）；
  - **CLI 路径**：`build … --extract-out runs/<run_id>/04-extraction.json` 由 CLI 落盘
    （与第 2 步的 CLI 交卷方式配套）；也可用 `--json state.json` 导出后手动取
    `extractions[0]`。

### 4. 调 Python 校验（可选）

```bash
python -m smini.cli ids extract --in runs/<run_id>/04-extraction.json
python -m smini.cli validate extract --in runs/<run_id>/04-extraction.json
```

- `ids extract`：把所有空 ID 用 sha256 填上（含 rule_id / process_id / step_id /
  flow_id / sm_id / state_id / transition_id / function_id / temporal_id /
  action_id / constraint_id / permission_id，幂等根本保证）
- `validate extract`：Schema 校验 + 类型/模态/步骤类型/控制流类型/输出类型/
  时态类型/动作级别/约束类型/授权效果/传导类型枚举校验 + span 越界校验
  （-1 合法）+ flow 下标越界校验 + transition 下标越界校验 +
  **软引用检查**（`reused_by`/`sub_process_ref`/`actor`/`role` 找不到引用
  只打 ⚠ warning，不阻断）

### 5. 渲染 HTML 版（每次生成 JSON 同步产出）

契约 JSON / `04-extraction.json` 落盘后，用配套脚本渲染一个**自包含 HTML 查看器**
（统计卡片 + 类型分布 + 关系图谱（**力导向布局 + 拖拽/缩放/平移 + 点击聚焦邻域
+ 按子图拆分 + 谓词着色/平行边曲率/类型与谓词图例**）+ 可搜索实体表 +
关系表（含传导类型/强度两列）+
**属性清单表** + **规则清单表** + **流程清单表** + **状态机**（SVG 状态图 + 状态/迁移表）+
**指标函数表** + **时态表** + **动作处置表** + **约束表** + **授权表**，
零外部依赖，浏览器直接打开）：

```bash
python <skill>/scripts/render_viewer.py --in <contract.json> --out <contract.html> [--title "…"]
```

- 输入支持契约格式 `{entities, relations, attributes, rules, processes}`，也兼容
  `04-extraction.json`（`mentions/triplets/rules/processes`，属性从 `value_type` 非空的
  三元组自动归入属性清单）
- 缺省输出 = 输入路径换 `.html`；HTML 内嵌数据，离线可用

### 6. 失败处理

| 情况 | 动作 |
|---|---|
| 宿主未交卷（未 `inject` 契约 / 未传 `--host-contract`） | 薄壳返回空结果 + 登记 `Degradation(kind=no_credential)`，`stats.mode="empty"`——零规则架构无确定性兜底，不造假 |
| 宿主交卷但调用失败 | 同上空结果 + `Degradation(kind=unavailable)` |
| 某文档抽取为空 | 正常（可能是无实体的段落），不报错 |

> 注意：**不存在确定性 fallback**。`smini fallback extract` 已删除——需要
> 抽取就必须由**宿主 agent 交卷**或走 Skill。

## 输出契约

`runs/<run_id>/04-extraction.json` → `ExtractionResult`
字段：`doc_id` / `chunks[]` / `mentions[]` / `relations[]` / `triplets[]` / `rules[]` / `processes[]` / `states[]` / `functions[]` / `temporal[]` / `actions[]` / `constraints[]` / `permissions[]` / `degradations[]` / `stats`
（`stats.mode` 标记 `llm` 或 `empty`，供编排层统计；`stats.attributes` 标记属性三元组数；
`stats.rules` 标记规则数；`stats.processes` 标记流程数；`stats.states/functions/temporal/actions/constraints/permissions` 标记六件套数。
规则、流程与六件套均为独立产出，不入实体-边图，`build_kg` 在 metadata 登记
`rule_count` / `process_count` / `state_count` / `function_count` / `temporal_count` /
`action_count` / `constraint_count` / `permission_count`；`prop_kind`/`strength`
作为边属性入图）
**同时产出同名 `.html`**（`scripts/render_viewer.py` 渲染的自包含查看器，含 12 个 Tab），供人直接浏览。

## 自检清单（交付前逐条过）

### A. 宿主产出契约（交卷前，宿主 agent 自检）

- [ ] 契约结构 = `entities:[{surface,canonical,type}]` + `relations:[{subject,predicate,object,object_type,evidence}]` + `attributes:[{entity,name,value,value_type,evidence}]` + `rules:[{subject,condition,action,modality,evidence}]` + `processes:[{name,steps,flows}]`，JSON 合法可解析
- [ ] 每个 `surface`、每条 `evidence` **逐字取自原文**（用于溯源，不改写、不转述、不概括）
- [ ] `canonical` 是实体唯一身份：同一实体跨 mention / 跨文档用同一 canonical（「特斯拉」「Tesla」→「特斯拉」），不每处自由发挥
- [ ] 所有 `type` / `object_type` 都在 19 类枚举内（12 通用 + 7 金融扩展）；判不出用 `OTHER`，不自创类型
- [ ] 宾语是日期/金额/百分比/数量时，`object_type` 已标 `DATE/MONEY/PERCENT/QUANTITY`（literal 由它派生）
- [ ] 宾语是另一实体时，`object_type` 标该实体类型；给不出可省略
- [ ] 每条 relation 的 `subject` / `object` 与 `entities` 中某条的 `surface` / `canonical` 一致（literal 宾语除外）
- [ ] 每条 attribute 的 `entity` 与 `entities` 中某条的 `canonical` 一致；`name` 是归一化属性名；`value` **逐字取自原文**
- [ ] 每个 attribute 的 `value_type` 都在 10 类独立枚举内（`DATE TIME MONEY PERCENT QUANTITY NUMBER BOOLEAN ENUM STRING OTHER`）；判不出用 `OTHER`，不自创类型
- [ ] **实体 vs 属性分界正确**：宾语是独立实体 → relations；宾语是字面量 → attributes（唯一硬分界）
- [ ] **规则判定正确**：含道义动词（应当/必须/不得/禁止/可以/有权）或条件-动作结构（如果/当…时…则…）→ rules；纯事实陈述 → 保持 relations/attributes，不进 rules
- [ ] **规则四层分级正确**：单属性取值约束 → `attributes.required`/`value_type`；实体间引用约束 → `relations`；跨属性不变量 → `constraints`；仅 L4 道义/条件-动作 → `rules`（能下沉不堆进 rules[]，防规则集膨胀）
- [ ] 每条 rule 的 `action` 必有；`modality` 在 5 类独立枚举内（`OBLIGATION PROHIBITION PERMISSION CONDITIONAL OTHER`）；判不出用 `OTHER`，不自创类型
- [ ] **不抽非规则段落**：定义、标题、序言、交叉引用表等不进 rules（防规则集膨胀）
- [ ] **流程判定正确**：含顺序/阶段/流程性描述（先/再/然后/最后、流程/步骤/生命周期）→ processes；纯事实（relations/attributes）与纯规范（rules）不因流程抽取而重复（五件套并行不互斥）；含审批/审核/复核/会签步骤 → `flow_type="APPROVAL"`
- [ ] 每个 process 的 `steps` 有 `label`（做什么）；`kind` 在 6 类独立枚举内（`TASK GATEWAY EVENT START END SYSTEM_TASK`），判不出用 `TASK`，不自创类型
- [ ] 每个 flow 的 `from`/`to` 是 steps 数组整数下标（0-based）；`type` 在 4 类独立枚举内（`SEQUENCE PARALLEL CONDITIONAL LOOP`），判不出用 `SEQUENCE`，不自创类型；CONDITIONAL 分支带 `condition`
- [ ] **审批流增强字段（可选，给出即正确）**：`approval_outcome` 在 4 类枚举内（`APPROVE REJECT RETURN OTHER`）；`RETURN` 必有 `reject_to`（整数下标）；`REJECT` 通常无 `reject_to`；`lane`/`approval_chain`/`on_reject` 用自然语言/名称，不编造
- [ ] **状态机判定正确**：对象有离散状态且存在迁移（评估变化/申请转化）→ states；与 processes 分界（执行步骤序列 vs 生命周期拓扑）正确
- [ ] 每个 state 有 `label`；`initial` 标记初始态；每个 transition 的 `from`/`to` 是 states 数组整数下标（0-based）；`event`/`condition`/`action` 全可空
- [ ] **函数指标判定正确**：可量化经验/阈值/定义式 → functions；与 attributes 分界（字面量值 vs 计算定义）正确；`formula` 用声明式自然语言/运算符描述，不强制可执行数学表达式
- [ ] 每个 function 的 `output_type` 在 7 类独立枚举内（`NUMBER PERCENT RANK BOOLEAN ENUM STRING OTHER`）；判不出用 `OTHER`，不自创类型
- [ ] **时态判定正确**：时间限定（生效/频率/时间窗/触发时点）绑定主体 → temporal；与 DATE 实体分界（图中节点 vs 绑定主体的时间语义）正确
- [ ] 每个 temporal 的 `kind` 在 4 类独立枚举内（`VALIDITY FREQUENCY WINDOW TRIGGER`）；判不出用 `VALIDITY`，不自创类型
- [ ] **动作处置判定正确**：处置动作级别/代价/触发 → actions；与 rules 分界（规范 vs 处置元信息）正确
- [ ] 每个 action 的 `level` 在 3 类独立枚举内（`OBSERVE WARN INTERVENE`）；判不出用 `OBSERVE`，不自创类型
- [ ] **约束判定正确**：结构约束（至少划分为五级/不低于/应当包含/不得并存）→ constraints；与 rules 分界（该做什么 vs 什么合法）正确
- [ ] 每个 constraint 的 `type` 在 6 类独立枚举内（`CARDINALITY VALUE_RANGE ENUM DISJOINT REQUIRED CONSISTENCY`）；判不出用 `CONSISTENCY`，不自创类型
- [ ] **授权判定正确**：谁能/不能执行什么动作 → permissions；与 rules 的 PERMISSION 模态分界（actor-action-effect+范围 vs 完整条件-动作结构）正确
- [ ] 每个 permission 的 `effect` 在 2 类独立枚举内（`PERMIT DENY`）；判不出用 `DENY`（最小权限），不自创类型；`data_scope` 在 5 类枚举内（`ALL OWN DEPT CUSTOM OTHER`），判不出用 `OTHER`
- [ ] relations 的 `prop_kind` 在 4 类独立枚举内（`FACTUAL DEPENDENCY CAUSAL TRIGGER`）；普通事实省略即 FACTUAL，不自创类型
- [ ] **借鉴字段（可选，给出即正确）**：rules 的 `rule_type` 在 4 类枚举内（`VALIDATION DERIVATION TRIGGER OTHER`）、`certainty` 在 3 类枚举内（`GENERAL ENTERPRISE OTHER`）、`output_type` 复用函数输出 7 类枚举（判不出省略）、`reused_by` 是字符串数组；attributes 的 `required` 是布尔；steps 的 `kind` 可用 `START/END/SYSTEM_TASK`；permissions 的 `actor_type` 在 3 类枚举内（`HUMAN SYSTEM OTHER`）、`data_scope` 在 5 类枚举内（`ALL OWN DEPT CUSTOM OTHER`）；流程 `flow_type` 在 2 类枚举内（`COLLABORATION APPROVAL`）、步骤 `approval_outcome` 在 4 类枚举内（`APPROVE REJECT RETURN OTHER`，RETURN 必有 `reject_to`）；流程步骤的 `sub_process_ref`/`lane`、流程 `preconditions/postconditions`/`approval_chain`、流 `on_reject`、动作 `precondition/postcondition`、授权 `role` 均用自然语言/名称，不编造
- [ ] **document_meta（可选）**：`source` 由 Python 强制取输入文档 URI，宿主只需给 `domain/version/doc_type`
- [ ] 六件套均**不入实体-边图**；不输出偏移 / ID / `object_literal`（由 Python 惰性锚点补算）
- [ ] 实体是专有名词性成分，不抽句子片段或指代词（如「该公司」）
- [ ] JSON 落盘后已调用 `scripts/render_viewer.py` 生成同名 `.html`（供人直接浏览）

### B. Python 交付前

- [ ] 所有 `mention_id` / `relation_id` / `triplet_id` / `rule_id` / `process_id` / `step_id` / `flow_id` 已由 Python 补算（非自造哈希）
- [ ] 所有 `entity_type` / `object_type` 都在 19 类枚举内（12 通用 + 7 金融扩展）
- [ ] 同一实体跨 mention 的 `normalized`（canonical）一致——身份不漂移
- [ ] 日期/金额类宾语的 `object_type` 已标 `DATE/MONEY/PERCENT/QUANTITY`（literal 由它派生）
- [ ] 每条 relation 有 `evidence` 原文引用（找不到 span 时 `evidence_span` 为 null 可接受）
- [ ] 属性三元组：`object_literal=True`、`value_type` 在 10 类枚举内（非法已兜底 `OTHER`）、`extractor="llm.attr.v1"`、`triplet_id` 已补算
- [ ] 规则：`modality` 在 5 类枚举内（非法已兜底 `OTHER`）、`extractor="llm.rule.v1"`、`rule_id` 已补算、action 必有
- [ ] 规则未进入实体-边图谱（`build_kg` metadata 已登记 `rule_count`）
- [ ] 流程：`kind` 在 6 类枚举内（非法已兜底 `TASK`）、`flow.type` 在 4 类枚举内（非法已兜底 `SEQUENCE`）、`flow_type` 在 2 类枚举内（非法已兜底 `COLLABORATION`）、`approval_outcome` 在 4 类枚举内（非法已兜底 `OTHER`）、`reject_to` 越界已置 None、`extractor="llm.proc.v1"`、`process_id`/`step_id`/`flow_id` 已补算、steps 有 label
- [ ] 流程 flow 下标越界已丢弃；流程未进入实体-边图谱（`build_kg` metadata 已登记 `process_count`）
- [ ] 状态机：`extractor="llm.sm.v1"`、`sm_id`/`state_id`/`transition_id` 已补算、states 有 label、transitions 下标越界已丢弃
- [ ] 函数指标：`extractor="llm.fn.v1"`、`output_type` 在 7 类枚举内（非法已兜底 `OTHER`）、`function_id` 已补算
- [ ] 时态：`extractor="llm.tp.v1"`、`kind` 在 4 类枚举内（非法已兜底 `VALIDITY`）、`temporal_id` 已补算
- [ ] 动作处置：`extractor="llm.ac.v1"`、`level` 在 3 类枚举内（非法已兜底 `OBSERVE`）、`action_id` 已补算
- [ ] 约束：`extractor="llm.cn.v1"`、`type` 在 6 类枚举内（非法已兜底 `CONSISTENCY`）、`constraint_id` 已补算
- [ ] 授权：`extractor="llm.pm.v1"`、`effect` 在 2 类枚举内（非法已兜底 `DENY`）、`data_scope` 在 5 类枚举内（非法已兜底 `OTHER`）、`permission_id` 已补算
- [ ] 关系传导：`prop_kind` 在 4 类枚举内（非法/缺失已兜底空 = 普通事实）、`strength` 透传；传导字段已随边入图
- [ ] 字段：`rule_type`/`certainty`/`actor_type`/`data_scope`/`flow_type`/`approval_outcome` 非法已兜底（`OTHER`/`COLLABORATION`）、rules `output_type` 非法已兜底 `OTHER`、attributes `required` 已 bool 化、`reject_to` 越界已置 None、`sub_process_ref`/`role`/`lane`/`approval_chain`/`on_reject`/`preconditions`/`postconditions`/`precondition`/`postcondition` 已透传
- [ ] `document_meta.source` 已取输入文档 URI（非空）
- [ ] `validate extract` 已跑：软引用（reused_by/sub_process_ref/actor/role）若有未匹配，已出现 ⚠ warning 且不阻断
- [ ] 六件套未进入实体-边图谱（`build_kg` metadata 已登记六件套各自 count）
- [ ] 每个 mention 的 span 要么精确切出 `text`，要么 `char_start=-1`（未定位，已兜底）
- [ ] 若走了空降级，`degradations[]` 非空且说明了影响
