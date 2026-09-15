---
name: semantica-normalize
description: "清洗解析后的文本并保留可逆的归一化操作清单（SpanPatch），产出 NormalizedDocument。规则来源于 semantica 源码提炼（Unicode NFC、控制字符剥离、连续空格折叠、DATE/MONEY/PERCENT/QUANTITY 规则实体与归一化）。Use when the user says 归一化/清洗文本/规范化/normalize/clean text, or after parse. ★关键设计：LLM 只产出 patch 提议，坐标漂移与几何校验由 Python 执行。"
---

# semantica-normalize · 归一化（Step 3）

> 清洗文本，**同时保留一份可逆的编辑日志（SpanPatch）**，让下游的偏移能回溯到原文。
>
> 这是 Citation 能"指出原文第几行第几个字"的前提。

## ★ 关键设计：LLM 只提议，Python 执行

**为什么这么切**：`SpanPatch` 必须满足三条几何约束（`types.py::validate_patches`）：

1. 在 new 坐标系下按 `new_start` **升序**
2. 区间**不重叠**
3. `new_start == orig_start + 前面所有 patch 的 delta 之和`

LLM 逐字符算累积漂移**几乎必错**，而且错得很隐蔽——
可能只偏移 2 个字符，导致 Citation 的 `quoted_text` 与原文对不上，排查成本极高。

**所以**：
- LLM 产出 → **归一化操作提议清单**（`patches[]`，只填 orig 侧坐标）
- Python 执行 → 应用 patch、计算 new 侧坐标、校验几何约束、重映射 block span

## Workflow（声明式）

### 1. 读输入
`runs/<run_id>/02-parsed.json`（`ParsedDocument`）。
取 `text` 作为工作对象（**orig 坐标系**）。

### 2. 产出 patch 提议 `patches[]`

四类清洗项（源自 `normalize.py::_normalize_text` 逐字符扫描逻辑）：

| kind | 规则 | 示例 |
|---|---|---|
| `control` | 剥离 `ord(c) < 0x20` 的字符，**保留 `\n` 与 `\t`** | `\x0b` → `` |
| `whitespace` | 连续 2 个以上空格折叠为 1 个 | `"a   b"` → `"a b"` |
| `unicode_nfc` | 逐字符做 Unicode NFC 归一化 | 分解序列 → 组合字符 |
| `*_canonicalize` | 规则实体归一（见下表） | 见下 |

每条 patch 只填：`orig_start` / `orig_end` / `kind` / `original` / `replacement`。
`new_start` / `new_end` 留 `null`，Python 回填。

> **`orig_start`/`orig_end` 的坐标系**：指「应用此前所有 patch 之后的中间态文本」中的下标。
> 实操建议：按**从左到右的顺序**依次给出 patch，这样每条的 orig 坐标就是
> 「上一条 patch 处理完之后」的位置 —— 与 Python 的逐字符扫描顺序一致。

### 3. 规则实体归一（`*_canonicalize`）

| 类型 | 正则（源自 `_RULES`） | 归一化函数 |
|---|---|---|
| `DATE` | `\d{4}年\d{1,2}月(?:\d{1,2}日?)?` \| `\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?` \| `\d{1,2}月\d{1,2}日?` \| `\d{4}年` \| `\d{1,2}月` | 年→`-`、月→`-`、日→删（`2024年3月5日` → `2024-3-5`） |
| `MONEY` | `[¥$€£]\s?\d[\d,]*(?:\.\d+)?\s*(?:万\|亿)?\s*(?:元\|美元\|欧元\|英镑)?` 或纯数字+单位 | 不改写 |
| `PERCENT` | `\d+(?:\.\d+)?%` | 去掉尾部 `%` |
| `QUANTITY` | `\d[\d,]*(?:\.\d+)?\s?(?:kg\|千克\|公里\|km\|米\|吨\|升\|毫升\|平方米\|平方厘米\|个\|摄氏度\|°C\|度)` | 不改写 |

**重叠消解**（源自 `_extract_rule_mentions`）：多个规则命中同一区间时，
按 `(start, -length)` 排序、**长者优先**、跳过被覆盖者。

这些实体写进 `mentions[]`，`mention_id` 留空由 Python 补算。

### 4. 落盘（提议态）
写 `runs/<run_id>/03-normalized.json`，`text` 仍放**原文**（未应用 patch）。
遵循 `contracts/normalized.schema.json`。

### 5. 调 Python 应用 patch

```bash
python -m smini.cli normalize --apply \
  --in runs/<run_id>/03-normalized.json \
  --out runs/<run_id>/03-normalized.json
```

Python 做的事：
1. 按序应用 patch，计算 `new_start` / `new_end`
2. **校验三条几何约束**（`validate_patches`）—— 违反即报错，不静默
3. 用 `_forward_offset` 把 `blocks[]` 的 span 从 orig 重映射到 norm 坐标系
4. 用 `text` 字段回填归一化后的文本
5. 补算 `mentions[].mention_id`

**失败则回退**：`python -m smini.cli fallback normalize ...`（确定性清洗），
登记 `Degradation(component="normalize.span", kind=parse_failed, ...)`。

## 输出契约

`runs/<run_id>/03-normalized.json` → `NormalizedDocument`
字段：`doc_id` / `source` / `text` / `blocks[]` / `patches[]` / `mentions[]` / `warnings[]`

**Python 执行后**：`text` 已是归一化文本，`patches[]` 各条补齐 new 侧坐标，
`blocks[]` 的 span 已重映射。

## 自检清单

- [ ] patch 按 orig 从左到右排列
- [ ] 每条 patch 的 `original` 确实位于 `[orig_start, orig_end)`
- [ ] 剥离类操作的 `replacement` 是空串
- [ ] 日期类 patch 的 `replacement` 是 `YYYY-M-D` 形式
- [ ] 没有手动填 `new_start` / `new_end`（那是 Python 的活）
- [ ] 若 Python 报几何约束违反，走 fallback 并登记 Degradation
