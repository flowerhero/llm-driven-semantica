---
name: smini-parse
description: "从原始文档提取结构化文本与版式区块，产出 ParsedDocument。让大模型按 semantica 源码提炼的版式规范识别 BlockKind 10 类封闭枚举（含确定性实现从未识别的 caption/image_ref/footnote/metadata）。Use when the user says 解析文档 / 提取版式 / 分块 / parse document / extract layout，或紧接 ingest 之后。保真铁律：block.text 必须严格等于 text[char_start:char_end]，长度偏差超 max(8,5%) 即回退确定性解析。"
---

# smini-parse · 解析（Step 2）

> 把原始字节变成「**纯文本 + 版式区块**」。
>
> 本 Skill 承载的规则逐条来自 semantica 源码提炼，出处见 `references/parsing-rules.md`。
> 确定性实现（`smini/steps/parse.py`）降级为 **fallback 兜底**与**保真校验器**。

## 铁律

1. **保真优先于美观。** 你是在**转录**，不是在改写。
   `block.text` 必须严格等于 `text[char_start:char_end]`，一个字符都不能差。
   > 出处：`smini/steps/parse.py:3-5` ——「每个 Block 的 `char_start`/`char_end` 锚定在
   > `ParsedDocument.text` 上，且 `block.text == text[char_start:char_end]` ——
   > 这是整条链路偏移回溯的**第一根桩**」。这根桩一歪，后面所有 Citation 全废。

2. **坐标不自己算。** 你给出区块的 **kind 与文本**，坐标交给 Python 补。
   > 为什么：逐字符算偏移 LLM 几乎必错，而且**错得自洽**（block.text 取自切片，
   > 常规校验查不出来），会静默污染下游所有溯源。
   > 调用 `smini spans parse` 补算 `block_id` 与 `char_start`/`char_end`。

3. **不静默造假。** 二进制格式转不出文本 → 显式标记 `parser="unsupported"` + warning，
   **绝不编造内容填充**。出处：`parse.py:230-250`。

4. **降级必须出声。** 保真校验失败 → 回退确定性解析 + 登记 `Degradation`。

---

## Workflow（声明式）

### 1. 读输入

`runs/<run_id>/01-raw.json`（`RawDocument[]`）。`content` 是 **base64** 字节。

> **语义**：Parse / Normalize / Extract 都是**逐文档**操作（span 坐标系是文档内的），
> 到 Build KG 才跨文档汇聚成一张图。故默认一次处理一个文档，产出单个 `ParsedDocument`。
> 批次处理时顶层为数组，`validate` 会逐元素校验。

### 2. 判定格式并解码

用 `DocumentFormat` 15 类（推断表见 `references/parsing-rules.md` §1）。

解码回退链（出处 `parse.py:39-64`）：
```
utf-8 → utf-8-sig → gb18030 → latin-1 → utf-8(replace 兜底)
```
非 utf-8 时必须在 `warnings` 记录实际用的编码。
> 编码推断权**归 Parse**（Ingest 只搬字节）——这样猜错了在本步暴露，可被保真校验拦下。

### 3. 取纯文本 `text`

- **纯文本类**（markdown / text / code / json / csv / xml）：直接解码结果
- **HTML**：先剥标签再取文本（出处 `parse.py:67-76` `_strip_html`）
- **PDF / DOCX / PPTX / XLSX 等二进制**：**不要自己硬啃字节**，调 WorkBuddy 工具拿文本
  - PDF → `pdf` / `pdfkit-py` Skill
  - Office → `tencent-docs-routing` 路由到对应 Office Skill
  - 拿到文本后，`parser` 标记 `"pdf.skill"` / `"office.skill"`
- **工具不可用**：交给 Python 兜底 → 标记 `parser="unsupported"` + warning

> ★ 边界：**不处理扫描版 PDF / 图片**。无文本层的 PDF 直接标记
> `parser="unsupported"` + warning「无文本层，需 OCR（本项目不处理）」，**不做 OCR**。

### 4. 识别版式区块 `blocks[]` ← **本 Skill 的核心**

**BlockKind 10 类封闭枚举**（`types.py::BlockKind`），不得自创：

```
paragraph  heading  table  code  list_item
quote      caption  image_ref  footnote  metadata
```

| kind | 判定线索 | 必须带的附加字段 |
|---|---|---|
| `heading` | 标题行（`^(#{1,6})\s+`、`第N章`、字号/加粗明显的短行、独立成行的标题） | `level`：1–6 |
| `list_item` | 列表项（`-` `*` `+` `1.` `1)` 开头，或无标记但明显并列的短行） | — |
| `quote` | 引用（`>` 开头、引号包裹的独立段落、缩进块） | — |
| `table` | 表格（markdown 管道表、制表符对齐的网格、HTML table） | **`rows: string[][]`** 二维单元格 |
| `code` | 代码围栏、缩进代码块、命令行片段 | `language`（能识别则给，不能则 `null`） |
| `paragraph` | 其余连续正文 | — |
| `caption` | 图/表下方的说明文字（「图 1 …」「表 2 …」「如图所示」） | — |
| `image_ref` | 图片引用占位（`![alt](src)`、`<img>`、「见附图」） | — |
| `footnote` | 脚注/尾注（行首 `[1]`、`¹`、「注：」「参考文献」） | — |
| `metadata` | 文档元信息块（标题页的作者/日期/版本号、文首的摘要字段） | — |

> ★ **这是 LLM 相比确定性实现的增值点（已实测）**：
> 原 `parse.py::_parse_blocks` 只覆盖 **6 类**（paragraph / heading / list_item /
> quote / table / code），`caption` / `image_ref` / `footnote` / `metadata`
> **四类从未被识别过**。
> 实测证据：`图示：2023年交付量分布` 被确定性路径判为 `paragraph`，
> LLM 应判为 **`caption`**。

### 5. 输出结构

每个 block 给这四个字段即可，**不要给坐标**：
```json
{"kind": "heading", "text": "特斯拉公司概览", "level": 1}
{"kind": "table", "text": "| 年份 | 营收 |\\n|---|---|", "rows": [["年份","营收"],["2023","967亿"]]}
```
`block_id` / `order` / `char_start` / `char_end` 一律留空，由 Python 补算。

### 6. 落盘 + 补算坐标

写 `runs/<run_id>/02-parsed.json`，`parser` 标记来源。然后：

```bash
# 补算 block_id + char_start/char_end（LLM 不给坐标）
python -m smini.cli spans parse --in runs/<run_id>/02-parsed.json
```

### 7. 保真校验（**不可跳过**）

```bash
python -m smini.cli validate parse \
  --in runs/<run_id>/02-parsed.json \
  --orig runs/<run_id>/01-raw.json
```

校验四件事（出处 `parse.py:291-295` + `runtime.py::_check_parsed_doc`）：
1. **长度保真**：`abs(len(text) - len(原文)) > max(8, 0.05 * len(原文))` → 失败
2. **Schema 校验**：`contracts/parsed.schema.json`
3. **span 自洽**：每个 `block.text == text[char_start:char_end]`，且不越界
4. **类型专属必填**：`table` 必须有 `rows`，`heading` 必须有 `level`，`order` 全局单调递增

任一项失败 → 回退 `python -m smini.cli fallback parse ...`，并登记
`Degradation(component="parse.fidelity", kind=parse_failed, ...)`。

> 为什么必须 Python 校验：LLM 转录长文时极易丢段、合并段落或改写标点，
> 这种偏差**静默发生**，唯有机械比对能发现。

---

## 输出契约

`runs/<run_id>/02-parsed.json` → `ParsedDocument`（单文档）或 `ParsedDocument[]`（批次）

```
doc_id / format / source / text / blocks[] / parser / parser_version / warnings[] / degradations[]
```

`Block`：`block_id` / `kind` / `text` / `order` / `char_start` / `char_end`
/ `level`(heading) / `rows`(table) / `language`(code)

---

## 降级对照

| 情况 | 动作 | `kind` |
|---|---|---|
| 长度偏差超阈值 | 回退确定性解析 | `parse_failed` |
| 二进制格式无解析后端 | `parser="unsupported"` + warning，**不编造文本** | `unsupported_format` |
| PDF 无文本层（扫描件） | 标记降级，**不做 OCR** | `unsupported_format` |
| 解码只能靠 replace 兜底 | 记录 warning「内容可能含乱码」 | — |
| 解析后无任何区块 | 记录 warning「文档可能为空」 | — |

---

## 自检清单

- [ ] `text` 与原文长度差 ≤ `max(8, 5%)`
- [ ] 每个 block 的 `text` 能由 `text[char_start:char_end]` 精确切出（已跑 `validate` 确认）
- [ ] `order` 全局单调递增且从 0 开始
- [ ] table 有非空 `rows`；heading 有 `level`（1–6）；code 尽量给 `language`
- [ ] **没有**自己填 `block_id` / `char_start` / `char_end`
- [ ] 无文本层的 PDF 已标记 `unsupported`，未编造内容
- [ ] 走了 fallback 时已登记 Degradation
- [ ] 已尝试识别 `caption` / `image_ref` / `footnote` / `metadata`（确定性路径覆盖不到的 4 类）

---

## 验证

```bash
bash skills/smini-parse/verify.sh
```

覆盖：markdown 全版式识别、span 自洽、保真、幂等、LLM 提议态经 `spans` 补算后
与确定性路径逐字节一致、多文档批次、编码回退、PDF 显式降级；
反向覆盖 span 越界 / 文本不一致 / order 乱序 / 非法 kind / table 缺 rows /
heading 缺 level / 长度超差 / 区块文本找不到。
