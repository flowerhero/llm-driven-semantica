---
name: smini-ingest
description: "把各类来源摄入为 RawDocument 列表，是 semantica 流水线的 Step 1。按 source 形态路由到 WorkBuddy 自身或生态内的工具（WebFetch / agent-browser / pdf / pdfkit-py / tencent-docs-routing / 资料库）；本地文件与裸文本走 Python 兜底。Use when the user says 摄取文档 / 导入文件 / 抓取网页 / 从 X 建图 / ingest / read documents / 把这个 PDF 读进来。铁律：Ingest 不做任何解读，只做拿字节、算 checksum、记出处三件事。"
---

# smini-ingest · 摄取（Step 1）

> **原样搬运，不做任何解读。编码推断推迟到 Parse。**
>
> 本 Skill 的核心价值是**路由**：判断 source 属于哪种形态，选对工具把它拿到手。
> 确定性实现 `smini/steps/ingest.py` 只能吃本地文件与裸文本，其余形态全靠 WorkBuddy 生态工具。

## 铁律（违反会导致全链路失真且无从恢复）

1. **Ingest 不做任何解读** —— 只做三件事：拿字节、算 checksum、记出处。
   > 出处：`smini/steps/ingest.py:1-4` docstring ——「唯一职责：原样搬运，不做任何解读。
   > 拿到字节 → 算 sha256 校验和 → 记下来源 SourceRef。**编码推断推迟到 Parse**」。
   >
   > **为什么是铁律**：原版在此阶段就把 bytes 解码成 str。一旦编码猜错（如 GBK 文件按 UTF-8 解），
   > 后面 Parse / Normalize / Extract 全在错误文本上工作，**且没有任何环节知道错了**。
   > 把解码权收归 Parse，才能让错误在 Parse 阶段暴露并可回退。
   >
   > 因此：**绝不在本步做解码、清洗、去噪、截断、去水印、格式转换。**

2. **绝不静默外联** —— 抓网页前先向用户确认 URL；拿不到就报错，**绝不伪造内容**。
   > 出处：`smini/steps/ingest.py:70-75` —— 遇到 `http(s)://` / `ftp://` 直接抛
   > `StepError("Web 源在 v1 不支持（需联网能力）")`。
   > Skill 模式下有真实工具可用，不必拒绝，但**外联动作必须先确认**。

3. **不处理扫描版 PDF / 图片** —— 无文本层的 PDF 直接标记降级，**不做 OCR**
   （用户 2026-09-03 裁定：不处理扫描件）。

4. **不出 ID** —— `doc_id` / `source_id` / `checksum` 一律由 Python 的 sha256 算出。
   > 理由见 `smini/ids.py` 头部注释：原版用 `hash()` 带随机盐，跨进程 ID 漂移会导致图谱无限膨胀。

---

## Workflow（声明式）

### 第 1 步 · 识别 source 形态

判定顺序**必须严格按此表自上而下**（基于 `smini/steps/ingest.py:62-80` 的分支顺序）：

| # | 输入形态 | 判定条件 | 走哪条路 |
|---|---|---|---|
| 1 | Web 页面 | 以 `http://` / `https://` 开头 | → **A 路**（Web 工具） |
| 2 | 本地文件（显式） | 以 `file://` 开头 | → **E 路**（Python 兜底） |
| 3 | 裸文本（显式） | 以 `text://` 开头 | → **E 路**（Python 兜底） |
| 4 | PDF | 后缀为 `.pdf` | → **B 路**（PDF 工具） |
| 5 | Office 文档 | 后缀为 `.docx` / `.xlsx` / `.pptx` / `.doc` / `.xls` / `.ppt` | → **C 路**（Office 工具） |
| 6 | 在线文档 / 资料库 | 用户明示「资料库 / workbuddy.cn / 空间里的文档」 | → **D 路**（资料库 Skill） |
| 7 | 本地文件（隐式） | 字符串形如路径**且**该文件存在 | → **E 路**（Python 兜底） |
| 8 | 裸文本（隐式） | 以上都不匹配 | → **E 路**，当 inline 文本 |

> 注意第 1 条与源码的差异：源码 `ingest.py:70` 对 Web 源直接抛错，
> **Skill 模式下改走工具**——这是「智能从硬代码挪进 Skill」的直接体现。
>
> 注意第 4/5 条优先于第 7 条：`.pdf` 文件即使路径存在，也应走工具提取文本层，
> 而非把 PDF 二进制原样塞进 RawDocument（否则 Parse 拿到的是一堆乱码字节）。

格式与工具的详细路由表见 **`references/source-routing.md`**。

### 第 2 步 · 按路由取字节

**A 路 · Web**
- 静态页面 → `WebFetch` 工具（快、省事）
- 需要交互 / 登录 / 动态渲染 → `agent-browser` Skill
- 拿到正文后**转为 UTF-8 字节**，`source.uri` 保留原始 URL，
  `metadata` 记 `{"fetched_via": "webfetch"|"agent-browser", "url": <原始URL>}`
- `source_type = "web"`

**B 路 · PDF**
- 调 `pdf` 或 `pdfkit-py` Skill 提取文本层
- 只取**有文本层**的 PDF。若返回空或极少文本（扫描件特征），
  设 `metadata["no_text_layer"] = true`，并追加一条 Degradation
  （`kind: "unsupported_format"`）——**不做 OCR**
- 取到文本后转 UTF-8 字节，`source_type = "file"`，`source.uri = file://<绝对路径>`

**C 路 · Office**
- 先调 `tencent-docs-routing` 判定具体类型，再转对应 Office Skill
- 取文本后转 UTF-8 字节，`source_type = "file"`

**D 路 · 资料库**
- 调 `资料库` Skill 读取，取文本后转 UTF-8 字节
- `source_type = "db"`，`source.uri` 用资料库返回的链接

**E 路 · 本地文件 / 裸文本（Python 兜底）**
```bash
python -m smini.cli ingest --source "<spec>" [--source "<spec2>" ...] --out runs/<run_id>/01-raw.json
```
`--source` 可重复，一次摄入多个来源。spec 支持四种写法（出处 `ingest.py:64-80`）：
- `file:///绝对路径`
- `text://<内容>`（会做 URL decode）
- 形如路径且存在的字符串 → 当文件
- 其它 → 当 inline 文本

> **当 A/B/C/D 各路工具都不可用时，也必须走 E 路**——
> 若输入是本地文件，E 路能正确取字节；若输入是 PDF 但工具不可用，
> 则登记 Degradation 让 Parse 去降级（不要在本步伪造文本）。

### 第 3 步 · 组装 RawDocument

若走了 A/B/C/D 路（工具已给出文本），需自行组装 JSON。字段规范：

| 字段 | 取值 | 出处 |
|---|---|---|
| `doc_id` | **留空**，交给 Python 算 `doc_id(uri, data)` | `ingest.py:87` / `:106` |
| `source.source_id` | **留空**，Python 算 `stable_id("src", uri, prefix="s_")` | `types.py::SourceRef.of` |
| `source.uri` | `file://<绝对路径>` / `https://<URL>` / `memory://inline/<sha256前16位>` | `ingest.py:85` / `:103` |
| `source.source_type` | `file` / `web` / `db` / `stream` / `text` | `types.py::SourceType` |
| `source.checksum` | **留空**，Python 算 sha256 | `ingest.py:86` |
| `content` | **原始字节的 base64**（不是解码后的字符串） | `smini/runtime.py:301` |
| `size_bytes` | 字节数 | `ingest.py:93` |
| `media_type` | 文件 → `application/octet-stream`；inline 文本 → `text/plain` | `ingest.py:91` / `:108` |
| `fetched_at` | ISO8601 UTC 或 `null` | `types.py::SourceRef` |
| `metadata` | 取数方式、路径、URL 等，便于回溯 | `ingest.py:96` / `:113` |

**补算 ID（必做）**——自己拼 ID 会破坏幂等：
```bash
python -m smini.cli ids raw --in runs/<run_id>/01-raw.json
```
> `ids raw` 会做三件事（`smini/runtime.py:320-331`）：
> 从 base64 还原字节 → 算 `checksum` 并同步到 `source.checksum` → 补 `doc_id` 与 `size_bytes`。

### 第 4 步 · 落盘

写 `runs/<run_id>/01-raw.json`，类型为 `RawDocument[]`（数组，即使只有一个文档）。
契约见 `contracts/raw.schema.json`。

### 第 5 步 · 校验（必做，不可跳过）

```bash
python -m smini.cli validate raw --in runs/<run_id>/01-raw.json
```
退出码 `0` = 通过，`1` = 契约违规（**此时必须停下修，不得带着错误产物进 Parse**）。

---

## 输出契约

`runs/<run_id>/01-raw.json` → `RawDocument[]`

```
doc_id / source{source_id,uri,source_type,checksum,fetched_at,locator,extra}
/ media_type / content(base64) / size_bytes / checksum / fetched_at / metadata
/ degradations[]
```

字段完整定义见 `contracts/raw.schema.json`。

---

## 降级对照（任何 fallback 都必须出声）

在文档对象的 `degradations[]` 里追加记录，五要素齐全：
`component` / `kind` / `reason` / `fallback` / `impact`。

| 情况 | 动作 | `kind` |
|---|---|---|
| 网页抓取失败 | 报错并告知用户，**不伪造内容** | `unavailable` |
| PDF 无文本层（扫描件） | 标记 `no_text_layer`，交 Parse 降级，**不做 OCR** | `unsupported_format` |
| PDF/Office 工具不可用 | 改用 E 路取原始字节，让 Parse 降级 | `unavailable` |
| 后缀不认识 | `format=unknown` + warning，让 Parse 决定 | — |
| 路径不存在 | 直接报错，停止 | — |
| 文本被截断（超长） | 标记 `truncated`，写明截断位置 | `truncated` |

---

## 自检清单（交付前逐条确认）

- [ ] **没有**在 Ingest 阶段做任何文本解码或清洗
- [ ] `content` 是 base64，不是已解码的字符串
- [ ] `checksum` 是 64 位十六进制 sha256，由 `smini ids raw` 算出
- [ ] `doc_id` / `source_id` 由 Python 算出，**没有手写**
- [ ] `source.uri` 保留了真正的出处（绝对路径 / 原始 URL）
- [ ] `metadata` 记录了取数方式，便于回溯
- [ ] `validate raw` 退出码为 0
- [ ] 扫描件 PDF 已标记 `no_text_layer`，未尝试 OCR
- [ ] 任何 fallback 都已登记 `degradations`，未静默降级

---

## 验证

```bash
bash skills/smini-ingest/verify.sh
```

覆盖：多形态摄入、契约校验通过、幂等（同内容两次 ID 一致）、
反面用例（非法 checksum / 非 base64 内容 / Web 源引导 / 多余字段）被正确拦截。
