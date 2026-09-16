"""smini.runtime — Skill 调用的确定性运行时。

extract-only 架构下，Python 只做 Skill 确实做不到的事（3 类）：

    ids       sha256 内容寻址 ID 补算 —— LLM 算不出，ID 错则幂等崩塌
    validate  JSON Schema 校验 + 领域约束 + 软引用检查 —— 机械判断，不该花 token
    graph     build 建图（惰性锚点消费层）—— 幂等需位运算级一致

每个命令都是「JSON 文件进 → JSON 文件出」的纯函数式变换，
因为 Skill 在 agent 循环里运行，只能通过文件通信。

用法
----
    python -m smini.cli ids      extract --in runs/x/04-extraction.json
    python -m smini.cli validate extract --in runs/x/04-extraction.json
    python -m smini.cli graph    build --in runs/x/04-extraction.json --out runs/x/05-graph.json
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
    "extraction": "extraction.schema.json",
}

# 允许用 step 名当别名（Skill 里更自然：validate extract）
_ALIAS = {"extract": "extraction"}


def load_schema(name: str) -> dict:
    name = _ALIAS.get(name, name)
    fn = _SCHEMA_OF.get(name)
    if fn is None:
        raise ValueError(f"未知契约名 {name!r}，可选：{sorted(_SCHEMA_OF)}")
    p = _CONTRACTS_DIR / fn
    if not p.exists():
        raise FileNotFoundError(f"契约文件缺失：{p}")
    return json.loads(p.read_text(encoding="utf-8"))


def cmd_ids(what: str, inp: str, out: str | None = None) -> int:
    data = load_json(inp)
    target = out or inp
    n = 0

    if what == "extract":
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

    else:
        print(f"未知 ids 目标：{what}（可选 extract）", file=sys.stderr)
        return 2

    dump_json(target, data)
    print(f"[ids] 补算了 {n} 个 ID → {target}")
    return 0


def cmd_validate(what: str, inp: str) -> int:
    data = load_json(inp)
    errs: list[str] = []

    # Schema 校验（extract-only 架构只有 extraction 契约；extract 为别名）
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

    if errs:
        print(f"[validate] ✗ {what} 校验失败（{len(errs)} 项）：", file=sys.stderr)
        for e in errs[:20]:
            print(f"  - {e}", file=sys.stderr)
        return 1

    print(f"[validate] ✓ {what} 通过：{inp}")
    return 0


# --------------------------------------------------------------------------
# 命令：graph build —— 建图（惰性锚点消费层）
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


def run(argv: Sequence[str]) -> int:
    import argparse

    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, *rest = argv

    if cmd == "ids":
        p = argparse.ArgumentParser(prog="smini ids")
        p.add_argument("what", choices=["extract"])
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", default=None)
        a = p.parse_args(rest)
        return cmd_ids(a.what, a.inp, a.out)

    if cmd == "validate":
        p = argparse.ArgumentParser(prog="smini validate")
        p.add_argument("what", choices=sorted(_SCHEMA_OF) + sorted(_ALIAS))
        p.add_argument("--in", dest="inp", required=True)
        a = p.parse_args(rest)
        return cmd_validate(a.what, a.inp)

    if cmd == "graph":
        p = argparse.ArgumentParser(prog="smini graph")
        p.add_argument("what", choices=["build"])
        p.add_argument("--in", dest="inp", required=True)
        p.add_argument("--out", required=True)
        p.add_argument("--disambiguation", default=None)
        a = p.parse_args(rest)
        return cmd_graph_build(a.inp, a.out, a.disambiguation)

    print(f"未知子命令：{cmd}", file=sys.stderr)
    return 2
