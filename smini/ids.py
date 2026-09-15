"""smini.ids — 确定性 ID 生成。

这是整个项目**最关键的一个文件**，它直接修复 semantica 原版最致命的缺陷。

原版做法（semantica/kg/graph_builder.py:608）::

    entity_id = str(hash(str(item)))

问题：Python 的 `hash()` 对 str 是**带随机盐**的（PYTHOHASHSEED 默认随机）。
同一个实体名，在两次不同的进程里会算出两个不同的 ID。后果是：
  * 增量构建时，同一批数据跑第二遍会生成一批全新的节点 —— 图谱无限膨胀
  * 跨进程 / 跨机器的流水线无法对齐
  * 测试结果不可复现

本模块改用 sha256。代价是比 hash() 慢约一个数量级，但换来**跨进程、
跨机器、跨版本**的严格幂等性。对知识图谱这种"ID 即身份"的场景，
这个交换是唯一正确的选择。

分隔约定
--------
拼接多个字段时用 ``\\x1f``（ASCII Unit Separator）分隔，而不是 ``:`` 或
``|`` 这类可能出现在业务字符串里的字符。否则
``("a:b", "c")`` 和 ``("a", "b:c")`` 会碰撞成同一个 ID。
"""

from __future__ import annotations

import hashlib
from typing import Any

#: 字段分隔符。ASCII 31 (Unit Separator)，业务文本里几乎不可能出现。
SEP = "\x1f"

#: 默认截取长度。16 个 hex 字符 = 64 bit。
#: 对 10^6 量级的实体，碰撞概率约 2.7e-8（生日界），足够安全且可读。
DEFAULT_LEN = 16


def _stringify(value: Any) -> str:
    """把任意字段值转成稳定字符串。

    刻意不依赖 ``repr()`` —— 不同 Python 版本对某些对象的 repr 会变。
    """
    if value is None:
        return "\x00"  # 明确区分 None 与空串
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"  # 不用 "True"/"False"，避免大小写歧义
    if isinstance(value, (int, float)):
        # 1 和 1.0 认为是同一个值，统一走 float 归一化
        return f"{float(value):.10g}"
    if isinstance(value, (list, tuple)):
        return SEP.join(_stringify(v) for v in value)
    if isinstance(value, dict):
        # 按 key 排序，保证 dict 顺序不影响 ID
        return SEP.join(f"{k}={_stringify(value[k])}" for k in sorted(value))
    return str(value)


def stable_id(*parts: Any, prefix: str = "", length: int = DEFAULT_LEN) -> str:
    """生成跨进程稳定的确定性 ID。

    Args:
        *parts: 参与哈希的字段，按顺序拼接。
        prefix: 可选前缀，用于让 ID 自解释（如 ``"e_"`` 表示实体）。
        length: 截取长度，默认 16。

    Returns:
        ``prefix`` + ``length`` 位十六进制。

    Examples:
        >>> stable_id("PERSON", "Ada Lovelace", prefix="e_")
        'e_2f2e5b0f9c47ac19'  # 任何机器、任何进程、任何 Python 版本都一样
    """
    payload = SEP.join(_stringify(p) for p in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length]}"


def content_hash(data: bytes) -> str:
    """对原始字节求 sha256，用于原始文档的 checksum。"""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# 语义化构造器：统一各层 ID 的构成规则，避免各处自由发挥导致规则漂移
# ---------------------------------------------------------------------------


def doc_id(uri: str, content: bytes | None = None) -> str:
    """文档 ID。

    有内容时按内容哈希（同一文件换名字 ID 不变），无内容时按 URI。
    这样"同一份 PDF 从两个路径进来"能被识别为同一文档。
    """
    if content is not None:
        return stable_id("doc", content_hash(content), prefix="d_")
    return stable_id("doc", uri, prefix="d_")


def chunk_id(doc_id_: str, char_start: int, char_end: int) -> str:
    return stable_id("chunk", doc_id_, char_start, char_end, prefix="c_")


def mention_id(doc_id_: str, char_start: int, char_end: int, surface: str) -> str:
    """实体提及 ID。

    同一处文字被两个抽取器各抽一次，ID 相同 —— 这正是我们想要的，
    后续按 ID 去重即可完成"多抽取器投票"。
    """
    return stable_id("mention", doc_id_, char_start, char_end, surface, prefix="m_")


def entity_id(entity_type: str, canonical_name: str) -> str:
    """实体 ID —— 幂等建图的核心。

    只由 (类型, 规范名) 决定。这意味着：
      * 同一实体从不同文档被抽到，自动收敛为同一个节点
      * 同一批数据重复 build，节点 ID 不变（配合 upsert 即天然幂等）
      * 实体消歧后的合并，就是让两个实体共享同一个 canonical_name
    """
    return stable_id("entity", entity_type, canonical_name.strip().casefold(), prefix="e_")


def edge_id(
    subject_id: str,
    predicate: str,
    object_id: str | None = None,
    object_literal: str | None = None,
) -> str:
    """关系边 ID。

    支持两种宾语：指向另一个实体（object_id），或字面量值（object_literal，
    如日期、金额）。两者互斥，都不给则视为无效边，由调用方拦截。
    """
    if object_id is None and object_literal is None:
        raise ValueError("edge_id 需要 object_id 或 object_literal 至少一个")
    return stable_id("edge", subject_id, predicate, object_id, object_literal, prefix="r_")


def triplet_id(subject_id: str, predicate: str, object_key: str) -> str:
    return stable_id("triplet", subject_id, predicate, object_key, prefix="t_")


def rule_id(subject: str, condition: str, action: str, modality: str) -> str:
    """业务规则 ID（规则抽取 v3 新增）。

    只由 (subject, condition, action, modality) 决定 —— 内容寻址，跨进程幂等：
    同一批数据重跑 → rule_id 不变 → 天然去重；同一条规则跨文档被抽到 →
    自动收敛为同一 ID。condition/subject 为空串时也参与哈希（空与缺失
    在 ``_stringify`` 中被区分），保证无条件义务与带条件规则不会碰撞。
    """
    return stable_id("rule", subject, condition, action, modality, prefix="u_")


def process_id(name: str) -> str:
    """流程 ID（流程抽取 v4 新增）。

    只由流程名决定 —— 内容寻址，跨进程幂等：同一流程跨文档被抽到 →
    自动收敛为同一 ID。
    """
    return stable_id("process", name.strip().casefold(), prefix="p_")


def step_id(process_id_: str, index: int, label: str) -> str:
    """流程步骤 ID（v4）。由 (process, 下标, label) 内容寻址。"""
    return stable_id("step", process_id_, index, label, prefix="s_")


def flow_id(process_id_: str, from_index: int, to_index: int,
            flow_type: str, condition: str) -> str:
    """流程控制流 ID（v4）。由 (process, from, to, type, condition) 内容寻址。"""
    return stable_id("flow", process_id_, from_index, to_index, flow_type, condition, prefix="f_")


# ---------------------------------------------------------------------------
# 预测决策本体（v6）：states / functions / temporal / actions / constraints /
# permissions 六件套的 ID 构造器 —— 全部内容寻址，跨进程幂等
# ---------------------------------------------------------------------------


def state_machine_id(object_name: str, machine_name: str) -> str:
    """状态机 ID（v6）。由 (object, name) 内容寻址，跨进程幂等。"""
    return stable_id("sm", object_name.strip().casefold(),
                     machine_name.strip().casefold(), prefix="sm_")


def state_id(sm_id: str, index: int, label: str) -> str:
    """状态 ID（v6）。由 (sm_id, 下标, label) 内容寻址。"""
    return stable_id("state", sm_id, index, label, prefix="st_")


def transition_id(sm_id: str, from_index: int, to_index: int,
                  event: str, condition: str) -> str:
    """状态迁移 ID（v6）。由 (sm_id, from, to, event, condition) 内容寻址。"""
    return stable_id("trans", sm_id, from_index, to_index, event, condition, prefix="tr_")


def function_id(name: str, subject: str, formula: str) -> str:
    """函数指标 ID（v6）。由 (name, subject, formula) 内容寻址。"""
    return stable_id("func", name.strip().casefold(), subject, formula, prefix="fn_")


def temporal_id(subject: str, kind: str, value: str) -> str:
    """时序时态 ID（v6）。由 (subject, kind, value) 内容寻址。"""
    return stable_id("temporal", subject, kind, value, prefix="tp_")


def action_id(name: str, actor: str, level: str, trigger: str) -> str:
    """动作处置 ID（v6）。由 (name, actor, level, trigger) 内容寻址。"""
    return stable_id("action", name.strip().casefold(), actor, level, trigger, prefix="ac_")


def constraint_id(subject: str, ctype: str, description: str) -> str:
    """约束 ID（v6）。由 (subject, type, description) 内容寻址。"""
    return stable_id("constraint", subject, ctype, description, prefix="cn_")


def permission_id(actor: str, action: str, effect: str) -> str:
    """授权 ID（v6）。由 (actor, action, effect) 内容寻址。"""
    return stable_id("permission", actor, action, effect, prefix="pm_")


def conflict_id(subject_id: str, predicate: str) -> str:
    """冲突 ID 只由 (主语, 谓词) 决定。

    同一对 (s, p) 的所有候选值汇聚成一个冲突对象，而不是每个值一个冲突。
    """
    return stable_id("conflict", subject_id, predicate, prefix="x_")
