# smini-extract 版本迭代记录

> 本文件仅记录 smini-extract 各版本的迭代来历与新增能力，供维护者追溯。
> **执行 skill 不需要阅读本文件**——契约、字段含义、判定原则等执行所需内容
> 均在本 skill 的 `SKILL.md` 与 `references/extraction-rules.md` 中（已去掉版本标注）。

## v2 · 属性抽取
- 新增 `attributes[]` 通道，抽取实体的字面量数据属性（本体论 DatatypeProperty）。
- 与关系（ObjectProperty）的唯一硬分界：宾语是独立实体 → `relations`；宾语是字面量 → `attributes`。

## v3 · 业务规则抽取
- 新增 `rules[]` 通道，抽取业务规则（条件-动作/结论 + 道义模态）。
- 对齐 SWRL 的 Body/IF → Head/THEN 骨架 + 道义模态（`OBLIGATION`/`PROHIBITION`/`PERMISSION`/`CONDITIONAL`/`OTHER`）。
- 规则是独立产出，不进入实体-边图谱。
- 判定原则：含道义动词（应当/必须/不得/禁止/可以/有权）或条件-动作结构（如果/当…时…则…）→ 抽为 `rules`；纯事实陈述不进 `rules`；不抽定义/标题/序言/交叉引用表等非规则段落。

## v4 · 业务流程抽取
- 新增 `processes[]` 通道，抽取业务流程（`steps` 步骤 + `flows` 控制流）。
- 本体论依据：流程是 DOLCE 的 perdurant（持续体）；PSL 用 Activity 作最小执行单元；BPMN 用 Process/Activity/Gateway/Actor 建模；Workflow Patterns 给出顺序/并行/排他/循环基本控制模式。
- 判定原则：含顺序/阶段/流程性描述（先/再/然后/最后、流程/步骤/生命周期）→ `processes`；纯事实与纯规范不因流程抽取而重复（五件套并行不互斥）。

## v5 · 金融领域扩展
- 实体类型扩展 7 类金融专用枚举：`FINANCIAL_INSTRUMENT` / `REGULATION` / `FINANCIAL_INDICATOR` / `MARKET` / `RISK` / `TRANS` / `SECURITY_CODE`（依据金融 NER 学术 taxonomy 与金融惯例）。

## v6 · 预测决策本体六件套
- 新增六件套通道：`states` / `functions` / `temporal` / `actions` / `constraints` / `permissions`，
  把业务文档从「描述事实的静态本体」升级为「可预测、可决策的动态本体」。
- `relations` 新增 `prop_kind`（传导类型：`FACTUAL`/`DEPENDENCY`/`CAUSAL`/`TRIGGER`）与 `strength`（强度）作为边属性入图。
- 六件套均为独立产出，不入实体-边图（是行为/时态/授权语义，非实体间事实）。
- 设计文档：`docs/预测决策本体增量设计方案.md`（7 决策点已确认）。

## v7 · 七模型字段级借鉴
- 借鉴 ontology-driven-dev 的 M1/M2/M3/M5/M6 模型，为已有通道增补字段（保持能力范围与实现机制不变）：
  - `rules` 增 `rule_type` / `output_type` / `reused_by` / `certainty`
  - `processes` 增 `preconditions` / `postconditions` 与 `steps[].sub_process_ref`；`StepKind` 增 `START` / `END` / `SYSTEM_TASK`
  - `actions` 增 `precondition` / `postcondition`
  - `permissions` 增 `role` / `actor_type`
  - `attributes` 增 `required` 与 `value_type` 的 `DICT_REF` / `ENTITY_REF`
  - 输出顶层增 `document_meta {domain, source, version, doc_type}`
  - `validate` 增软引用检查（`reused_by` / `sub_process_ref` / `actor` / `role` 找不到引用只 warning、不阻断）
- 明确不借鉴：simpleeval 可执行表达式、flow_engine / 代码生成、8 阶段人工硬暂停门禁、DDL/MU 界面。
- 设计文档：`docs/七模型借鉴增量设计方案.md`。

## 历史对照：v1 规则驱动 → 契约即规则
原版（v1）采用确定性规则驱动，新版（契约即规则）改为由抽取方按契约交卷、Python 仅做薄壳映射。主要变化：
- 实体识别：正则 NER + 黑名单 → 抽取方语义判断
- 关系识别：8 条正则模式 → 开放识别 + 谓词归一化（可选）
- 属性识别：无 → 抽取方属性抽取（宾语字面量 → `attributes`）
- 规则识别：无 → 抽取方规则抽取（道义动词/条件-动作 → `rules`）
- 流程识别：无 → 抽取方流程抽取（顺序/阶段/流程性描述 → `processes`）
- 切分：entity-aware 300 字符 → 单 chunk，上下文由调用方控制
- span：精确区间强制校验 → 尽力 find，-1 未定位允许
- object_literal：抽取阶段判定 → `object_type` 派生，消费层兜底
- 无 LLM：确定性 fallback + 降级 → 未交卷即空结果 + 降级（无兜底）
