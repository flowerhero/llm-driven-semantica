# smini-parse · 版式规则参考

> 本文件是 `SKILL.md` 的**规则出处与细则**，全部内容提炼自 semantica 源码。
> 修改前请先核对源码，避免规则与实现脱节。

---

## 1. 格式判定与解码

### 1.1 解码回退链

**出处**：`smini/steps/parse.py:39-64` `_decode()`

```python
if fmt in (MARKDOWN, TEXT, CODE, CSV, JSON, XML, HTML):
    encodings = ["utf-8", "utf-8-sig", "gb18030", "latin-1"]
else:
    encodings = []        # 二进制格式不在此解码，交给降级分支

for enc in encodings:
    try:  return content.decode(enc), enc, warnings
    except UnicodeDecodeError:  continue

# 兜底：替换非法字节，宁可带乱码也不要崩
text = content.decode("utf-8", errors="replace")
warnings.append("utf-8 解码失败，已用 replacement 字符兜底（内容可能含乱码）")
```

要点：
- `gb18030` 覆盖中文 GBK/GB2312 场景，排第三
- `latin-1` **永不失败**（任何字节都能解），放在最后的 `replace` 之前
- 非 utf-8 解码成功时追加 warning `f"编码推断为 {enc}（非 utf-8）"`（`parse.py:253-254`）

### 1.2 不支持的格式

**出处**：`parse.py:230-250`

`PDF / DOCX / PPTX / XLSX / EPUB / IMAGE / AUDIO` 在 Python 兜底路径下**没有解析后端**，
产出占位文本并显式降级：

```python
text = f"[格式 {fmt.value} 在 v1 不支持解析，仅记录原始字节 {rd.size_bytes}B]"
parser = "unsupported"
warnings = [f"格式 {fmt.value} 在 v1 无解析后端，已降级为占位文本"]
```

> Skill 模式下 PDF/Office **应当先走 WorkBuddy 工具**拿到真实文本，
> 这条降级只在工具不可用（或扫描件无文本层）时触发。

---

## 2. BlockKind 判定细则

### 2.1 确定性实现的正则

**出处**：`parse.py:32-36`

```python
_HEADING_RE   = re.compile(r"^(#{1,6})\s+(.*)$")
_LIST_RE      = re.compile(r"^([-*+]|\d+[.)])\s+(.*)$")
_QUOTE_RE     = re.compile(r"^>\s?(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|(.+)\|\s*$")
```

### 2.2 判定顺序

**出处**：`parse.py:117-198`，**顺序不可调换**（先特殊后一般）：

| 序 | 条件 | 结果 |
|---|---|---|
| 1 | 空行 | 冲刷已累积的段落 |
| 2 | markdown 且 `` ``` `` 开头 | `CODE`（读到下一个围栏，取 `language`） |
| 3 | markdown 且当前行是管道行 **且** 下一行是分隔行 | `TABLE`（持续读到非管道行，解析 `rows`） |
| 4 | markdown 且匹配 `_HEADING_RE` | `HEADING`（`level = min(#个数, 6)`） |
| 5 | markdown 且匹配 `_LIST_RE` | `LIST_ITEM` |
| 6 | markdown 且匹配 `_QUOTE_RE` | `QUOTE` |
| 7 | 其余 | 累加进 `PARAGRAPH`（直到空行或特殊行） |

### 2.3 确定性实现的两个硬局限（LLM 的增值空间）

1. **只在 `fmt is DocumentFormat.MARKDOWN` 下才识别 heading / list / quote / table / code**
   （`parse.py:131`、`:148`、`:163`）。
   → 一份 `.txt` 文档即使写满 `# 标题` 和 `- 列表`，也**全被当成 paragraph**。
   **LLM 应当做到格式无关的版式识别。**

2. **10 类里只覆盖 6 类**：`paragraph / heading / list_item / quote / table / code`。
   `caption` / `image_ref` / `footnote` / `metadata` **从未被识别过**。

### 2.4 类型专属附加字段

| kind | 字段 | 说明 |
|---|---|---|
| `heading` | `level` | 1–6；源码 `min(len(m.group(1)), 6)`（`parse.py:167`） |
| `table` | `rows` | `list[list[str]]`，按 `\|` 切分并 `strip` 每格（`parse.py:153`） |
| `code` | `language` | 围栏后的标识；无则 `None`（`parse.py:133`） |

### 2.5 HTML 预处理

**出处**：`parse.py:67-76` `_strip_html()`

依次：删 `<script>`/`<style>` 整块 → 去其余标签 → 还原 `&nbsp; &amp; &lt; &gt; &quot;`
→ 折叠连续空格/制表符。

注意：HTML 走这条处理后，`text` 与原始字节**必然不同**，故 HTML 的保真校验
应以「去标签后的文本」为基准，而非原始 HTML 字节。

---

## 3. 保真校验

### 3.1 长度保真

**出处**：`parse.py:291-295`（LLM 路径）、`runtime.py::_check_parsed_doc`（validate 命令）

```
abs(len(text) - len(原文)) > max(8, int(0.05 * len(原文)))  →  判定失败
```

短文档有 8 字符的绝对余量，长文档放宽到 5%。

### 3.2 span 自洽

对每个 block：`0 <= char_start < char_end <= len(text)` 且 `text[s:e] == block.text`。

### 3.3 顺序与专属字段

- `order` 必须全局单调递增（**跨 kind**，不是每种 kind 各自从 0 开始）
- `table` 必须有非空 `rows`
- `heading` 必须有 `level`

---

## 4. 已修复的三个静默 bug（坐标计算的坑）

> 这三个 bug 的共同特征：**产物自洽，所以校验查不出来**——
> `block.text` 取自 `text[start:end]`，切片永远成立，错的只是范围。
> 这是坐标类代码最危险的失效模式。

### 4.1 表格被截断，后半段变成残片段落

原实现（`parse.py:157`）：
```python
tend = (pos + len(lines[k - 1]) + (len(lines[k]) + 1 if k < n else 0)) if k > i else line_end
```
`pos` 在读取表格的多行循环里**没有随 k 递增**，导致 `tend` 只覆盖到表格开头一小段。

实测：4×3 表格被判为 `span=[99,121)`（仅 22 字符），
剩余部分被下一个 paragraph 收走，内容形如 `' |\n| 2023 | 9'`。

**修法**：逐行累加，不依赖循环变量：
```python
tend = line_start + sum(len(lines[m]) + 1 for m in range(i, k)) - 1
```

### 4.2 代码围栏后紧跟正文时整体偏移

原实现（`parse.py:144`）：`pos = cend + len(lines[j]) + 1` —— 少算 1 个换行。

实测：正文应为 `[18,26)`，实为 `[17,24)`（偏 1 且被截断）。
若代码块后有空行，空行的 `line_end + 1` 会**碰巧把偏移修正回来**，
所以只在无空行时暴露——典型的间歇性 bug。

**修法**：`pos = cend + len(lines[j]) + 2`（cend + 换行 + 围栏行 + 换行）

### 4.3 quote / heading / list 的起始偏移错位

- `_QUOTE_RE = r"^>\s?(.*)$"` 的 `\s?` 会吃掉 `>` 后的空格，
  但原实现写死 `cstart = line_start + 1`，只跳过 `>` → **span 与 text 错开一位**
- heading / list 用 `cstart = line_start + m.end(1) + 1`，
  假设「标记与内容之间恰好 1 个分隔符」，多空格或 tab 时会整体偏移

**修法**：用捕获组的**真实起始位置**，不做宽度假设：
```python
cstart = line_start + m.start(2)   # heading / list_item
cstart = line_start + m.start(1)   # quote
```

> **教训**：任何手工偏移计算，都要么用 `m.start(n)` 取捕获组真实位置，
> 要么逐行累加。绝不要假设「分隔符是一个字符」。

---

## 5. 常见坑

| 坑 | 表现 | 规避 |
|---|---|---|
| 在 Ingest 就解码 | 编码猜错且无人知晓 | 解码权归 Parse（铁律） |
| LLM 自己填坐标 | 错得自洽、校验查不出 | 一律 `smini spans parse` 补算 |
| 重复段落从头 find | 所有副本定位到第一处 | 顺序定位（从上一个区块末尾起找） |
| 把扫描件当文本解析 | 产出乱码或空内容 | 标记 `unsupported`，不做 OCR |
| 改写标点/合并段落 | 保真校验失败 | 转录，不是改写 |
| 非 markdown 文档不识别版式 | 全是 paragraph（确定性路径的局限） | LLM 做格式无关的识别 |
| HTML 拿原始字节做保真基准 | 必然超差误报 | 以去标签后的文本为基准 |
