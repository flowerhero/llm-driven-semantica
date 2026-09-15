# llm-driven-semantica

基于 [semantica](https://github.com/sharptoolbox/semantica) 8 步知识图谱流水线的**精简实现**，采用 **Skill-First** 架构：

> 智能（解析版式、命名实体识别、类型化关系抽取、属性/规则/流程抽取、预测决策六件套、答案合成）由**宿主 agent（大模型）**承担，遵循"**契约即规则**"：宿主只须按约定 JSON 契约交卷（实体/关系/属性/规则/流程/状态机/函数指标/时态/动作/约束/授权）；
> Python 只保留**确定性薄壳**——内容寻址 ID、Schema 校验、枚举兜底、惰性锚点、双时态知识图谱、检索溯源。

规则并非凭空设计，而是逐条**溯源到上游 semantica 源码**（见各 `skills/*/references/` 与 `docs/semantica-8步流水线源码解析.md`）。

---

## 特性

- **9 个 Skill** 承载处理逻辑，每个 skill 声明式 workflow + 铁律 + 规则出处
- **确定性运行时 `smini`**：纯 Python 标准库，**零三方依赖**，不读取任何模型 API 凭证
- **契约即规则**：宿主 LLM 按契约交卷（十一件套），薄壳只做确定性映射与枚举兜底
- **幂等**：LLM 只「提议」事实，所有 ID 由 Python 做 `sha256` 内容寻址 → 重跑严格一致
- **契约先行**：Skill 与运行时之间靠 5 份 JSON Schema（`contracts/`）做唯一线格式
- **147 项测试全绿**（契约 + 端到端 + 运行时）

## 架构

```
用户文档
   │  smini-ingest      （按来源形态路由到宿主工具 / 本地兜底）
   ▼  smini-parse       （抽结构化文本 + 版式）
   ▼  smini-normalize   （产出归一化 patch 提议）
   ▼  smini-extract     （宿主 LLM 契约抽取）★
   ▼  smini-build-kg    （实体消歧 + 建图）
   ▼  smini-qa          （谓词归并 + 语义冲突识别）
   ▼  smini-store       （后端选择 + 回执校验）
   ▼  smini-deliver     （查询理解 + 答案合成）
   │  smini-pipeline    （主编排，串联 8 步）
   ▼
ContextPackage（带引用的答案 + 知识图谱）
```

Skill 之间靠 `runs/<id>/NN-*.json` 文件通信；Python 运行时原子命令（`smini.cli` 的 `ingest / fallback / ids / validate / spans / graph / deliver`）负责 Skill 做不到的确定性计算。

## 快速开始

```bash
# 1. 安装（可编辑模式）
pip install -e .

# 2. 跑测试（147 项）
python -m unittest discover -s tests

# 3. 跑端到端示例（使用内嵌样本）
python -m smini.cli build --sample -q "特斯拉 总部"

# 4. 查看参数
python -m smini.cli build --help
```

### 在宿主 agent 中使用（主体路径）

在宿主 agent（豆包/Claude 等具备大模型能力的 agent）中调用 `smini-pipeline` skill，它会按声明式 workflow 串联其余 8 个 step skill；
需要调用外部工具（浏览器抓取、PDF 解析、Office 读取、资料库）时由 skill 显式触发宿主工具。宿主 agent 即 LLM：按 `skills/smini-extract/SKILL.md` 的契约直接交卷即可，**无需配置任何模型 API 凭证**。

## 目录结构

```
llm-driven-semantica/
├── skills/              # 9 个 Skill（本项目核心交付物）
│   ├── smini-ingest/
│   ├── smini-parse/
│   ├── smini-normalize/
│   ├── smini-extract/   # ★ 最能体现价值：宿主 LLM 契约即规则抽取
│   ├── smini-build-kg/
│   ├── smini-qa/
│   ├── smini-store/
│   ├── smini-deliver/
│   └── smini-pipeline/  # 主编排
├── smini/               # 确定性运行时（纯标准库，零依赖）
├── contracts/           # 5 份 JSON Schema 线格式契约
├── tests/               # 147 项测试（使用内嵌样本，不依赖外部素材）
├── docs/                # 设计文档（架构、实现对比、源码解析）
├── LICENSE              # Apache-2.0
├── NOTICE               # 上游 MIT 出处声明
├── pyproject.toml
└── .gitignore
```

## 许可证

本项目以 **Apache License 2.0** 发布（见 `LICENSE`）。

设计参考了上游 [semantica](https://github.com/sharptoolbox/semantica)（**MIT License**, © 2026 sharptoolbox），
依据其许可保留版权与声明，详见 `NOTICE`。

## 已知边界

- 不处理扫描版 PDF（需另接 OCR 能力）
- 抽取无确定性兜底：**必须由宿主 LLM 按契约交卷**（未交卷则登记降级并返回空，不造假）
- 宿主 LLM 由 agent 平台承载，**无需配置外部模型凭证**；`smini` 运行时不发起任何模型 HTTP 调用
- `tests/fixtures/` 中二进制与私有素材未入库（详见该目录 README）；测试本身使用内嵌样本，不受影响
