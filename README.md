# llm-driven-semantica

基于[semantica]知识图谱理念的**精简实现**，摒弃了[semantica]的繁重依赖和复杂实现，采用 **「契约即规则」（Rule-by-Contract）** 架构：

> 语义抽取（实体-关系三元组、属性、业务规则、业务流程、预测决策六件套、时态/动作/约束/授权）由**宿主 agent（大模型）**承担——宿主只须按约定 JSON 契约交卷（十一件套）；
> Python 只保留**确定性薄壳**——内容寻址 ID、Schema 校验、枚举兜底、惰性锚点、双时态知识图谱。
> **零规则抽取**：不配置任何 `SMINI_LLM_*` 环境变量、不发起模型 HTTP 调用、无确定性 Python 抽取规则。

抽取结果质量高度依赖所调用模型的能力，建议使用SOTA模型执行本skill。

---

## 特性

- **extract-only 三步流水线**：`ingest（读源原语）→ extract（宿主 LLM 契约抽取 ★）→ build_kg（惰性锚点消费层）`
- **宿主即 LLM（方式 3）**：宿主 agent 按契约交卷 JSON（`--host-contract` / `HostAgentLLMProvider.inject()`），无需任何模型凭证
- **确定性运行时 `smini`**：纯 Python 标准库，**零三方依赖**，只做 Skill 做不到的计算（ID / 校验 / 建图）
- **幂等**：LLM 只「提议」事实，所有 ID 由 Python 做 `sha256` 内容寻址 → 重跑严格一致
- **十一件套契约**：`entities / relations / attributes / rules / processes / states / functions / temporal / actions / constraints / permissions`
- **126 项测试全绿**（契约 + 端到端 + 运行时），并同步渲染自包含 HTML 查看器（`scripts/render_viewer.py`，12 Tab）

## 架构

```
用户文档 / 文本
   │  宿主 agent 读取（摄入/归一化外包宿主，无确定性 Python 解析）
   ▼  smini-extract  ★ 宿主按契约交卷（十一件套 JSON）
   │  Python 薄壳：ID 补算（sha256 内容寻址）+ Schema 校验 + 枚举兜底
   ▼  build_kg（惰性锚点消费层）
   │  object_literal 派生 · 属性双落位 · 六件套 count 登记 · 实体-边图
   ▼
KnowledgeGraph + HTML 查看器（scripts/render_viewer.py）
```

## 快速开始

### 在宿主 agent 中使用（主体路径）

宿主 agent（Claude、WorkBuddy等具备大模型能力的 agent）按 `skills/smini-extract/SKILL.md` 的契约直接交卷即可，例如：
“调用smini-extract skill抽取 /path/to/doc”
**无需配置任何模型 API 凭证**：
宿主读文档 → 产出 `{entities, relations, attributes, rules, processes, ...}` 十一件套 JSON → Python 薄壳确定性映射 → 建图 + 渲染 HTML。

```bash
# 0. 环境：Python ≥ 3.10（代码使用 `X | None` 注解与 `types.UnionType`，3.9 不支持）；
#    推荐 Python 3.12 —— `uv run --python 3.12 -m smini.cli …`（uv 会自动匹配）

# 1. 安装（可编辑模式）
pip install -e .

# 2. 跑测试（126 项）
python -m unittest discover -s tests

# 3. 跑内置样例（宿主未交卷 → 抽取为空，但管线完整跑通并提示）
python -m smini.cli build --sample

# 4. 宿主 agent 充当 LLM：按契约交卷后抽取
python -m smini.cli build --sample --host-contract contract.json --extract-out runs/<run_id>/04-extraction.json
```

## 项目结构

```
smini/                 # 确定性薄壳（Python 标准库，零依赖）
├── sources.py         #   读源原语（text:// / file:// / inline；透传宿主 raw_docs）
├── steps/extract.py   #   ★ 宿主契约抽取（薄壳映射 + 惰性锚点）
├── steps/build_kg.py  #   实体-边图 + 属性双落位 + 六件套 count
├── steps/project.py   #   项目层薄壳（ProjectManager：项目生命周期 + 来源归并）
├── steps/interview.py #   访谈控制器薄壳（会话落盘 + delta 合并 + 收尾接线）
├── llm.py             #   HostAgentLLMProvider（宿主即 LLM，零凭证）
├── ids.py             #   sha256 内容寻址 ID
├── runtime.py         #   原子命令：ids / validate / graph build
├── cli.py             #   python -m smini.cli
└── types.py           #   数据契约（dataclass）
contracts/             # 唯一 JSON Schema：extraction.schema.json
skills/smini-extract/  # 宿主契约 Skill（SKILL.md + references + scripts/render_viewer.py）
skills/smini-interview/# 访谈式本体构建 Skill（无文档/文档不全时的信息源替换）
tests/                 # 测试（145 项）
docs/                  # 设计文档 + 源码解析
```

## 访谈式本体构建（无文档 / 文档不全时）

现实里并非总有文档可供抽取：项目刚启动只有业务方脑子里的信息，或文档缺实际做法、例外情形、角色分工。此时用 **`smini-interview`** 以多轮对话为信息源，产出与文档抽取**完全同构**的宿主契约（同一张 JSON → 同一套 validate / 渲染 / 图谱）。

**「项目（project）」为持久化一等单元**：访谈强制挂靠项目，会话永远从项目累计模型（consolidated）起步、收尾后归并回项目——项目建立后**随时可追加访谈**。

```
projects/<项目名>/
├── index.json                   # 项目注册表
├── project.json                 # 累计模型 consolidated + 来源清单 sources + 冲突 conflicts
├── interviews/<会话id>/         # 每次访谈（session.json + host-contract + 04-extraction.json + html）
├── runs/<runid>/                # 项目模式文档抽取（结构同现有 runs/）
└── 04-extraction.html           # 项目累计模型总览渲染
```

```bash
# 新建项目 + 访谈（薄壳命令；提问/判定/收尾由宿主 LLM 声明式完成）
python -m smini.cli project create 适当性管理项目 --domain 金融/适当性
python -m smini.cli interview new 适当性管理项目
python -m smini.cli interview append 适当性管理项目 --user "回答" --delta delta.json --question "下一问"
python -m smini.cli interview finish 适当性管理项目

# 追加访谈（再次调起 = 在累计模型上继续，不重复已覆盖主题）
python -m smini.cli interview new 适当性管理项目

# 项目模式文档抽取归并
python -m smini.cli project ingest 适当性管理项目 runs/<runid>
```

宿主（豆包）充当 LLM 时的实际用法：按 `skills/smini-interview/SKILL.md` 的接缝，每轮交卷
`{delta, next_question, closing, note}`；薄壳只做合并/落盘/收尾接线/归并——不生成问题、不判断覆盖度、不决定收尾。

## 许可

Apache-2.0（`LICENSE` / `NOTICE`）。本体抽取规则部分溯源自 MIT 许可的 [semantica](https://github.com/sharptoolbox/semantica)。
