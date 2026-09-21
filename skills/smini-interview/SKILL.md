---
name: semantica-interview
description: "以多轮访谈（对话）为信息源，构建/完善本体（ontology）模型——当没有文档或文档不全时，通过向用户提问收集信息，产出与文档抽取完全同构的宿主契约（十一件事：entities+relations+attributes+rules+processes+states+functions+temporal+actions+constraints+permissions）。「项目（project）」为持久化一等单元：访谈强制挂靠项目，会话永远从项目累计模型起步、收尾后归并回项目，项目建立后随时可追加访谈。契约即规则架构：宿主（豆包）充当 LLM，负责提问/判定/增量交卷/收尾判定；薄壳 Python 只做项目生命周期、会话落盘、delta 合并去重、归并与渲染。Use when the user says 开始访谈/访谈建模/补充访谈/再访谈一下/没有文档/通过对话了解/项目访谈/访谈 XX 项目/为 XX 项目访谈/继续上次访谈, or when 明确在提取或设计某个「项目」的本体模型且需要补充信息（可随时调起）."
---

# semantica-interview · 访谈式本体构建（信息源替换 + 项目归集）

> 把「多轮对话」变成与「文档抽取」同构的本体建模输入。
> 核心思想：**信息源替换** —— 文档驱动管线输入是归一化文档，访谈管线输入是
> 多轮对话累积的信息；最终产物是同一张宿主契约 JSON，走同一套
> validate / 渲染 / 图谱。
>
> **项目 = 持久化一等单元**：访谈**强制挂靠项目**，会话初始
> `model_draft` = 项目累计模型（consolidated），收尾后归并回项目——
> 这就是「项目建立之后随时可以被追加访谈」的落盘保证：
> 追加访谈不是从空开始，而是带着项目已有知识继续追问。
>
> 契约即规则：宿主 LLM 负责**提问、判定、增量交卷、收尾判定**；
> 薄壳 Python（`smini/steps/project.py` + `smini/steps/interview.py`）只做
> 项目生命周期、会话落盘/恢复、delta 合并去重、来源归并、渲染接线——
> **不生成问题、不判断覆盖度、不决定收尾、不解释语义**。

## 运行时要求（Python 版本）

- **Python ≥ 3.10**（与 smini-extract 相同；推荐 3.12）。
- 全项目零第三方依赖，`python -m smini.cli project|interview …` 直接可跑。
- 不需要配置 `SMINI_LLM_BASE_URL / SMINI_LLM_API_KEY / SMINI_LLM_MODEL`：
  宿主 agent（豆包）本身就是 LLM，Python 薄壳只做确定性工作。

## 契约（唯一约定：与 smini-extract 完全同构，不复制）

宿主每轮交卷 `delta` 与收尾落盘的 `model_draft` / `host-contract.json`，都是
**同一张宿主契约**（十一件事 + 可选 `document_meta`）：

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

字段含义、枚举、判定原则（实体 vs 属性硬分界 / 规则判定 / 流程判定 /
规则四层分级 / 预测决策六件套判定）**全部复用**：
- 完整字段表：`skills/smini-extract/SKILL.md` §契约（本项目内路径）
- 判定规则全集：`skills/smini-extract/references/extraction-rules.md`
- 契约完整示例：`skills/smini-extract/references/host-contract.example.json`

访谈与文档抽取的差异**只在信息来源**：
- 文档抽取：`surface` / `evidence` **逐字取自原文**；
- 访谈：`surface` / `evidence` **逐字取自用户原话**（用户怎么说就怎么记，
  不转述、不概括、不润色）；`canonical` 仍是规范名 = 实体唯一身份
  （用户两种说法指同一实体 → 归同一 canonical）。

## 项目层（projects/<项目名>/）

```
projects/
├── index.json                   # 项目注册表（project list 用）
└── <项目名>/                    # 项目名清洗：去掉 / : * ? " < > | 与连续破折号
    ├── project.json             # 项目状态：累计模型 + 来源清单 + 冲突
    ├── interviews/<会话id>/     # 每次访谈（session.json + host-contract + 04-extraction.json + html）
    ├── runs/<runid>/            # 项目模式文档抽取 / 混合模式材料档案：
    │                            #   materials/（材料原文）+ host-contract.json（抽取契约）
    │                            #   + 04-extraction.json + 04-extraction.html
    └── 04-extraction.html       # 项目累计模型总览渲染（consolidated 视图）
```

`project.json` 关键字段（薄壳维护，宿主只读）：
- `active_session`：每项目**最多一个**进行中的访谈（OPEN）；
- `sources[]`：来源清单（`{kind: interview|document, ref, status, updated_at}`），
  可审计、可从 sources 重建 consolidated（`rebuild`）；
- `consolidated`：**累计本体模型**（宿主契约格式，十一件事）；
- `covered`：累计计数（追加访谈时缺口 = 累计 covered 与完整性参考之差）；
- `conflicts[]`：跨来源冲突登记（`{anchor, between, issue, resolved}`），
  **不静默覆盖**，unresolved 冲突在后续访谈/抽取时优先让用户裁决。

**归并规则（§5.3，确定性）**：各类目按锚点键**并集**；同锚点 → 保留最新来源值
（时间戳靠后胜）+ 白名单语义字段差异**登记冲突**（unresolved）；`covered` 重算。

| 类目 | 锚点键 |
|---|---|
| entities | `canonical` |
| relations | `subject+predicate+object` |
| attributes | `entity+name` |
| rules | `subject+condition+action+modality` |
| processes | `name` |
| states / functions / temporal / actions / constraints / permissions | 各自既有锚点键（与 extract 一致） |

## 会话数据模型（interviews/<sid>/session.json）

```jsonc
{
  "session_id": "ivw_…",          // 薄壳生成
  "project": "适当性管理项目",      // 访谈强制挂靠项目
  "project_ref": "适当性管理项目",
  "domain": "金融/适当性",
  "goal": "",
  "status": "OPEN",               // OPEN | CLOSING | COMPLETE | ABORTED
  "turn": 3,
  "model_draft": { …十一件事 + document_meta… },   // 初始 = 项目累计模型
  "covered": { "entities": 5, … },                 // 每轮重算
  "agenda": [],                   // 宿主透传的待办主题（可选）
  "asked": ["…"],                 // 已问问题（R6 不重复）
  "history": [ {"role": "host|user", "text": "…", "delta": {…}} ],
  "conflicts": [],                // 会话内冲突（宿主标记）
  "meta": { "created_at": …, "updated_at": …, "source": "interview" }
}
```

## 宿主-薄壳接缝（每轮交卷，唯一接缝）

**薄壳给宿主**（`interview summary`）：会话摘要 = 项目名/领域 + 当前轮次 +
`covered` 计数 + 关键 `canonical` 清单 + 待办 `agenda` + 未决 `conflicts`
（项目级 + 会话内）+ 最近历史。

**宿主交卷**（append_turn 的 host 参数）：

```jsonc
{
  "delta": { …十一件事的**子集**，只含本轮新增/修改条目… },  // 可空（纯提问轮）
  "next_question": "投资者有哪些关键属性？",   // 空串/缺省 = 本轮不提问
  "closing": false,                          // true = 请求收尾（先给用户"还有要补充的吗"机会）
  "note": "已记录 2 个实体、1 条属性"          // 给用户看的进展（每轮先转述增量再提问）
}
```

薄壳只做：合并 delta（锚点并集/更新）→ 更新 covered/history/asked → 落盘
session.json → closing 时收尾接线（host-contract → ExtractStep → 渲染 → 归并项目）。

## 提问策略（声明式规则 R1-R6，写进宿主提示词）

### R1 缺口驱动（最高优先）
对照 `covered` 与完整性参考，**优先问缺口最大的维度**。完整性期望（声明式，
判断"是否适用"而非机械凑数）：

| 维度 | 最低期望（若领域适用） | 判断线索 |
|---|---|---|
| entities | ≥ 3（主体/角色/系统/客体） | 谁参与、被处理、被管理 |
| relations | ≥ 2 | 实体间的职责/依赖/控制/从属 |
| attributes | ≥ 2 | 实体有可量化属性（等级/金额/期限/状态） |
| rules | ≥ 2 | 有"应当/不得/如果…则"的业务规范 |
| processes | ≥ 1 | 有顺序执行的动作链 |
| 六件套 | 按需 | 有状态演化→states；有计算公式→functions；有时间限定→temporal；有处置动作→actions；有结构约束→constraints；有权限→permissions |

**追加访谈时**：缺口 = 项目累计 covered 与完整性参考之差；已覆盖主题不重复提问。

### R2 主题聚焦
一轮只推进**一个主题**（实体盘点 → 关系 → 属性 → 规则 → 流程 → 六件套），
不在一个问题里混多个主题；连续追问同一实体时给用户"可以只说不知道"的出口。

### R3 追问链
对用户新提到的关键实体，优先追问其**属性**（等级/金额/期限/状态）与**关系**
（与谁协作/由谁负责/受谁约束），而不是立刻跳到新主题。

### R4 确认式提问
对高风险结论（关键规则、审批链、授权范围）做**反查确认**：「我理解到……
是这样吗？」；发现 `conflicts`（会话内或项目内 unresolved）时，下一轮优先
让用户裁决。

### R5 领域话术
`domain=金融` 时，问题模板偏向：客户/产品/机构、适当性/风险/合规、审批链/
授权/数据范围、指标与公式（金融领域扩展枚举的触发词表复用
`extraction-rules.md` §12）。

### R6 问题卫生
- 一次一问，问题具体、可回答（不用"还有什么要补充的吗"这类开放到无效的问题）；
- 不重复 `asked` 中已问过的问题（除非为确认冲突）；
- 问题配一句"为什么问"（让用户理解意图，提高回答质量）。

## 收尾判定（声明式，满足任一即收尾）

| 条件 | 说明 |
|---|---|
| 用户显式结束 | 「结束 / 够了 / 先这样」 |
| 覆盖度达标 | 十一类中适用的维度已覆盖、无重大缺口、无未决冲突 |
| 议程耗尽 | `agenda` 已全部推进完毕 |
| 轮数保护 | 达到 **20 轮** 时提示「信息已较充分，可结束；若需继续可说明还想覆盖的方面」 |

收尾动作（薄壳，宿主**不需要重新生成完整契约**）：
`model_draft` + `document_meta.source="interview://<项目名>"` → 落盘
`host-contract.json` → 走既有薄壳（`HostAgentLLMProvider.inject` →
`ExtractStep`）→ `04-extraction.json` + 会话渲染 `04-extraction.html` →
**归并进项目 consolidated**（sources +1）→ 项目总览渲染 `04-extraction.html`。

## Workflow（声明式）

### 模式 1：新建项目 + 访谈（从零建模）
```
用户: 开始访谈「XX项目」/ 为「XX项目」访谈建模
宿主: 提取项目名 + 领域（用户没给就首问顺带收集）→ 项目不存在 → 薄壳 create
薄壳: 建会话（初始 model_draft = 空累计模型）→ 返回会话摘要
宿主: 首问（领域 + 建模目标 + 缺口盘点：R1）
用户: 回答1
宿主: 按接缝交卷 {delta, next_question, closing, note}
薄壳: 合并 delta → covered/history/落盘 → 显示 note
…循环…
宿主: 满足收尾判定 → closing=true（或用户说结束）
薄壳: finalize → host-contract + ExtractionResult + 两级 HTML + 归并项目
```

### 模式 2：追加访谈（项目已存在）
```
用户: 给「XX项目」再补充访谈一下
宿主: 读项目累计摘要（covered/canonical/agenda/未决冲突）
薄壳: active_session 无 OPEN（上次已 COMPLETE）→ 建新会话，
      初始 model_draft = 项目累计模型（核心：不是空起步）
宿主: 基于"已有什么"继续追问缺口（R1 追加版，不重复已覆盖主题）
…循环 → 收尾 → 归并（锚点键无重复 → 无冲突；有差异 → 登记冲突待裁决）
```

### 模式 3：中断恢复
```
会话 OPEN 未收尾 → 再次调起时提示：
「继续上次访谈（第 N 轮，待办主题：…）？还是开新会话？」
→ 继续 = 恢复原会话（薄壳 open_session 恢复）；开新会话 = 先把旧会话标记
  ABORTED（保留轨迹），再建新会话（--force-new / abort）
```

### 模式 4：混合模式（访谈中吸收文档片段）
用户访谈中粘贴文档片段（「这是相关制度，你看下」）→ 宿主把片段按文档增量
并入本轮 delta（作为"回答 + 文档"合并输入），`evidence` 标注来自片段原文。

**材料目录（推荐做法）**：用户把一整个材料目录放进项目（如
`projects/<项目名>/<材料目录>/`）→ 宿主读材料、把梳理出的增量交卷 delta 的
同时，**用 `--materials <材料目录>` 归档**（可多次传）：

```bash
python -m smini.cli interview append <项目名> --user "…" \
  --delta delta.json --materials "projects/<项目名>/<材料目录>" \
  --question "…" --note "…"
```

薄壳把材料原文复制到 `runs/<runid>/materials/`，本轮 delta 快照作为该文档来源
的抽取契约落盘 `runs/<runid>/host-contract.json`（+ 04-extraction.json + html），
并在 project.sources 登记 `kind="document"` 来源——**材料全文可审计、可从
sources 重建（rebuild）**，与访谈 interview 来源归并锚点并集、幂等安全。

### 模式 5：无项目语境
用户只说「开始访谈」（未给项目名）→ 宿主首问顺带收集项目名/领域；
**访谈强制挂靠项目**（没有项目就没有追加落点）。

## 失败处理

| 情况 | 动作 |
|---|---|
| 项目不存在且用户未给项目名 | 首问收集项目名/领域，不凭空造项目名 |
| 会话已 COMPLETE/ABORTED 又收到 append | 薄壳报错，宿主提示开新会话（追加访谈） |
| 用户回答与已有模型矛盾 | 登记冲突（R4 下轮反查裁决），不静默覆盖 |
| 20 轮保护触发 | 提示信息已充分，给用户继续/结束选择 |
| 渲染脚本缺失 | 数据产物（host-contract / 04-extraction.json）照常落盘，渲染跳过不阻断 |

## 输出契约

每次访谈收尾（`projects/<项目名>/interviews/<sid>/`）：
- `session.json` — 会话轨迹（含 model_draft 全部中间态，可审计）
- `host-contract.json` — 宿主契约（十一件事，`source="interview://<项目名>"`）
- `04-extraction.json` — 薄壳映射后的 ExtractionResult
- `04-extraction.html` — 会话渲染（render_viewer 复用）

项目级（`projects/<项目名>/`）：
- `project.json` — 项目状态（consolidated + sources + conflicts）
- `04-extraction.html` — **项目累计模型总览**（consolidated 视图）

**每次生成 JSON 同步产出 HTML**（render_viewer.py），供人直接浏览。

## 自检清单（交付前逐条过）

### A. 宿主交卷（每轮 append 前自检）

- [ ] 交卷结构 = `{delta?, next_question?, closing?, note?}`，JSON 合法可解析
- [ ] `delta` 只含**本轮新增/修改**条目（不是把累计模型整份重交）；
      十一件事键都在契约内；无偏移、无 ID、无 object_literal（薄壳惰性补算）
- [ ] 每条 `surface` / `evidence` **逐字取自用户原话**（不转述、不概括、不润色）
- [ ] `canonical` 是实体唯一身份：与项目已有 `canonical` 一致时不新建条目
      （同一实体 → 同 canonical，不每处自由发挥）
- [ ] 所有 `type` / `object_type` / `value_type` / `modality` 等在既有枚举内；
      判不出用兜底值（`OTHER` / `TASK` / `SEQUENCE` / `DENY` 等），不自创类型
- [ ] 实体 vs 属性硬分界正确：用户提到的是独立实体 → relations；是字面量
      （等级/金额/期限/状态值）→ attributes
- [ ] 规则判定正确：用户说了"应当/必须/不得/如果…则" → rules；纯事实 → 不进 rules
- [ ] 流程判定正确：用户描述了顺序/阶段/审批步骤 → processes（steps + flows，
      from/to 用整数下标）
- [ ] 六件套判定正确（按需）：有状态演化→states；有计算公式→functions；
      有时间限定→temporal；有处置动作→actions；有结构约束→constraints；
      有谁能/不能做什么→permissions
- [ ] `next_question` 满足 R1-R6：一次一问、不重复 `asked`、附"为什么问"、
      优先缺口最大维度、对高风险结论做 R4 确认式反查
- [ ] 发现与已有模型矛盾时：本轮 delta 里标出，且 `next_question` 优先让用户裁决
- [ ] `closing=true` 前：已给用户一次"还有要补充的吗"的机会，且满足收尾判定
      （用户显式结束 / 覆盖度达标 / 议程耗尽 / 20 轮保护）
- [ ] `note` 每轮都有（先转述增量再提问，让用户看到进展）
- [ ] **混合模式读了材料目录**：已用 `--materials <目录>` 归档到
      `runs/<runid>/materials/`（原文）+ delta 快照（host-contract/04-extraction/html），
      并在 project.sources 登记 `kind="document"` 来源（材料全文可审计、rebuild 可重建）

### B. 薄壳 Python 交付前（host-contract / ExtractionResult）

- [ ] 会话初始 `model_draft` = 项目累计模型（追加访谈不是空起步）
- [ ] delta 已按锚点键合并（并集 / 同锚点更新），covered 已重算
- [ ] `host-contract.json` 的 `document_meta.source` = `interview://<项目名>`（非空）
- [ ] 收尾已走 `ExtractStep`（ID/span/object_literal 惰性补算，与文档抽取同一薄壳）
- [ ] 会话产物四件套齐全：session.json / host-contract.json / 04-extraction.json /
      04-extraction.html
- [ ] 已归并进项目：sources +1、consolidated 更新、covered 重算、无静默覆盖
      （白名单语义差异已登记 conflicts）
- [ ] 项目总览 `04-extraction.html` 已生成（consolidated 视图）
- [ ] `validate extract` 已跑（可选）：`python -m smini.cli validate extract
      --in projects/<项目名>/interviews/<sid>/04-extraction.json`
