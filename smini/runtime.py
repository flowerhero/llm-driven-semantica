"""smini.runtime — Skill 调用的确定性运行时。

Skill-First 架构下，Python 只做 Skill 确实做不到的事（5 类）：

    ids       sha256 内容寻址 ID —— LLM 算不出，ID 错则幂等崩塌
    spans     SpanPatch 坐标映射 + 几何校验 —— 逐字符精确，LLM 必错且错得隐蔽
    graph     建图 / 双时态 / 裁决 / 去重 —— 幂等需位运算级一致
    validate  JSON Schema 校验 + 保真比对 —— 机械判断，不该花 token
    retrieve  确定性检索 + Citation 组装 —— 不能靠 LLM 回忆图里有什么

每个命令都是「JSON 文件进 → JSON 文件出」的纯函数式变换，
因为 Skill 在 agent 循环里运行，只能通过文件通信。

用法
----
    python -m smini.cli ingest   --source "file:///a.md" --out runs/x/01-raw.json
    python -m smini.cli validate parse --in runs/x/02-parsed.json --orig runs/x/01-raw.json
    python -m smini.cli ids      extract --in runs/x/04-extraction.json
    python -m smini.cli normalize --apply --in runs/x/03-normalized.json --out runs/x/03-normalized.json
    python -m smini.cli graph    build --in runs/x/04-extraction.json --out runs/x/05-graph.json
    python -m smini.cli graph    qa    --in runs/x/05-graph.json --out runs/x/06-qa.json
    python -m smini.cli graph    store --in runs/x/06-qa.json --out runs/x/07-store.json
    python -m smini.cli deliver  --query "特斯拉 总部" --in runs/x/06-qa.json --out runs/x/08-package.json
    python -m smini.cli fallback extract --in runs/x/03-normalized.json --out runs/x/04-extraction.json
"""

from __future__ import annotations

import base64
import dataclasses
import datetime as _dt
import enum
import json
import re
import sys
import types as _pytypes
import typing
from pathlib import Path
from typing import Any, Sequence

from . import types as T
from .ids import (
    action_id,
    chunk_id,
    constraint_id,
    content_hash,
    doc_id,
    edge_id,
    entity_id,
    flow_id,
    function_id,
    mention_id,
    permission_id,
    process_id,
    rule_id,
    stable_id,
    state_id,
    state_machine_id,
    step_id,
    temporal_id,
    transition_id,
    triplet_id,
)
from .protocols import RunContext
from .types import to_dict

# --------------------------------------------------------------------------
# JSON I/O
# --------------------------------------------------------------------------


def load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def dump_json(path: str, data: Any) -> None:
    p = Path(path)
    if p.parent and str(p.parent) not in ("", "."):
        p.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# --------------------------------------------------------------------------
# 反序列化：dict → dataclass（to_dict 的反向）
# --------------------------------------------------------------------------


def revive(cls: type, data: Any) -> Any:
    """把 JSON dict 还原成 dataclass 实例（递归）。

    处理：嵌套 dataclass / Enum / datetime / bytes(base64) / tuple / list / dict。
    """
    if data is None:
        return None
    if not dataclasses.is_dataclass(cls):
        return _revive_scalar(cls, data)

    if not isinstance(data, dict):
        raise TypeError(f"期望 dict 来构造 {cls.__name__}，得到 {type(data).__name__}")

    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue  # 用 dataclass 默认值
        kwargs[f.name] = _revive_field(hints.get(f.name, Any), data[f.name])
    return cls(**kwargs)


def _revive_scalar(cls: Any, data: Any) -> Any:
    if isinstance(cls, type) and issubclass(cls, enum.Enum):
        return cls(data)
    if cls is _dt.datetime:
        if isinstance(data, str):
            return _dt.datetime.fromisoformat(data.replace("Z", "+00:00"))
        return data
    if cls is bytes:
        if isinstance(data, str):
            return base64.b64decode(data)
        return data
    return data


def _revive_field(tp: Any, data: Any) -> Any:
    if data is None:
        return None

    origin = typing.get_origin(tp)
    args = typing.get_args(tp)

    # X | None
    if origin is _pytypes.UnionType or (args and type(None) in args):
        inner = [a for a in args if a is not type(None)]
        if len(inner) == 1:
            return _revive_field(inner[0], data)
        return data

    if origin in (list, Sequence):
        item = args[0] if args else Any
        return [_revive_field(item, d) for d in data]

    if origin is tuple:
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_revive_field(args[0], d) for d in data)
        return tuple(_revive_field(args[i], d) for i, d in enumerate(data))

    if origin is dict:
        vt = args[1] if len(args) > 1 else Any
        return {k: _revive_field(vt, v) for k, v in data.items()}

    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            return revive(tp, data)
        if issubclass(tp, enum.Enum):
            return tp(data)
        if tp is _dt.datetime:
            return _dt.datetime.fromisoformat(data.replace("Z", "+00:00")) if isinstance(data, str) else data
        if tp is bytes:
            return base64.b64decode(data) if isinstance(data, str) else data
    return data


# --------------------------------------------------------------------------
# 轻量 JSON Schema 校验器（draft-07 子集，零依赖）
# --------------------------------------------------------------------------


class SchemaError(Exception):
    """Schema 校验失败。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors[:5]))


def _type_ok(tp: str, val: Any) -> bool:
    if tp == "object":
        return isinstance(val, dict)
    if tp == "array":
        return isinstance(val, list)
    if tp == "string":
        return isinstance(val, str)
    if tp == "integer":
        return isinstance(val, int) and not isinstance(val, bool)
    if tp == "number":
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    if tp == "boolean":
        return isinstance(val, bool)
    if tp == "null":
        return val is None
    return True


def validate_against_schema(
    data: Any, schema: dict, path: str = "$", root: dict | None = None
) -> list[str]:
    """返回错误列表（空列表 = 通过）。支持 draft-07 常用关键字。"""
    errs: list[str] = []
    if root is None:
        root = schema

    if "$ref" in schema:
        # 只支持同文件 #/definitions/... 引用（从 root 解析）
        ref = schema["$ref"]
        if ref.startswith("#/definitions/"):
            target: Any = root
            for part in ref[len("#/definitions/"):].split("/"):
                target = (target.get("definitions") or {}).get(part, {})
            return validate_against_schema(data, target, path, root)
        return errs

    # type
    tp = schema.get("type")
    if tp is not None:
        tps = tp if isinstance(tp, list) else [tp]
        if not any(_type_ok(t, data) for t in tps):
            errs.append(f"{path}: 类型应为 {'|'.join(tps)}，实为 {type(data).__name__}")
            return errs

    # enum
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: 值 {data!r} 不在枚举 {schema['enum']} 内")

    # 字符串
    if isinstance(data, str):
        if "pattern" in schema and not re.search(schema["pattern"], data):
            errs.append(f"{path}: 不匹配 pattern {schema['pattern']}")
        if "minLength" in schema and len(data) < schema["minLength"]:
            errs.append(f"{path}: 长度 < {schema['minLength']}")

    # 数值
    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            errs.append(f"{path}: {data} < minimum {schema['minimum']}")
        if "maximum" in schema and data > schema["maximum"]:
            errs.append(f"{path}: {data} > maximum {schema['maximum']}")

    # 数组
    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            errs.append(f"{path}: 元素数 < {schema['minItems']}")
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            errs.append(f"{path}: 元素数 > {schema['maxItems']}")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for i, item in enumerate(data):
                # root 必须透传：items 常为 $ref，丢 root 会解析成空 schema
                # 使整个数组型契约（raw/parsed/...）的校验静默失效
                errs.extend(
                    validate_against_schema(item, item_schema, f"{path}[{i}]", root)
                )

    # 对象
    if isinstance(data, dict):
        for req in schema.get("required", []):
            if req not in data:
                errs.append(f"{path}: 缺必填字段 '{req}'")
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        if addl is False:
            for k in data:
                if k not in props:
                    errs.append(f"{path}: 出现未声明字段 '{k}'")
        elif isinstance(addl, dict):
            for k, v in data.items():
                if k not in props:
                    errs.extend(validate_against_schema(v, addl, f"{path}.{k}", root))
        for k, sub in props.items():
            if k in data:
                errs.extend(validate_against_schema(data[k], sub, f"{path}.{k}", root))

    return errs


_CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"

_SCHEMA_OF = {
    "raw": "raw.schema.json",
    "parsed": "parsed.schema.json",
    "normalized": "normalized.schema.json",
    "extraction": "extraction.schema.json",
    "graph": "graph.schema.json",
}

# 允许用 step 名当别名（Skill 里更自然：validate parse / validate extract）
_ALIAS = {"parse": "parsed", "extract": "extraction", "kg": "graph", "qa": "graph"}


def load_schema(name: str) -> dict:
    name = _ALIAS.get(name, name)
    fn = _SCHEMA_OF.get(name)
    if fn is None:
        raise ValueError(f"未知契约名 {name!r}，可选：{sorted(_SCHEMA_OF)}")
    p = _CONTRACTS_DIR / fn
    if not p.exists():
        raise FileNotFoundError(f"契约文件缺失：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# 命令：ingest
# --------------------------------------------------------------------------


def cmd_ingest(sources: Sequence[str], out: str) -> int:
    from .steps.ingest import resolve_source

    docs = [resolve_source(s) for s in sources]
    data = []
    for d in docs:
        dd = to_dict(d)
        dd["content"] = base64.b64encode(d.content).decode("ascii")
        data.append(dd)
    dump_json(out, data)
    print(f"[ingest] {len(data)} 个 RawDocument → {out}")
    for d in data:
        print(f"  doc_id={d['doc_id'][:16]}… uri={d['source']['uri']} bytes={d['size_bytes']}")
    return 0


# --------------------------------------------------------------------------
# 命令：ids —— 补算内容寻址 ID（LLM 绝不出 ID）
# --------------------------------------------------------------------------


def cmd_ids(what: str, inp: str, out: str | None = None) -> int:
    data = load_json(inp)
    target = out or inp
    n = 0

    if what == "raw":
        for i, d in enumerate(data):
            # content 必须无条件校验：即使 ID 已填过，content 损坏也说明数据不一致。
            # 若只在「ID 缺失」分支里解码，坏数据会因跳过校验而静默通过。
            try:
                raw = base64.b64decode(d.get("content", ""), validate=True)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"[ids] ✗ 第 {i} 个文档 content 非合法 base64：{exc}",
                    file=sys.stderr,
                )
                return 1
            if not d.get("checksum") or not d.get("doc_id"):
                uri = d.get("source", {}).get("uri", "")
                ch = content_hash(raw)
                d["checksum"] = ch
                d["source"]["checksum"] = ch
                d["size_bytes"] = len(raw)
                if not d.get("doc_id"):
                    d["doc_id"] = doc_id(uri, raw)
                n += 1

    elif what == "extract":
        doc_id_ = data.get("doc_id", "")
        for m in data.get("mentions", []):
            if not m.get("mention_id"):
                cs, ce = int(m.get("char_start", -1)), int(m.get("char_end", -1))
                if cs >= 0 and ce > cs:
                    m["mention_id"] = mention_id(doc_id_, cs, ce, m["text"])
                else:
                    # 契约即规则：span 未定位（尽力 find 失败）时，身份退化为
                    # canonical —— 同 canonical 跨文档收敛，避免每次算出新 ID
                    m["mention_id"] = stable_id(
                        "mention", m.get("normalized") or m.get("text", ""), prefix="m_"
                    )
                n += 1
        for c in data.get("chunks", []):
            if not c.get("chunk_id"):
                c["chunk_id"] = chunk_id(doc_id_, int(c["char_start"]), int(c["char_end"]))
                n += 1
        # 同时接受两种写法：LLM 给 surface 文本，确定性路径给 mention_id
        by_surface: dict[str, str] = {}
        for m in data.get("mentions", []):
            mid = m.get("mention_id", "")
            if mid:
                by_surface[m["text"]] = mid
                by_surface[mid] = mid
        skip = 0
        for r in data.get("relations", []):
            if r.get("relation_id"):
                continue
            subj_ref = r.get("subject_ref", "")
            obj_ref = r.get("object_ref", "")
            sid = by_surface.get(subj_ref, subj_ref)
            if obj_ref in by_surface:
                # 宾语指向另一实体
                r["relation_id"] = edge_id(
                    sid or subj_ref, r.get("predicate", ""), by_surface[obj_ref]
                )
                n += 1
            elif obj_ref:
                # 字面量宾语（DATE/MONEY 等）：走 object_literal 参数
                r["relation_id"] = edge_id(
                    sid or subj_ref, r.get("predicate", ""), None, obj_ref
                )
                n += 1
            else:
                # 宾语两侧全空：数据不完整，交 validate 去拦，此处不崩
                skip += 1
        if skip:
            print(
                f"[ids] 警告：{skip} 条 relation 缺 object_ref，无法补算 relation_id"
                "（应由 validate extract 拦下）",
                file=sys.stderr,
            )
        for t in data.get("triplets", []):
            if not t.get("triplet_id"):
                st = T.EntityType(t["subject_type"])
                sid = entity_id(st, t["subject"])
                okey = t["object"] if t.get("object_literal") else (
                    entity_id(T.EntityType(t["object_type"]), t["object"]) if t.get("object_type") else t["object"]
                )
                t["triplet_id"] = triplet_id(sid, t["predicate"], okey)
                n += 1
        # 业务规则（v3）：rule_id 由 (subject, condition, action, modality) 内容寻址
        for r in data.get("rules", []):
            if not r.get("rule_id"):
                r["rule_id"] = rule_id(
                    r.get("subject", ""), r.get("condition", ""),
                    r.get("action", ""), r.get("modality", "OTHER"),
                )
                n += 1
        # 业务流程（v4）：process_id / step_id / flow_id 全部内容寻址补算
        for p in data.get("processes", []):
            if not p.get("process_id"):
                p["process_id"] = process_id(p.get("name", ""))
                n += 1
            pid = p.get("process_id", "")
            for i, st in enumerate(p.get("steps", [])):
                if not st.get("step_id"):
                    st["step_id"] = step_id(pid, i, st.get("label", ""))
                    n += 1
            for f in p.get("flows", []):
                if not f.get("flow_id"):
                    # 兼容两种命名：宿主契约 from/to ↔ ExtractionResult from_index/to_index
                    fi = f.get("from_index", f.get("from", -1))
                    ti = f.get("to_index", f.get("to", -1))
                    f["flow_id"] = flow_id(
                        pid, int(fi), int(ti),
                        f.get("type", "SEQUENCE"), f.get("condition", ""),
                    )
                    n += 1
        # 预测决策本体（v6）：六件套 ID 全部内容寻址补算
        for sm in data.get("states", []):
            if not sm.get("sm_id"):
                sm["sm_id"] = state_machine_id(sm.get("object", ""), sm.get("name", ""))
                n += 1
            sid = sm.get("sm_id", "")
            for i, st in enumerate(sm.get("states", [])):
                if not st.get("state_id"):
                    st["state_id"] = state_id(sid, i, st.get("label", ""))
                    n += 1
            for tr in sm.get("transitions", []):
                if not tr.get("transition_id"):
                    # 兼容两种命名：宿主契约 from/to ↔ ExtractionResult from_index/to_index
                    fi = tr.get("from_index", tr.get("from", -1))
                    ti = tr.get("to_index", tr.get("to", -1))
                    tr["transition_id"] = transition_id(
                        sid, int(fi), int(ti),
                        tr.get("event", ""), tr.get("condition", ""),
                    )
                    n += 1
        for fn in data.get("functions", []):
            if not fn.get("function_id"):
                fn["function_id"] = function_id(
                    fn.get("name", ""), fn.get("subject", ""), fn.get("formula", ""),
                )
                n += 1
        for tm in data.get("temporal", []):
            if not tm.get("temporal_id"):
                tm["temporal_id"] = temporal_id(
                    tm.get("subject", ""), tm.get("kind", ""), tm.get("value", ""),
                )
                n += 1
        for ac in data.get("actions", []):
            if not ac.get("action_id"):
                ac["action_id"] = action_id(
                    ac.get("name", ""), ac.get("actor", ""),
                    ac.get("level", ""), ac.get("trigger", ""),
                )
                n += 1
        for cn in data.get("constraints", []):
            if not cn.get("constraint_id"):
                cn["constraint_id"] = constraint_id(
                    cn.get("subject", ""), cn.get("type", ""), cn.get("description", ""),
                )
                n += 1
        for pm in data.get("permissions", []):
            if not pm.get("permission_id"):
                pm["permission_id"] = permission_id(
                    pm.get("actor", ""), pm.get("action", ""), pm.get("effect", ""),
                )
                n += 1

    elif what == "graph":
        for e in (data.get("entities") or {}).values() if isinstance(data.get("entities"), dict) else []:
            if not e.get("entity_id"):
                e["entity_id"] = entity_id(e["entity_type"], e["canonical_name"])
                n += 1
        for versions in (data.get("edges") or {}).values() if isinstance(data.get("edges"), dict) else []:
            for e in versions:
                if not e.get("edge_id"):
                    e["edge_id"] = edge_id(
                        e.get("subject_id", ""),
                        e.get("predicate", ""),
                        e.get("object_id"),
                        e.get("object_literal"),
                    )
                    n += 1
    else:
        print(f"未知 ids 目标：{what}（可选 raw/extract/graph）", file=sys.stderr)
        return 2

    dump_json(target, data)
    print(f"[ids] 补算了 {n} 个 ID → {target}")
    return 0


# --------------------------------------------------------------------------
# 命令：spans —— 补算区块坐标（LLM 给文本与类型，Python 定位 span）
# --------------------------------------------------------------------------


def cmd_spans(what: str, inp: str, out: str | None = None) -> int:
    """补算 block 的 block_id 与 char_start/char_end。

    LLM 只需给出区块的 kind 与文本内容，坐标由 Python 精确定位 —— 逐字符算
    偏移 LLM 几乎必错，而且错得**自洽**（block.text 取自切片，校验查不出来），
    会静默污染下游所有溯源。
    """
    if what != "parse":
        print(f"未知 spans 目标：{what!r}（当前仅支持 parse）", file=sys.stderr)
        return 2

    data = load_json(inp)
    target = out or inp
    docs = data if isinstance(data, list) else [data]
    n_id = n_span = 0

    for d in docs:
        text = d.get("text", "") or ""
        doc_id_ = d.get("doc_id", "")
        search_from = 0
        for order, b in enumerate(d.get("blocks") or []):
            if not b.get("block_id"):
                b["block_id"] = stable_id("block", doc_id_, order, prefix="b_")
                n_id += 1
            if b.get("order") != order:
                b["order"] = order

            bt = b.get("text", "") or ""
            s, e = b.get("char_start"), b.get("char_end")
            ok = (isinstance(s, int) and isinstance(e, int)
                  and 0 <= s < e <= len(text) and text[s:e] == bt)
            if not ok:
                # 顺序定位：从上一个区块末尾起找。若每次都从头 find，
                # 重复段落会全部定位到第一处。
                i = text.find(bt, search_from)
                if i < 0:
                    i = text.find(bt)  # 回退：全文本找一次
                if i < 0:
                    print(
                        f"[spans] ✗ 区块 order={order} (kind={b.get('kind')}) "
                        f"的文本在 text 中找不到：{bt[:40]!r}",
                        file=sys.stderr,
                    )
                    return 1
                b["char_start"], b["char_end"] = i, i + len(bt)
                n_span += 1
            search_from = int(b["char_end"])

    dump_json(target, data)
    print(f"[spans] 补算 block_id {n_id} 个 / span {n_span} 个 → {target}")
    return 0


# --------------------------------------------------------------------------
# 命令：validate —— Schema + 领域约束
# --------------------------------------------------------------------------


# 文档级契约：定义放在 definitions，顶层按形态（单文档 / 批次数组）分发校验
_DOC_LEVEL = {"parsed": "ParsedDocument"}


def _check_parsed_doc(doc: dict, errs: list[str], path: str,
                      orig_text: str | None) -> None:
    """ParsedDocument 的领域约束（Schema 表达不了的部分）。"""
    text = doc.get("text", "") or ""
    blocks = doc.get("blocks") or []

    for i, b in enumerate(blocks):
        s, e = int(b.get("char_start", -1)), int(b.get("char_end", -1))
        if not (0 <= s < e <= len(text)):
            errs.append(f"{path}.blocks[{i}]: span 越界 [{s},{e}) 不在 0..{len(text)}")
        elif text[s:e] != b.get("text"):
            errs.append(
                f"{path}.blocks[{i}]: block.text 与 text[{s}:{e}] 不一致（保真失败）"
            )
        # 类型专属必填：table 必须有二维单元格，heading 必须有层级
        if b.get("kind") == "table" and not b.get("rows"):
            errs.append(f"{path}.blocks[{i}]: table 区块缺 rows（二维单元格）")
        if b.get("kind") == "heading" and b.get("level") is None:
            errs.append(f"{path}.blocks[{i}]: heading 区块缺 level")

    orders = [b.get("order") for b in blocks]
    if orders and orders != sorted(orders):
        errs.append(f"{path}.blocks: order 未全局单调递增")

    if orig_text is not None:
        diff = abs(len(text) - len(orig_text))
        thr = max(8, int(0.05 * len(orig_text)))
        if diff > thr:
            errs.append(
                f"{path}: 保真失败：解析文本长度 {len(text)} vs 原文 {len(orig_text)}，"
                f"偏差 {diff} > 阈值 {thr}"
            )


def cmd_validate(what: str, inp: str, orig: str | None = None) -> int:
    data = load_json(inp)
    errs: list[str] = []

    if what in ("graph", "kg", "qa") and isinstance(data, dict) and "entities" in data:
        # graph 契约兼容两种形态：QAResult（含 graph 键）与裸 KnowledgeGraph
        root = load_schema("graph")
        errs.extend(validate_against_schema(
            data, root["definitions"]["KnowledgeGraph"], "$", root))
    elif what in _DOC_LEVEL:
        # 顶层 type 为 ["object","array"] 且无 properties，按实际形态分发；
        # 数组时逐元素校验（此前直接 .get() 会崩，多文档批次无法校验）
        root = load_schema(what)
        sub = root["definitions"][_DOC_LEVEL[what]]
        docs = data if isinstance(data, list) else [data]
        raws = load_json(orig) if orig else None
        if raws is not None and not isinstance(raws, list):
            raws = [raws]
        for i, doc in enumerate(docs):
            path = "$" if isinstance(data, dict) else f"$[{i}]"
            errs.extend(validate_against_schema(doc, sub, path, root))
            ot = None
            if raws is not None and i < len(raws):
                r0 = raws[i] or {}
                rb = base64.b64decode(r0.get("content", "")) if isinstance(r0.get("content"), str) else b""
                ot = rb.decode("utf-8", errors="replace")
            _check_parsed_doc(doc, errs, path, ot)
    else:
        root = load_schema(what)
        errs.extend(validate_against_schema(data, root, "$", root))

    # 领域约束（Schema 表达不了的）
    if what == "extraction":
        for i, m in enumerate(data.get("mentions", [])):
            s, e = int(m.get("char_start", -1)), int(m.get("char_end", -1))
            # 契约即规则：char_start=-1 表示「未定位」（尽力 find 失败），允许；
            # 已定位但区间非法才报错。
            if s >= 0 and e <= s:
                errs.append(f"$.mentions[{i}]: 非法 span [{s},{e})")
        for i, t in enumerate(data.get("triplets", [])):
            if t.get("object_type") in ("DATE", "MONEY", "PERCENT", "QUANTITY") and not t.get("object_literal"):
                errs.append(
                    f"$.triplets[{i}]: object_type={t['object_type']} 但 object_literal 不为 true"
                )
        for i, m in enumerate(data.get("mentions", [])):
            if not str(m.get("mention_id", "")).strip():
                errs.append(f"$.mentions[{i}]: mention_id 为空（应先跑 ids extract）")
        for i, r in enumerate(data.get("rules", [])):
            if not str(r.get("rule_id", "")).strip():
                errs.append(f"$.rules[{i}]: rule_id 为空（应先跑 ids extract）")
            if not str(r.get("action", "")).strip():
                errs.append(f"$.rules[{i}]: action 为空（契约要求 action 必有）")
        # 业务流程（v4）领域约束：ID 非空 + flow 下标越界校验
        for i, p in enumerate(data.get("processes", [])):
            if not str(p.get("process_id", "")).strip():
                errs.append(f"$.processes[{i}]: process_id 为空（应先跑 ids extract）")
            n_steps = len(p.get("steps", []))
            for j, st in enumerate(p.get("steps", [])):
                if not str(st.get("step_id", "")).strip():
                    errs.append(f"$.processes[{i}].steps[{j}]: step_id 为空（应先跑 ids extract）")
                if not str(st.get("label", "")).strip():
                    errs.append(f"$.processes[{i}].steps[{j}]: label 为空（契约要求 label 必有）")
            for j, f in enumerate(p.get("flows", [])):
                if not str(f.get("flow_id", "")).strip():
                    errs.append(f"$.processes[{i}].flows[{j}]: flow_id 为空（应先跑 ids extract）")
                # 兼容两种命名：宿主契约 from/to ↔ ExtractionResult from_index/to_index
                fi = f.get("from_index", f.get("from"))
                ti = f.get("to_index", f.get("to"))
                if not (isinstance(fi, int) and isinstance(ti, int)
                        and 0 <= fi < n_steps and 0 <= ti < n_steps):
                    errs.append(
                        f"$.processes[{i}].flows[{j}]: 下标越界 "
                        f"from={fi} to={ti}（steps 共 {n_steps} 个）"
                    )
        # 预测决策本体（v6）领域约束：六件套 ID 非空 + 状态机/迁移下标校验
        for i, sm in enumerate(data.get("states", [])):
            if not str(sm.get("sm_id", "")).strip():
                errs.append(f"$.states[{i}]: sm_id 为空（应先跑 ids extract）")
            n_states = len(sm.get("states", []))
            for j, st in enumerate(sm.get("states", [])):
                if not str(st.get("state_id", "")).strip():
                    errs.append(f"$.states[{i}].states[{j}]: state_id 为空（应先跑 ids extract）")
                if not str(st.get("label", "")).strip():
                    errs.append(f"$.states[{i}].states[{j}]: label 为空（契约要求 label 必有）")
            for j, tr in enumerate(sm.get("transitions", [])):
                if not str(tr.get("transition_id", "")).strip():
                    errs.append(f"$.states[{i}].transitions[{j}]: transition_id 为空（应先跑 ids extract）")
                fi = tr.get("from_index", tr.get("from"))
                ti = tr.get("to_index", tr.get("to"))
                if not (isinstance(fi, int) and isinstance(ti, int)
                        and 0 <= fi < n_states and 0 <= ti < n_states):
                    errs.append(
                        f"$.states[{i}].transitions[{j}]: 下标越界 "
                        f"from={fi} to={ti}（states 共 {n_states} 个）"
                    )
        for i, fn in enumerate(data.get("functions", [])):
            if not str(fn.get("function_id", "")).strip():
                errs.append(f"$.functions[{i}]: function_id 为空（应先跑 ids extract）")
            if not str(fn.get("formula", "")).strip():
                errs.append(f"$.functions[{i}]: formula 为空（契约要求 formula 必有）")
        for i, tm in enumerate(data.get("temporal", [])):
            if not str(tm.get("temporal_id", "")).strip():
                errs.append(f"$.temporal[{i}]: temporal_id 为空（应先跑 ids extract）")
        for i, ac in enumerate(data.get("actions", [])):
            if not str(ac.get("action_id", "")).strip():
                errs.append(f"$.actions[{i}]: action_id 为空（应先跑 ids extract）")
        for i, cn in enumerate(data.get("constraints", [])):
            if not str(cn.get("constraint_id", "")).strip():
                errs.append(f"$.constraints[{i}]: constraint_id 为空（应先跑 ids extract）")
            if not str(cn.get("description", "")).strip():
                errs.append(f"$.constraints[{i}]: description 为空（契约要求 description 必有）")
        for i, pm in enumerate(data.get("permissions", [])):
            if not str(pm.get("permission_id", "")).strip():
                errs.append(f"$.permissions[{i}]: permission_id 为空（应先跑 ids extract）")

        # v7 软引用检查（借鉴对方一致性门禁，但降级为 warning 不阻断）：
        # 抽取端引用本就是宽松语义，找不到引用只提示、不判失败。
        warns: list[str] = []
        proc_names = {str(p.get("name", "")).strip() for p in data.get("processes", []) if p.get("name")}
        fn_names = {str(f.get("name", "")).strip() for f in data.get("functions", []) if f.get("name")}
        mention_names = {str(m.get("normalized", "")).strip() for m in data.get("mentions", []) if m.get("normalized")}
        for i, r in enumerate(data.get("rules", [])):
            for rb in (r.get("reused_by") or []):
                if rb and rb not in proc_names and rb not in fn_names:
                    warns.append(f"$.rules[{i}].reused_by: {rb!r} 未匹配到任何流程/函数名（软警告）")
        for i, p in enumerate(data.get("processes", [])):
            for j, st in enumerate(p.get("steps", [])):
                sr = (st.get("sub_process_ref") or "").strip()
                if sr and sr not in proc_names:
                    warns.append(f"$.processes[{i}].steps[{j}].sub_process_ref: {sr!r} 未匹配到任何流程名（软警告）")
        for i, pm in enumerate(data.get("permissions", [])):
            for field in ("actor", "role"):
                v = (pm.get(field) or "").strip()
                if v and v not in mention_names:
                    warns.append(f"$.permissions[{i}].{field}: {v!r} 未匹配到任何实体 canonical（软警告）")
        for i, ac in enumerate(data.get("actions", [])):
            v = (ac.get("actor") or "").strip()
            if v and v not in mention_names:
                warns.append(f"$.actions[{i}].actor: {v!r} 未匹配到任何实体 canonical（软警告）")
        for w in warns:
            print(f"[validate] ⚠ {w}", file=sys.stderr)

    elif what == "normalized":
        for i, p in enumerate(data.get("patches", [])):
            s, e = int(p.get("orig_start", -1)), int(p.get("orig_end", -1))
            if s < 0 or e < s:
                errs.append(f"$.patches[{i}]: 非法 orig span [{s},{e})")
            orig_text = data.get("text", "")
            if 0 <= s < e <= len(orig_text) and orig_text[s:e] != p.get("original"):
                errs.append(f"$.patches[{i}]: original 与 text[{s}:{e}] 不一致")

    if errs:
        print(f"[validate] ✗ {what} 校验失败（{len(errs)} 项）：", file=sys.stderr)
        for e in errs[:20]:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"[validate] ✓ {what} 通过：{inp}")
    return 0


# --------------------------------------------------------------------------
# 命令：normalize --apply —— 执行 LLM 提议的 patch（坐标由 Python 算）
# --------------------------------------------------------------------------


def cmd_normalize_apply(inp: str, out: str | None = None) -> int:
    data = load_json(inp)
    target = out or inp
    text = data.get("text", "")
    proposals = data.get("patches", []) or []

    # 按 orig_start 排序（LLM 应已排好，这里兜底）
    proposals = sorted(proposals, key=lambda p: (int(p.get("orig_start", 0)), int(p.get("orig_end", 0))))

    parts: list[str] = []
    applied: list[dict] = []
    cursor = 0      # 中间态文本的当前位置
    cum = 0         # 累积漂移

    for p in proposals:
        s, e = int(p["orig_start"]), int(p["orig_end"])
        original = p.get("original", "")
        replacement = p.get("replacement", "")
        if s < cursor:
            # 与已应用的区间重叠 → 跳过并告警
            data.setdefault("warnings", []).append(
                f"patch 区间 [{s},{e}) 与已应用区间重叠（cursor={cursor}），已跳过"
            )
            continue
        parts.append(text[cursor:s])
        parts.append(replacement)
        new_s = s + cum
        new_e = new_s + len(replacement)
        cum += len(replacement) - (e - s)
        applied.append({
            "orig_start": s, "orig_end": e,
            "new_start": new_s, "new_end": new_e,
            "kind": p.get("kind", "other"),
            "original": original, "replacement": replacement,
        })
        cursor = e

    parts.append(text[cursor:])
    norm = "".join(parts)

    # 几何校验（types.validate_patches 的三条约束）
    patches = [revive(T.SpanPatch, p) for p in applied]
    geo_err: str | None = None
    try:
        T.validate_patches(patches)
    except Exception as exc:  # noqa: BLE001
        geo_err = f"SpanPatch 几何约束违反：{exc}"

    if geo_err:
        print(f"[normalize] ✗ {geo_err}", file=sys.stderr)
        data.setdefault("warnings", []).append(geo_err)
        dump_json(target, data)
        return 1

    data["text"] = norm
    data["patches"] = applied

    # block span 重映射（orig → norm），越界保护
    def fwd(off: int, at_end: bool = False) -> int:
        res = off
        for p in patches:
            if p.orig_end <= off:
                res += p.delta
            elif p.orig_start <= off < p.orig_end:
                return p.new_end if at_end else p.new_start
            else:
                break
        return max(res, 0)

    for b in data.get("blocks", []) or []:
        s, e = int(b.get("char_start", 0)), int(b.get("char_end", 0))
        ns, ne = fwd(s), fwd(e, at_end=True)
        if 0 <= ns < ne <= len(norm):
            b["char_start"], b["char_end"] = ns, ne
        # 越界则保留原坐标（不静默错位）

    # 规则实体的 mention_id 补算
    did = data.get("doc_id", "")
    for m in data.get("mentions", []) or []:
        if not m.get("mention_id"):
            m["mention_id"] = mention_id(
                did, int(m.get("char_start", 0)), int(m.get("char_end", 0)), m.get("text", "")
            )

    dump_json(target, data)
    print(f"[normalize] ✓ 应用 {len(applied)} 个 patch：{len(text)} → {len(norm)} 字符 → {target}")
    return 0


# --------------------------------------------------------------------------
# 命令：graph build / qa / store
# --------------------------------------------------------------------------


def _ctx() -> RunContext:
    return RunContext()


def cmd_graph_build(inp: str, out: str, disambiguation: str | None = None) -> int:
    """建图：复用已验证的确定性 BuildKGStep。"""
    from .steps.build_kg import BuildKGStep

    raw = load_json(inp)
    items = raw if isinstance(raw, list) else [raw]
    extractions = [revive(T.ExtractionResult, d) for d in items]

    # 消歧：让若干 surface 共享 canonical_name → 按 ID 自然归并
    merged = 0
    if disambiguation:
        sug = load_json(disambiguation)
        for grp in (sug.get("disambiguation", sug).get("groups", []) if isinstance(sug, dict) else []):
            canonical = grp.get("canonical", "")
            etype = grp.get("entity_type", "")
            for er in extractions:
                for m in er.mentions:
                    if m.text in grp.get("surfaces", []) and str(m.entity_type) in (etype, etype.upper()):
                        if m.normalized != canonical:
                            m.normalized = canonical
                            merged += 1
                for t in er.triplets:
                    if t.subject in grp.get("surfaces", []):
                        t.subject = canonical

    g = BuildKGStep().transform(extractions, _ctx())
    dump_json(out, to_dict(g))
    s = g.stats
    print(f"[graph build] 实体 {s.entity_count} / 边 {s.edge_count} "
          f"(字面量 {s.literal_edge_count}，孤立 {s.orphan_entity_count})，消歧归并 {merged} → {out}")
    return 0


def cmd_graph_qa(inp: str, out: str, suggestions: str | None = None) -> int:
    """质检：冲突检测 + 去重，产出已修复图谱。"""
    from .steps.qa import QAStep

    g = revive(T.KnowledgeGraph, load_json(inp))
    ctx = _ctx()

    # LLM 同义谓词归并 → 注入 ctx，QAStep 内部会读取
    pred_norm: dict[str, str] = {}
    semantic_conflicts: list[dict] = []
    if suggestions:
        sug = load_json(suggestions)
        for grp in (sug.get("predicate_normalization", {}).get("groups", []) if isinstance(sug, dict) else []):
            for v in grp.get("variants", []):
                pred_norm[v] = grp.get("canonical", v)
        semantic_conflicts = (sug.get("conflicts", []) if isinstance(sug, dict) else []) or []
    if pred_norm:
        ctx.config["predicate_normalization"] = pred_norm

    result = QAStep().transform(g, ctx)

    # LLM 报的语义冲突：Python 侧未能自动覆盖的，合并进来
    if semantic_conflicts:
        have = {(c.subject_id, c.predicate) for c in result.conflicts}
        for c in semantic_conflicts:
            key = (c.get("subject_id"), c.get("predicate"))
            if key in have:
                continue
            try:
                result.conflicts.append(revive(T.Conflict, c))
                result.metrics.unresolved_count += 0 if c.get("resolution") else 1
            except Exception:  # noqa: BLE001
                continue
        result.metrics.conflict_count = len(result.conflicts)

    dump_json(out, to_dict(result))
    m = result.metrics
    print(f"[graph qa] 冲突 {m.conflict_count} / 未决 {m.unresolved_count} / "
          f"重复集群 {m.duplicate_cluster_count} / 合并 {m.entities_merged} → {out}")
    print(f"  passed={'true' if m.unresolved_count == 0 else 'false'}"
          f"{'' if m.unresolved_count == 0 else '  ★ 不得进入 Store'}")
    return 0


def cmd_graph_store(inp: str, out: str, backend: str = "memory") -> int:
    """落库：强制 upsert 语义。"""
    from .steps.qa import QAStep  # noqa: F401  (保证 import 顺序无副作用)
    from .steps.store import StoreStep

    data = load_json(inp)
    # 兼容：既接受 QAResult（含 graph），也接受裸 KnowledgeGraph
    if isinstance(data, dict) and "graph" in data:
        if int(data.get("metrics", {}).get("unresolved_count", 0)) > 0:
            print("[graph store] ✗ QA 存在未裁决冲突，拒绝落库", file=sys.stderr)
            return 1
        graph = data["graph"]
    else:
        graph = data

    g = revive(T.KnowledgeGraph, graph)
    # StoreStep 吃的是 QAResult（含 .graph），裸图需包一层
    qa = revive(T.QAResult, data) if isinstance(data, dict) and "graph" in data else T.QAResult(graph=g)
    ctx = _ctx()

    from .stores import MemoryGraphStore
    store = MemoryGraphStore()

    # json 后端：先加载已有库，让 upsert 的「更新」分支真正生效
    # （这样跨进程复跑也能验证幂等：第二次 written 应为 0）
    lib_path: Path | None = None
    if backend == "json":
        lib_path = Path(out).with_name("graph-store.json")
        if lib_path.exists():
            try:
                old = revive(T.KnowledgeGraph, load_json(str(lib_path)))
                for e in old.entities.values():
                    store.entities[e.entity_id] = e
                for k, vs in old.edges.items():
                    store.edges[k] = list(vs)
                ctx.log("info", f"store: 已从 {lib_path} 载入现有库")
            except Exception as exc:  # noqa: BLE001
                print(f"[graph store] 载入现有库失败（按空库处理）：{exc}", file=sys.stderr)

    receipt = StoreStep(store=store).transform(qa, ctx)
    # 回执要如实反映实际使用的后端（store.backend 恒为 memory；frozen 需 replace）
    if receipt.backend != backend:
        receipt = dataclasses.replace(receipt, backend=backend)

    if lib_path is not None:
        dump_json(str(lib_path), {
            "entities": {k: to_dict(v) for k, v in store.entities.items()},
            "edges": {k: [to_dict(e) for e in vs] for k, vs in store.edges.items()},
        })

    dump_json(out, to_dict(receipt))
    r = receipt
    print(f"[graph store] backend={r.backend} 实体 +{r.entities_written}/~{r.entities_updated} "
          f"边 +{r.edges_written}/~{r.edges_updated} idempotent={r.idempotent} → {out}")
    return 0


# --------------------------------------------------------------------------
# 命令：deliver —— 确定性检索 + Citation
# --------------------------------------------------------------------------


def cmd_deliver(query: str, inp: str, out: str, answer: str | None = None) -> int:
    from .steps.deliver import DeliverStep

    data = load_json(inp)
    graph = data.get("graph", data) if isinstance(data, dict) else data
    g = revive(T.KnowledgeGraph, graph)
    ctx = _ctx()
    # DeliverStep 从 ctx.config["deliver"]["query"] 读查询
    ctx.config["deliver"] = {"query": query}

    # 它吃的是 QAResult（含 .graph），裸图需包一层
    qa = revive(T.QAResult, data) if isinstance(data, dict) and "graph" in data else T.QAResult(graph=g)
    pkg = DeliverStep().transform(qa, ctx)
    if answer is not None:
        pkg.answer = answer
    dump_json(out, to_dict(pkg))
    print(f"[deliver] query={query!r} → {len(pkg.facts)} 条事实 / "
          f"{len(pkg.citations)} 条引用 → {out}")
    return 0


# --------------------------------------------------------------------------
# 命令：fallback —— 确定性兜底（Skill/LLM 不可用时）
# --------------------------------------------------------------------------


def cmd_fallback(step: str, inp: str, out: str | None = None) -> int:
    """走确定性实现，并在产物里登记 Degradation。"""
    target = out or inp
    data = load_json(inp)
    ctx = _ctx()
    result: Any = None
    component = f"{step}.llm"

    if step == "parse":
        from .steps.parse import ParseStep
        raws = data if isinstance(data, list) else [data]
        docs = [revive(T.RawDocument, _b64_to_bytes(d)) for d in raws]
        result = ParseStep().transform(docs, ctx)

    elif step == "normalize":
        from .steps.normalize import NormalizeStep
        docs = data if isinstance(data, list) else [data]
        parsed = [revive(T.ParsedDocument, d) for d in docs]
        result = NormalizeStep().transform(parsed, ctx)

    elif step == "extract":
        print("[fallback] ✗ 零规则架构已删除确定性抽取：extract 必须走 LLM 或 Skill",
              file=sys.stderr)
        return 2

    else:
        print(f"fallback 暂不支持 step={step!r}（可选 parse/normalize/extract）", file=sys.stderr)
        return 2

    out_data = to_dict(result)
    if isinstance(out_data, list):
        out_data = out_data[0] if len(out_data) == 1 else out_data

    # 登记 Degradation（降级必须出声）
    deg = {
        "component": component,
        "kind": "unavailable",
        "reason": "Skill/LLM 路径不可用，回退确定性实现",
        "fallback": "deterministic.v1",
        "impact": "覆盖率低于 LLM 路径（仅规则/正则能识别的模式）",
        "recoverable": True,
    }
    if isinstance(out_data, dict):
        out_data.setdefault("degradations", []).append(deg)
        stats = out_data.setdefault("stats", {})
        if isinstance(stats, dict):
            stats["mode"] = "deterministic"
        if step == "parse":
            out_data.setdefault("warnings", []).append("已回退确定性解析")

    dump_json(target, out_data)
    print(f"[fallback] {step} 走确定性路径，已登记 Degradation → {target}")
    return 0


def _b64_to_bytes(d: dict) -> dict:
    """RawDocument 的 content 在 JSON 里是 base64，还原成 bytes。"""
    if isinstance(d, dict) and isinstance(d.get("content"), str):
        d = dict(d)
        d["content"] = base64.b64decode(d["content"])
    return d


# --------------------------------------------------------------------------
# 分发
# --------------------------------------------------------------------------


def run(argv: Sequence[str]) -> int:
    import argparse

    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, *rest = argv

    if cmd == "ingest":
        p = argparse.ArgumentParser(prog="smini ingest")
        p.add_argument("--source", action="append", default=[], required=True)
        p.add_argument("--out", required=True)
        a = p.parse_args(rest)
        return cmd_ingest(a.source, a.out)

    if cmd == "ids":
        p = argparse.ArgumentParser(prog="smini ids")
        p.add_argument("what", choices=["raw", "extract", "graph"])
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", default=None)
        a = p.parse_args(rest)
        return cmd_ids(a.what, a.inp, a.out)

    if cmd == "spans":
        p = argparse.ArgumentParser(prog="smini spans")
        p.add_argument("what", choices=["parse"])
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", default=None)
        a = p.parse_args(rest)
        return cmd_spans(a.what, a.inp, a.out)

    if cmd == "validate":
        p = argparse.ArgumentParser(prog="smini validate")
        p.add_argument("what", choices=sorted(_SCHEMA_OF) + sorted(_ALIAS))
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--orig", default=None, help="原始文件（parse 保真比对用）")
        a = p.parse_args(rest)
        return cmd_validate(a.what, a.inp, a.orig)

    if cmd == "normalize":
        p = argparse.ArgumentParser(prog="smini normalize")
        p.add_argument("--apply", action="store_true", required=True)
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", default=None)
        a = p.parse_args(rest)
        return cmd_normalize_apply(a.inp, a.out)

    if cmd == "graph":
        p = argparse.ArgumentParser(prog="smini graph")
        p.add_argument("what", choices=["build", "qa", "store"])
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--disambiguation", default=None)
        p.add_argument("--suggestions", default=None)
        p.add_argument("--backend", default="memory")
        a = p.parse_args(rest)
        if a.what == "build":
            return cmd_graph_build(a.inp, a.out, a.disambiguation)
        if a.what == "qa":
            return cmd_graph_qa(a.inp, a.out, a.suggestions)
        return cmd_graph_store(a.inp, a.out, a.backend)

    if cmd == "deliver":
        p = argparse.ArgumentParser(prog="smini deliver")
        p.add_argument("--query", required=True)
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--answer", default=None)
        a = p.parse_args(rest)
        return cmd_deliver(a.query, a.inp, a.out, a.answer)

    if cmd == "fallback":
        p = argparse.ArgumentParser(prog="smini fallback")
        p.add_argument("step", choices=["parse", "normalize", "extract"])
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", default=None)
        a = p.parse_args(rest)
        return cmd_fallback(a.step, a.inp, a.out)

    print(f"未知子命令：{cmd}", file=sys.stderr)
    return 2
