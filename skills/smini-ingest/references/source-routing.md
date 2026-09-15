# smini-ingest · 来源路由参考

> 本文件是 `SKILL.md` 的**规则出处与细则**，全部内容提炼自 semantica 源码。
> 修改前请先核对源码，避免规则与实现脱节。

---

## 1. DocumentFormat 推断表

**出处**：`smini/steps/ingest.py:32-59` `_format_from_path()`

逻辑：取后缀 → 转小写 → 去掉前导 `.` → 查表 → 未命中给 `UNKNOWN`。

```python
def _format_from_path(path: str) -> DocumentFormat:
    """从文件扩展名推断格式。未知扩展名给 UNKNOWN，让 Parse 决定降级。"""
    suffix = Path(path).suffix.lower().lstrip(".")
```

| 后缀 | DocumentFormat | 备注 |
|---|---|---|
| `pdf` | `PDF` | |
| `docx` | `DOCX` | |
| `pptx` | `PPTX` | |
| `xlsx` | `XLSX` | |
| `html` / `htm` | `HTML` | 两个后缀同归一格式 |
| `md` / `markdown` | `MARKDOWN` | 两个后缀同归一格式 |
| `txt` / `text` | `TEXT` | 两个后缀同归一格式 |
| `csv` | `CSV` | |
| `json` | `JSON` | |
| `xml` | `XML` | |
| `epub` | `EPUB` | |
| `py` / `js` / `ts` / `java` / `go` / `rs` / `cpp` / `c` | `CODE` | 八个后缀同归一格式 |
| 其它 / 无后缀 | `UNKNOWN` | **不报错**，让 Parse 决定如何降级 |

**完整枚举（`types.py::DocumentFormat`，15 个值）**：
```
pdf, docx, pptx, xlsx, html, markdown, text, code,
json, csv, xml, epub, image, audio, unknown
```

> 注意：`image` 与 `audio` 两个枚举值在推断表里**没有对应后缀**——
> 源码只映射了 22 个后缀到 13 个格式。图片/音频必须由调用方显式指定，
> 或落到 `UNKNOWN`。本 Skill 裁定：**图片不处理（不做 OCR）**。

---

## 2. URI 约定

**出处**：`smini/types.py::SourceRef` docstring

```
URI 统一用 scheme 前缀，便于下游按协议分流：
  * file:///abs/path/to.pdf
  * https://example.com/page
  * postgres://host/db/table#row=42
  * memory://inline/<sha256>（TEXT 源）
```

实际实现中的差异（以代码为准，**文档与代码冲突时代码是真的**）：

| 场景 | 实际 URI | 出处 |
|---|---|---|
| 本地文件 | `file://<Path(path).resolve()>`（绝对路径） | `ingest.py:85` |
| inline 文本 | `memory://inline/<sha256 前 16 位>` | `ingest.py:103` |
| Web | 原始 URL（Skill 模式下由调用方填入） | 源码未实现，Skill 扩展 |
| DB | `postgres://...`（**v1 未实现**） | 仅 docstring 提及 |

> ⚠️ 文档写 `memory://inline/<sha256>`，代码实际取**前 16 位**（`content_hash(data)[:16]`）。
> 这是有意的——全 64 位太长，前 16 位对去重已足够。以代码为准。

---

## 3. source_id 与 doc_id 算法

**出处**：`smini/types.py::SourceRef.of` / `smini/ids.py`

```python
@classmethod
def of(cls, uri: str, source_type: SourceType, **kw: Any) -> "SourceRef":
    from .ids import stable_id
    return cls(
        source_id=kw.pop("source_id", stable_id("src", uri, prefix="s_")),
        uri=uri,
        source_type=source_type,
        **kw,
    )
```

- `source_id = stable_id("src", uri, prefix="s_")` —— **只依赖 uri，与内容无关**
- `doc_id = doc_id(uri, data)` —— **同时依赖 uri 与内容字节**
- `checksum = content_hash(data)` —— 内容的 sha256

**推论（幂等性的来源）**：
- 同一文件（同路径、同内容）重复摄入 → `source_id` / `doc_id` / `checksum` 全不变
- 同内容但换了路径 → `source_id` 变（uri 变了），`checksum` 不变，`doc_id` 变
- 同路径但内容变了 → 三者全变

> 这正是「**ID 必须由 Python 算**」的原因：LLM 无法精确复现 sha256，
> 手写 ID 会让同一份文档每次摄入都产生新 ID，图谱无限膨胀。

---

## 4. 各路工具的选择与降级

### A 路 · Web

| 工具 | 适用场景 | 备注 |
|---|---|---|
| `WebFetch` | 静态页面、公开内容 | 快，无浏览器开销 |
| `agent-browser` Skill | 需登录 / 动态渲染 / 需点击交互 | 重，但能拿真实渲染结果 |

**降级**：两者都失败 → 报错 + `Degradation(kind="unavailable")` + 告知用户。
**绝不**用记忆里的页面内容冒充抓取结果。

### B 路 · PDF

| 工具 | 适用场景 |
|---|---|
| `pdf` Skill | 通用 PDF 读取/抽取 |
| `pdfkit-py` Skill | 需要更细的 PDF 操作（拆分/表单/OCR 等） |

**无文本层的判定**：提取结果为空，或有效字符数 < 页数的若干倍（经验值：每页 < 50 字符）。
判定为扫描件 → `metadata["no_text_layer"] = true` + `Degradation(kind="unsupported_format")`。

### C 路 · Office

路由顺序：`tencent-docs-routing` → 判定类型 → 对应 Skill
- `.docx` / `.doc` → `tencent-docx`
- `.xlsx` / `.xls` → `tencent-docs-sheetagent`
- `.pptx` / `.ppt` → `tencent-pptx`

### D 路 · 资料库

调 `资料库` Skill（`workbuddy.cn/space` 链接、在线文档、网盘文件）。

### E 路 · Python 兜底

输入必须是**本地文件路径**或**裸文本**。Web 源走 E 路会报错，这是**正确行为**
（引导调用方改用 A 路工具）。

---

## 5. 常见坑（源码里踩过的）

| 坑 | 表现 | 规避 |
|---|---|---|
| Ingest 阶段就解码 | 编码猜错 → 全链路在错误文本上工作且无人知晓 | 铁律 1：只搬字节 |
| 手写 ID | 重跑产生新 ID → 图谱膨胀 | 一律 `smini ids raw` |
| `content` 存解码后的字符串 | Parse 再编码一次 → checksum 对不上 | 必须 base64 |
| 把 PDF 二进制当文本塞进去 | Parse 拿到乱码 | PDF 走 B 路提文本层 |
| 扫描件做 OCR | 成本高、准确率不可控，且用户已裁定不处理 | 直接标记降级 |
| 静默吞掉抓取失败 | 下游拿到空文档还以为成功 | 报错 + Degradation |
