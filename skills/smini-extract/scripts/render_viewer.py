#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""render_viewer.py — 把 smini-extract 产出的契约 JSON 渲染成自包含 HTML 查看器。

用法
----
    # 契约格式（宿主交卷：entities + relations）
    python render_viewer.py --in contract.json --out contract.html

    # 兼容 ExtractionResult（runs/<id>/04-extraction.json，自动从 mentions/triplets 转换）
    python render_viewer.py --in 04-extraction.json --out 04-extraction.html --title "抽取结果"

参数
----
    --in   契约 JSON 路径（{entities, relations} 或 {mentions, triplets}），必填
    --out  输出 HTML 路径（缺省 = 输入路径换 .html）
    --title 页面标题（缺省自动推断）
    --host 是否渲染为完整 HTML 文件（含 <!DOCTYPE>，浏览器直接打开；缺省 true）

产物
----
    单文件、零外部依赖的 HTML：统计卡片 + 类型分布 + 关系图谱（力导向布局，
    拖拽/缩放/平移 + 点击聚焦邻域 + 按子图拆分，边按谓词着色）+ 可搜索实体表
    + 关系表（含 evidence 原文引用）。
    数据以 JSON 内嵌于 <script>，离线可用。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# 输入规范化：契约格式 / ExtractionResult 格式 → {entities, relations, attributes, rules, processes}
# ---------------------------------------------------------------------------

def _norm(data: dict) -> tuple[list, list, list, list, list]:
    """返回 (entities, relations, attributes, rules, processes) 标准结构。

    契约格式：{entities:[{surface,canonical,type}],
              relations:[{subject,predicate,object,object_type,evidence}],
              attributes:[{entity,name,value,value_type,evidence}],
              rules:[{subject,condition,action,modality,evidence}],
              processes:[{name,description,steps,flows}]}
    ExtractionResult：{mentions:[...], relations:[...], triplets:[...], rules:[...],
                      processes:[...]}
      —— 属性从 triplets 中 value_type 非空的条目提取（薄壳已把 attribute
         映射成 value_type 非空的三元组）；rules / processes 直接取自 data。
    """
    attributes: list[dict] = []
    rules: list[dict] = []
    processes: list[dict] = []

    if data.get("entities") and data.get("relations"):
        # 契约格式：attributes / rules / processes 可能缺失（旧契约向后兼容）
        attributes = list(data.get("attributes") or [])
        rules = list(data.get("rules") or [])
        processes = [_norm_process(p) for p in (data.get("processes") or [])]
        # 契约 relations 透传 v6 传导字段（缺省空串 = 普通事实）
        rels = []
        for r in data["relations"]:
            rels.append(dict(r, prop_kind=r.get("prop_kind") or "",
                             strength=r.get("strength") or ""))
        return list(data["entities"]), rels, attributes, rules, processes

    mentions = data.get("mentions") or []
    triplets = data.get("triplets") or []
    relations = data.get("relations") or []

    entities: list[dict] = []
    seen: set = set()
    # mention_id → normalized，供 relation.evidence 回填按 canonical 匹配
    name_by_mid: dict = {}
    for m in mentions:
        name = m.get("normalized") or m.get("text")
        if m.get("mention_id"):
            name_by_mid[m["mention_id"]] = name
        if not name or name in seen:
            continue
        seen.add(name)
        entities.append({
            "surface": m.get("text") or name,
            "canonical": name,
            "type": m.get("entity_type") or "OTHER",
        })

    # triplets 关联 relations 补 evidence：
    # relation.subject_ref 是 mention_id，映射回 canonical 后与 triplet.subject 匹配
    ev_by_pred: dict = {}
    for r in relations:
        sid = name_by_mid.get(r.get("subject_ref"), r.get("subject_ref"))
        key = (sid, r.get("predicate"))
        if r.get("evidence_text") and key not in ev_by_pred:
            ev_by_pred[key] = r["evidence_text"]

    norm_relations: list[dict] = []
    for t in triplets:
        norm_relations.append({
            "subject": t.get("subject", ""),
            "predicate": t.get("predicate", ""),
            "object": t.get("object", ""),
            "object_type": t.get("object_type"),
            "evidence": ev_by_pred.get((t.get("subject"), t.get("predicate")), ""),
            "prop_kind": t.get("prop_kind") or "",
            "strength": t.get("strength") or "",
        })
        # 属性三元组：value_type 非空 → 归入属性清单
        if t.get("value_type"):
            attributes.append({
                "entity": t.get("subject", ""),
                "name": t.get("predicate", ""),
                "value": t.get("object", ""),
                "value_type": t.get("value_type"),
                "required": bool(t.get("required")),
                "evidence": ev_by_pred.get((t.get("subject"), t.get("predicate")), ""),
            })
    # ExtractionResult 的 rules：薄壳已映射为 {rule_id,subject,condition,action,modality,evidence_text}
    for r in data.get("rules") or []:
        rules.append({
            "subject": r.get("subject", ""),
            "condition": r.get("condition", ""),
            "action": r.get("action", ""),
            "modality": r.get("modality", "OTHER"),
            "evidence": r.get("evidence_text") or "",
            # 借鉴：用途类型 / 输出取值 / 复用方 / 确定性分级
            "rule_type": r.get("rule_type") or "",
            "output_type": r.get("output_type") or "",
            "reused_by": r.get("reused_by") or [],
            "certainty": r.get("certainty") or "",
        })
    # ExtractionResult 的 processes：薄壳已映射为 Process dataclass，
    # flows 用 from_index/to_index（与契约 from/to 命名不同，这里归一化）
    for p in data.get("processes") or []:
        processes.append(_norm_process(p))
    return entities, norm_relations, attributes, rules, processes


def _norm_process(p: dict) -> dict:
    """把契约/ExtractionResult 的 process 归一化成渲染用的 {name, description, steps, flows}。

    兼容两种 flow 命名：宿主契约 from/to ↔ ExtractionResult from_index/to_index。
    steps/evidence 兼容 evidence/evidence_text 两种键名。
    """
    steps: list[dict] = []
    for st in p.get("steps") or []:
        steps.append({
            "index": st.get("index", len(steps)),
            "label": st.get("label", ""),
            "kind": st.get("kind") or "TASK",
            "actor": st.get("actor", ""),
            "sub_process_ref": st.get("sub_process_ref", ""),  # 子流程引用
            # 审批流增强（v9 M6）：泳道 / 审批结果 / 驳回目标
            "lane": st.get("lane", ""),
            "approval_outcome": st.get("approval_outcome") or "",
            "reject_to": st.get("reject_to") if st.get("reject_to") is not None else None,
            "evidence": st.get("evidence") or st.get("evidence_text") or "",
        })
    flows: list[dict] = []
    for f in p.get("flows") or []:
        flows.append({
            "from": f.get("from_index", f.get("from", -1)),
            "to": f.get("to_index", f.get("to", -1)),
            "type": f.get("type") or "SEQUENCE",
            "condition": f.get("condition", ""),
            "on_reject": f.get("on_reject", ""),  # 驳回边条件（v9 M6）
            "evidence": f.get("evidence") or f.get("evidence_text") or "",
        })
    return {
        "name": p.get("name", ""),
        "description": p.get("description", ""),
        "flow_type": p.get("flow_type") or "",           # 流程类型（v9 M6）
        "approval_chain": list(p.get("approval_chain") or []),  # 审批链路摘要（v9 M6）
        "steps": steps,
        "flows": flows,
        "preconditions": list(p.get("preconditions") or []),    # 前置条件
        "postconditions": list(p.get("postconditions") or []),  # 后置条件
    }


def _norm_six(data: dict) -> dict:
    """把契约/ExtractionResult 的预测决策本体六件套归一化为渲染结构。

    兼容两种命名：宿主契约 from/to ↔ ExtractionResult from_index/to_index；
    evidence/evidence_text 两种键名。缺省返回空列表（旧数据向后兼容）。
    """
    def ev(x):
        return x.get("evidence") or x.get("evidence_text") or ""

    states = []
    for sm in data.get("states") or []:
        sts = [{"label": s.get("label", ""), "initial": bool(s.get("initial")),
                "evidence": ev(s)} for s in (sm.get("states") or [])]
        trs = []
        for t in sm.get("transitions") or []:
            trs.append({
                "from": t.get("from_index", t.get("from", -1)),
                "to": t.get("to_index", t.get("to", -1)),
                "event": t.get("event", ""),
                "condition": t.get("condition", ""),
                "action": t.get("action", ""),
                "evidence": ev(t),
            })
        states.append({"object": sm.get("object", ""), "name": sm.get("name", ""),
                       "states": sts, "transitions": trs})

    functions = [{
        "name": f.get("name", ""), "subject": f.get("subject", ""),
        "formula": f.get("formula", ""), "inputs": list(f.get("inputs") or []),
        "output_type": f.get("output_type") or "OTHER", "evidence": ev(f),
    } for f in data.get("functions") or []]

    temporal = [{
        "subject": t.get("subject", ""), "kind": t.get("kind") or "VALIDITY",
        "value": t.get("value", ""), "anchor": t.get("anchor", ""),
        "evidence": ev(t),
    } for t in data.get("temporal") or []]

    actions = [{
        "name": a.get("name", ""), "actor": a.get("actor", ""),
        "level": a.get("level") or "OBSERVE", "target": a.get("target", ""),
        "side_effect": a.get("side_effect", ""), "trigger": a.get("trigger", ""),
        # 动作前后置条件
        "precondition": a.get("precondition", ""), "postcondition": a.get("postcondition", ""),
        "evidence": ev(a),
    } for a in data.get("actions") or []]

    constraints = [{
        "subject": c.get("subject", ""), "type": c.get("type") or "CONSISTENCY",
        "description": c.get("description", ""), "evidence": ev(c),
    } for c in data.get("constraints") or []]

    permissions = [{
        "actor": p.get("actor", ""), "action": p.get("action", ""),
        "effect": p.get("effect") or "DENY", "scope": p.get("scope", ""),
        # 角色中间层 + 主体类型 + 数据可见范围
        "role": p.get("role", ""), "actor_type": p.get("actor_type") or "",
        "data_scope": p.get("data_scope") or "",
        "evidence": ev(p),
    } for p in data.get("permissions") or []]

    return {"states": states, "functions": functions, "temporal": temporal,
            "actions": actions, "constraints": constraints, "permissions": permissions}


# ---------------------------------------------------------------------------
# HTML 模板（复用并统一为通用标题）
# ---------------------------------------------------------------------------

def _meta_line(meta: dict) -> str:
    """document_meta（manifest 头）渲染成一行说明；全空则留空。"""
    parts = []
    for label, key in (("领域", "domain"), ("来源", "source"),
                       ("版本", "version"), ("文档类型", "doc_type")):
        v = str(meta.get(key) or "").strip()
        if v:
            parts.append(f"{label}：{v}")
    return " · ".join(parts)

TYPE_COLOR = {
    "PERSON": "#C0392B", "ORGANIZATION": "#3E7CB1", "LOCATION": "#16A085",
    "DATE": "#2F9E78", "TIME": "#7D8C9E", "MONEY": "#B7950B",
    "PERCENT": "#B7950B", "QUANTITY": "#7D8C9E", "PRODUCT": "#D97706",
    "EVENT": "#8E44AD", "CONCEPT": "#6C8FD1", "OTHER": "#7D8C9E",
    # 金融领域扩展类
    "FINANCIAL_INSTRUMENT": "#2E86AB", "REGULATION": "#B83227",
    "FINANCIAL_INDICATOR": "#1F7A8C", "MARKET": "#6D4C41",
    "RISK": "#C2185B", "TRANS": "#00838F", "SECURITY_CODE": "#5D4037",
}

# 属性值类型独立配色（属性是字面量 DatatypeProperty，非实体）
VALUE_TYPE_COLOR = {
    "DATE": "#2F9E78", "TIME": "#7D8C9E", "MONEY": "#B7950B",
    "PERCENT": "#B7950B", "QUANTITY": "#D97706", "NUMBER": "#16A085",
    "BOOLEAN": "#C0392B", "ENUM": "#8E44AD", "STRING": "#3E7CB1",
    "DICT_REF": "#5D4037", "ENTITY_REF": "#2E86AB",  # 字典/实体引用
    "OTHER": "#7D8C9E",
}

# 规则模态独立配色（业务规则：道义模态）
MODALITY_COLOR = {
    "OBLIGATION": "#C0392B", "PROHIBITION": "#EA6668", "PERMISSION": "#16A085",
    "CONDITIONAL": "#3E7CB1", "OTHER": "#7D8C9E",
}

# 流程步骤类型独立配色（业务流程 v4：TASK/GATEWAY/EVENT；增 START/END/SYSTEM_TASK）
STEP_KIND_COLOR = {
    "TASK": "#3E7CB1", "GATEWAY": "#D97706", "EVENT": "#8E44AD",
    "START": "#16A085", "END": "#C0392B", "SYSTEM_TASK": "#6C8FD1",
}

# 规则用途类型独立配色（校验/推导/触发）
RULE_TYPE_COLOR = {
    "VALIDATION": "#3E7CB1", "DERIVATION": "#8E44AD", "TRIGGER": "#D97706",
    "OTHER": "#7D8C9E",
}

# 确定性分级独立配色（行业通用/企业专属）
CERTAINTY_COLOR = {
    "GENERAL": "#16A085", "ENTERPRISE": "#B83227", "OTHER": "#7D8C9E",
}

# 主体类型独立配色（人工/系统）
ACTOR_TYPE_COLOR = {
    "HUMAN": "#3E7CB1", "SYSTEM": "#8E44AD", "OTHER": "#7D8C9E",
}

# 控制流类型独立配色（业务流程：SEQUENCE/PARALLEL/CONDITIONAL/LOOP）
FLOW_TYPE_COLOR = {
    "SEQUENCE": "#3E7CB1", "PARALLEL": "#16A085", "CONDITIONAL": "#D97706",
    "LOOP": "#8E44AD",
}

# 流程级类型独立配色（v9 M6 flowType：协作流/审批流）
PROC_FLOW_TYPE_COLOR = {
    "COLLABORATION": "#3E7CB1", "APPROVAL": "#B83227",
}

# 审批结果三态独立配色（v9 M6 approvalOutcomes：通过/否决/退回）
APPROVAL_OUTCOME_COLOR = {
    "APPROVE": "#16A085", "REJECT": "#C0392B", "RETURN": "#D97706", "OTHER": "#7D8C9E",
}

# 数据可见范围独立配色（v9 M5 dataScope：全机构/本人/本部门/自定义）
DATA_SCOPE_COLOR = {
    "ALL": "#16A085", "OWN": "#3E7CB1", "DEPT": "#8E44AD", "CUSTOM": "#D97706", "OTHER": "#7D8C9E",
}

# 预测决策本体六件套独立配色
FUNC_OUTPUT_COLOR = {   # 函数指标输出类型
    "NUMBER": "#16A085", "PERCENT": "#B7950B", "RANK": "#D97706",
    "BOOLEAN": "#C0392B", "ENUM": "#8E44AD", "STRING": "#3E7CB1", "OTHER": "#7D8C9E",
}
TEMPORAL_KIND_COLOR = {  # 时序时态类型
    "VALIDITY": "#3E7CB1", "FREQUENCY": "#16A085", "WINDOW": "#D97706",
    "TRIGGER": "#8E44AD",
}
ACTION_LEVEL_COLOR = {   # 动作处置级别
    "OBSERVE": "#3E7CB1", "WARN": "#D97706", "INTERVENE": "#C0392B",
}
CONSTRAINT_TYPE_COLOR = {  # 约束类型
    "CARDINALITY": "#8E44AD", "VALUE_RANGE": "#3E7CB1", "ENUM": "#D97706",
    "DISJOINT": "#C0392B", "REQUIRED": "#16A085", "CONSISTENCY": "#7D8C9E",
}
PERMISSION_EFFECT_COLOR = {  # 授权效果
    "PERMIT": "#16A085", "DENY": "#C0392B",
}
PROP_KIND_COLOR = {   # 关系传导类型
    "FACTUAL": "#7D8C9E", "DEPENDENCY": "#3E7CB1", "CAUSAL": "#C0392B",
    "TRIGGER": "#8E44AD",
}

_HTML_TPL = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root{--bg:#F4F3EE;--card:#FFFFFF;--line:#E4E3DD;--text:#1A1B1C;--sub:#6B7280;--accent:#3E7CB1;}
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'PingFang SC','Segoe UI',Arial,sans-serif;padding:24px;line-height:1.5;}
  .wrap{max-width:1080px;margin:0 auto;}
  h1{font-size:20px;font-weight:600;margin-bottom:4px;}
  .sub{color:var(--sub);font-size:13px;margin-bottom:20px;}
  .cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px;}
  .card{flex:1 1 150px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;}
  .card .num{font-size:24px;font-weight:600;color:var(--accent);}
  .card .lab{font-size:12px;color:var(--sub);margin-top:2px;}
  .dist{display:flex;gap:6px;height:10px;border-radius:6px;overflow:hidden;margin-bottom:20px;}
  .dist i{display:block;height:100%;}
  .legend{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:16px;font-size:12px;color:var(--sub);}
  .legend span{display:inline-flex;align-items:center;gap:6px;}
  .dot{width:10px;height:10px;border-radius:3px;display:inline-block;}
  .tabs{display:flex;gap:8px;margin-bottom:16px;}
  .tabs button{border:1px solid var(--line);background:var(--card);color:var(--sub);border-radius:20px;padding:7px 16px;font-size:13px;cursor:pointer;}
  .tabs button.on{background:var(--accent);color:#fff;border-color:var(--accent);}
  .panel{display:none;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-bottom:20px;}
  .panel.on{display:block;}
  .toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:14px;}
  .toolbar input,.toolbar select{padding:7px 12px;border:1px solid var(--line);border-radius:8px;font-size:13px;background:#fff;}
  .toolbar input{flex:1 1 200px;}
  table{width:100%;border-collapse:collapse;font-size:13px;}
  th{text-align:left;color:var(--sub);font-weight:500;padding:8px 10px;border-bottom:2px solid var(--line);white-space:nowrap;}
  td{padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top;}
  tr:hover td{background:#F8F7F3;}
  .badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;color:#fff;font-weight:500;}
  .ev{color:var(--sub);font-size:12px;font-style:italic;}
  .ev:before{content:"\\201C";}.ev:after{content:"\\201D";}
  #graph svg{width:100%;height:auto;display:block;background:#FCFBF8;border:1px solid var(--line);border-radius:10px;touch-action:none;}
  #graph .gnode{cursor:grab;}
  .graph-toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px;}
  .graph-toolbar button{border:1px solid var(--line);background:var(--card);color:var(--sub);border-radius:16px;padding:5px 14px;font-size:12px;cursor:pointer;}
  .graph-toolbar button.on{background:var(--accent);color:#fff;border-color:var(--accent);}
  .graph-hint{font-size:11px;color:var(--sub);margin-left:auto;}
  #g-legend{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px;font-size:11px;color:var(--sub);}
  #g-legend .gl-item{display:inline-flex;align-items:center;gap:5px;}
  .subgraph-card{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:14px;}
  .subgraph-card svg{width:100%;height:auto;display:block;background:#FCFBF8;border:1px solid var(--line);border-radius:10px;}
  .tip{font-size:11px;color:var(--sub);margin-top:8px;}
  @media (max-width:600px){body{padding:14px;} h1{font-size:17px;} .card .num{font-size:20px;}}
</style>
</head>
<body>
<div class="wrap">
  <h1>__TITLE__</h1>
  <div class="sub">来源：smini-extract 抽取结果 · 宿主 agent 按契约交卷 · Python 薄壳端到端运行（__SUMM__）</div>
  <div class="sub" id="meta" style="font-size:12px;">__META__</div>
  <div class="sub" id="summ6" style="font-size:12px;">__SUMM6__</div>
  <div class="cards">
    <div class="card"><div class="num">__NE__</div><div class="lab">实体</div></div>
    <div class="card"><div class="num">__NR__</div><div class="lab">关系</div></div>
    <div class="card"><div class="num">__NA__</div><div class="lab">属性</div></div>
    <div class="card"><div class="num">__NU__</div><div class="lab">规则</div></div>
    <div class="card"><div class="num">__NP__</div><div class="lab">流程</div></div>
    <div class="card"><div class="num">__NT__</div><div class="lab">实体类型</div></div>
    <div class="card"><div class="num">__NL__</div><div class="lab">字面量</div></div>
  </div>
  <div class="dist" id="dist"></div>
  <div class="legend" id="legend"></div>
  <div class="tabs">
    <button data-t="graph" class="on">关系图谱</button>
    <button data-t="entities">实体清单</button>
    <button data-t="relations">关系清单</button>
    <button data-t="attributes">属性清单</button>
    <button data-t="rules">规则清单</button>
    <button data-t="processes">流程清单</button>
    <button data-t="states">状态机</button>
    <button data-t="functions">指标函数</button>
    <button data-t="temporal">时态</button>
    <button data-t="actions">动作处置</button>
    <button data-t="constraints">约束</button>
    <button data-t="permissions">授权</button>
  </div>
  <div class="panel on" id="p-graph">
    <div class="graph-toolbar">
      <button data-gmode="full" class="on">全图</button>
      <button data-gmode="sub">按子图拆分</button>
      <span class="graph-hint">拖拽节点移动 · 滚轮缩放 · 拖空白平移 · 点击节点聚焦邻域（再点空白取消）</span>
    </div>
    <div id="g-legend"></div>
    <div id="graph"></div>
    <div class="tip">力导向布局：节点按关系聚团、度大者居中偏大，边按谓词着色（有向箭头），平行边以曲率错开；悬停/点击节点可聚焦其邻域。「按子图拆分」按连通分量把大图拆成可独立阅读的小图。</div>
  </div>
  <div class="panel" id="p-entities">
    <div class="toolbar">
      <input id="es" placeholder="搜索实体名称…">
      <select id="et"><option value="">全部类型</option></select>
    </div>
    <table id="etab"><thead><tr><th>surface</th><th>canonical（唯一身份）</th><th>type</th></tr></thead><tbody></tbody></table>
  </div>
  <div class="panel" id="p-relations">
    <table id="rtab"><thead><tr><th>主语</th><th>谓词</th><th>宾语</th><th>object_type</th><th>传导类型</th><th>强度</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
  </div>
  <div class="panel" id="p-attributes">
    <div class="toolbar">
      <input id="as" placeholder="搜索属性（实体 / 属性名 / 值）…">
      <select id="av"><option value="">全部值类型</option></select>
    </div>
    <table id="atab"><thead><tr><th>实体</th><th>属性名</th><th>值</th><th>value_type</th><th>必录</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">属性是实体的字面量数据属性（本体论 DatatypeProperty）：与关系不同，值不是实体、不产生图谱节点，落为实体属性。</div>
  </div>
  <div class="panel" id="p-rules">
    <div class="toolbar">
      <input id="us" placeholder="搜索规则（主体 / 条件 / 动作）…">
      <select id="um"><option value="">全部模态</option></select>
    </div>
    <table id="utab"><thead><tr><th>主体</th><th>条件（IF）</th><th>动作/结论（THEN）</th><th>模态</th><th>用途类型</th><th>输出</th><th>复用方</th><th>确定性</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">业务规则是条件-动作/结论的规范性知识：含道义动词（应当/不得/可以）或条件-动作结构（如果/当…时…则…）。新增：用途类型（校验/推导/触发）、输出取值、复用方（被哪些流程/函数引用）、确定性分级（行业通用/企业专属）。作为独立产出保留，不进入实体-边图谱。</div>
  </div>
  <div class="panel" id="p-processes">
    <div class="toolbar">
      <input id="ps" placeholder="搜索流程 / 步骤…">
    </div>
    <div id="procs"></div>
    <div class="tip">业务流程是「谁→按什么顺序→做什么」的执行结构（本体论 DOLCE 的 perdurant 持续体）。SVG 线性流程图：实线=顺序，虚线=并行（AND），带条件标签=排他分支（XOR），回环=循环（LOOP）。</div>
  </div>
  <div class="panel" id="p-states">
    <div class="toolbar">
      <input id="sts" placeholder="搜索状态机 / 对象 / 状态…">
    </div>
    <div id="states"></div>
    <div class="tip">状态机（预测决策）：**预测 = 对象在某个时间窗内从状态 A 演化到状态 B**。状态节点 + 迁移箭头表达对象生命周期拓扑；箭头标签 = 触发事件 / 守卫条件。</div>
  </div>
  <div class="panel" id="p-functions">
    <div class="toolbar">
      <input id="fns" placeholder="搜索指标 / 主体 / 公式…">
    </div>
    <table id="fntab"><thead><tr><th>指标名</th><th>主体</th><th>公式（声明式定义）</th><th>依赖因子</th><th>输出类型</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">函数指标：把业务经验写成**可计算定义**（如压力函数=需求强度÷供给能力），是推演与可解释性的输入。与 attributes 分界：attributes 存字面量值，functions 存计算定义。</div>
  </div>
  <div class="panel" id="p-temporal">
    <div class="toolbar">
      <input id="tps" placeholder="搜索主体 / 时间描述…">
    </div>
    <table id="tptab"><thead><tr><th>主体</th><th>时态类型</th><th>时间描述</th><th>锚点</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">时序时态：把时间限定（生效/频率/时间窗/触发时点）绑定到事实/规则/流程/状态。与 DATE 实体分界：DATE 是图中节点，temporal 是把时间语义绑定到主体。</div>
  </div>
  <div class="panel" id="p-actions">
    <div class="toolbar">
      <input id="acs" placeholder="搜索动作 / 执行者 / 触发…">
    </div>
    <table id="actab"><thead><tr><th>动作</th><th>执行者</th><th>级别</th><th>作用对象</th><th>副作用/代价</th><th>触发情形</th><th>前置条件</th><th>后置条件</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">动作处置：处置动作的元信息（怎么做/什么级别/什么代价/何时触发），供决策执行。级别：OBSERVE 观察 → WARN 警示 → INTERVENE 干预。</div>
  </div>
  <div class="panel" id="p-constraints">
    <div class="toolbar">
      <input id="cns" placeholder="搜索约束对象 / 描述…">
    </div>
    <table id="cntab"><thead><tr><th>约束对象</th><th>类型</th><th>约束内容</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">结构约束（SHACL 式）：基数/值域/枚举/不相交/必填/一致性——保证预测与决策不基于非法状态。与 rules 分界：rules 是「该做什么」的规范，constraints 是「什么合法」的结构约束。</div>
  </div>
  <div class="panel" id="p-permissions">
    <div class="toolbar">
      <input id="pms" placeholder="搜索主体 / 动作 / 范围…">
    </div>
    <table id="pmtab"><thead><tr><th>主体</th><th>动作</th><th>效果</th><th>范围</th><th>角色</th><th>主体类型</th><th>数据范围</th><th>evidence（原文引用）</th></tr></thead><tbody></tbody></table>
    <div class="tip">主体-动作授权（RBAC）：谁能（不能）执行什么动作，决策自动化的治理层。效果：PERMIT 允许 / DENY 禁止（默认禁止，最小权限原则）。数据范围（v9 M5 dataScope）：ALL 全机构 / OWN 本人 / DEPT 本部门 / CUSTOM 自定义。</div>
  </div>
</div>
<script>
const DATA = __DATA__;
const TC = __TYPECOLOR__;

// ---- 像素感知文本测量/截断/换行（解决中文全角字符按字符数截断导致的溢出） ----
const _mctx = (function(){ try { return document.createElement('canvas').getContext('2d'); } catch(e){ return null; } })();
function textW(s, fs){
  s = String(s==null?'':s);
  if (_mctx){ _mctx.font = fs+'px "PingFang SC","Segoe UI",Arial,sans-serif'; return _mctx.measureText(s).width; }
  let w = 0;
  for (let i=0;i<s.length;i++){ const c = s.charCodeAt(i); w += (c > 0x2E7F ? 1 : 0.55); }
  return w * fs;
}
function truncW(s, fs, maxW){
  s = String(s==null?'':s);
  let t = '', w = 0, i = 0;
  for (; i < s.length; i++){
    const cw = textW(s[i], fs);
    if (w + cw > maxW) break;
    w += cw; t += s[i];
  }
  return (i < s.length) ? t + '…' : t;
}
function wrapLines(s, fs, maxW, maxLines){
  s = String(s==null?'':s);
  const out = [];
  let start = 0;
  for (let ln = 0; ln < maxLines && start < s.length; ln++){
    let end = start, w = 0;
    while (end < s.length){
      const cw = textW(s[end], fs);
      if (w + cw > maxW && end > start) break;
      w += cw; end++;
    }
    if (ln === maxLines-1 && end < s.length) out.push(truncW(s.slice(start), fs, maxW));
    else { out.push(s.slice(start, end)); start = end; }
    if (end === s.length) break;
  }
  return out;
}

(function(){
  const dist = document.getElementById('dist');
  const cnt = {};
  DATA.entities.forEach(e => cnt[e.type] = (cnt[e.type]||0)+1);
  const total = DATA.entities.length || 1;
  const legend = document.getElementById('legend');
  Object.keys(cnt).forEach(t => {
    const i = document.createElement('i');
    i.style.background = TC[t]||'#999';
    i.style.width = (cnt[t]/total*100).toFixed(2)+'%';
    i.title = t+' '+cnt[t];
    dist.appendChild(i);
    const s = document.createElement('span');
    s.innerHTML = '<span class="dot" style="background:'+(TC[t]||'#999')+'"></span>'+t+' × '+cnt[t];
    legend.appendChild(s);
  });
  const sel = document.getElementById('et');
  Object.keys(cnt).forEach(t => {
    const o = document.createElement('option'); o.value=t; o.textContent=t+' ('+cnt[t]+')'; sel.appendChild(o);
  });
})();

(function(){
  const tb = document.getElementById('etab').querySelector('tbody');
  const es = document.getElementById('es'), et = document.getElementById('et');
  function render(){
    const q = es.value.trim(), t = et.value;
    tb.innerHTML='';
    DATA.entities.filter(e => (!t || e.type===t) && (!q || (e.surface||'').includes(q) || (e.canonical||'').includes(q)))
      .forEach(e => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td>'+esc(e.surface)+'</td><td>'+esc(e.canonical||e.surface)+'</td>'
          +'<td><span class="badge" style="background:'+(TC[e.type]||'#999')+'">'+e.type+'</span></td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  et.addEventListener('change', render);
  render();
})();

(function(){
  const tb = document.getElementById('rtab').querySelector('tbody');
  const PK = DATA.prop_kind_color || {};
  DATA.relations.forEach(r => {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td>'+esc(r.subject)+'</td><td><b>'+esc(r.predicate)+'</b></td><td>'+esc(r.object)+'</td>'
      +'<td>'+(r.object_type?('<span class="badge" style="background:'+(TC[r.object_type]||'#999')+'">'+r.object_type+'</span>'):'<span style="color:#bbb">—</span>')+'</td>'
      +'<td>'+(r.prop_kind?('<span class="badge" style="background:'+(PK[r.prop_kind]||'#999')+'">'+r.prop_kind+'</span>'):'<span style="color:#bbb">—</span>')+'</td>'
      +'<td>'+(r.strength?esc(r.strength):'<span style="color:#bbb">—</span>')+'</td>'
      +'<td class="ev">'+esc(r.evidence||'')+'</td>';
    tb.appendChild(tr);
  });
})();

(function(){
  const host = document.getElementById('atab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const VT = DATA.value_type_color || {};
  const attrs = DATA.attributes || [];
  const sel = document.getElementById('av');
  const vcnt = {};
  attrs.forEach(a => vcnt[a.value_type] = (vcnt[a.value_type]||0)+1);
  Object.keys(vcnt).forEach(t => {
    const o = document.createElement('option'); o.value=t; o.textContent=t+' ('+vcnt[t]+')'; sel.appendChild(o);
  });
  const es = document.getElementById('as');
  function render(){
    const q = es.value.trim(), v = sel.value;
    tb.innerHTML='';
    attrs.filter(a => (!v || a.value_type===v) && (!q || (a.entity||'').includes(q) || (a.name||'').includes(q) || (a.value||'').includes(q)))
      .forEach(a => {
        const tr = document.createElement('tr');
        // 必录标记（M1 required）
        tr.innerHTML = '<td>'+esc(a.entity)+'</td><td><b>'+esc(a.name)+'</b></td><td>'+esc(a.value)+'</td>'
          +'<td><span class="badge" style="background:'+(VT[a.value_type]||'#999')+'">'+(a.value_type||'OTHER')+'</span></td>'
          +'<td>'+(a.required ? '<b style="color:#C0392B">必录</b>' : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(a.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  sel.addEventListener('change', render);
  render();
})();

(function(){
  const host = document.getElementById('utab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const MD = DATA.modality_color || {};
  const rules = DATA.rules || [];
  const sel = document.getElementById('um');
  const mcnt = {};
  rules.forEach(r => mcnt[r.modality] = (mcnt[r.modality]||0)+1);
  Object.keys(mcnt).forEach(t => {
    const o = document.createElement('option'); o.value=t; o.textContent=t+' ('+mcnt[t]+')'; sel.appendChild(o);
  });
  const es = document.getElementById('us');
  function render(){
    const q = es.value.trim(), v = sel.value;
    tb.innerHTML='';
    const RTC = DATA.rule_type_color || {};
    const CERT = DATA.certainty_color || {};
    rules.filter(r => (!v || r.modality===v) && (!q || (r.subject||'').includes(q) || (r.condition||'').includes(q) || (r.action||'').includes(q)))
      .forEach(r => {
        const tr = document.createElement('tr');
        // 注意：空值 fallback（—）是 HTML 片段，不能塞进 esc()（否则源码被转义成字面文本）
        const rb = (r.reused_by || []).join('、');
        tr.innerHTML = '<td>'+ (r.subject ? esc(r.subject) : '<span style="color:#bbb">—</span>') +'</td>'
          +'<td>'+ (r.condition ? esc(r.condition) : '<span style="color:#bbb">—</span>') +'</td>'
          +'<td><b>'+esc(r.action)+'</b></td>'
          +'<td><span class="badge" style="background:'+(MD[r.modality]||'#999')+'">'+(r.modality||'OTHER')+'</span></td>'
          +'<td>'+(r.rule_type ? '<span class="badge" style="background:'+(RTC[r.rule_type]||'#999')+'">'+r.rule_type+'</span>' : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(r.output_type ? esc(r.output_type) : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(rb ? esc(rb) : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(r.certainty ? '<span class="badge" style="background:'+(CERT[r.certainty]||'#999')+'">'+r.certainty+'</span>' : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(r.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  sel.addEventListener('change', render);
  render();
})();

(function(){
  const host = document.getElementById('procs');
  if (!host) return;
  const SK = DATA.step_kind_color || {};
  const FT = DATA.flow_type_color || {};
  const PFT = DATA.proc_flow_type_color || {};
  const AO = DATA.approval_outcome_color || {};
  const procs = DATA.processes || [];
  const es = document.getElementById('ps');
  const FLOW_LABEL = {SEQUENCE:'顺序', PARALLEL:'并行', CONDITIONAL:'分支', LOOP:'循环'};
  const KIND_LABEL = {TASK:'活动', GATEWAY:'决策', EVENT:'事件', START:'开始', END:'结束', SYSTEM_TASK:'系统任务'};
  const BW = 190, BH = 80, GX = 44, GY = 160;   // 节点宽/高/水平间距/行间距（BH 加高容纳两行标签）
  const GUTTER = 180;                            // 右走廊宽度：跨行折线的标签区（防文字被画布右缘裁剪）
  const PER_ROW = 3;                             // 每行节点数（多行蛇形布局，避免窄屏过宽）
  const X0 = 20, Y0 = 56;

  function escAttr(s){ return esc(s).replace(/&quot;/g,'&quot;'); }

  function svgFlow(p, uid){
    const steps = p.steps || [];
    if (!steps.length) return '';
    const n = steps.length;
    const rows = Math.ceil(n/PER_ROW);
    const cols = Math.min(n, PER_ROW);
    const W = X0 + cols*(BW+GX) - GX + X0 + GUTTER;
    const H = Y0 + rows*GY + 36;
    const rowOf = i => Math.floor(i/PER_ROW);
    const colOf = i => i%PER_ROW;
    const nx = i => X0 + colOf(i)*(BW+GX);      // 节点左上 x
    const ny = i => Y0 + rowOf(i)*GY;            // 节点左上 y
    const ncx = i => nx(i)+BW/2;                 // 节点中心 x
    const ncy = i => ny(i)+BH/2;                 // 节点中心 y
    let s = '<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;">'
      + '<defs>'
      + ['SEQUENCE','PARALLEL','CONDITIONAL','LOOP'].map(t => {
          const c = FT[t] || '#999';
          return '<marker id="arr'+t+'_'+uid+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
            + '<path d="M0,0 L10,5 L0,10 z" fill="'+c+'"/></marker>';
        }).join('')
      + '</defs>';

    // 控制流（flows 为空时按顺序补 SEQUENCE 默认线）
    const flows = (p.flows && p.flows.length) ? p.flows : steps.slice(1).map((_,i)=>({from:i,to:i+1,type:'SEQUENCE',condition:'',evidence:''}));
    flows.forEach(f => {
      const fi = f.from, ti = f.to;
      if (fi<0 || ti<0 || fi>=n || ti>=n) return;
      const ft = f.type || 'SEQUENCE';
      const color = FT[ft] || '#999';
      // 驳回回跳（v9 M6）：回跳且带 on_reject / 涉及 RETURN 步骤 → 红色虚线
      const isReject = (ti < fi) && (f.on_reject || (steps[ti]||{}).approval_outcome==='RETURN' || (steps[fi]||{}).approval_outcome==='RETURN');
      const rjColor = '#C0392B';
      const strokeColor = isReject ? rjColor : color;
      const dash = (ft==='PARALLEL') ? ' stroke-dasharray="7,5"' : '';
      const R = X0 + cols*(BW+GX) - GX;          // 最右节点左缘 x（折线右折点参考）
      let d, label = FLOW_LABEL[ft]||ft, lx, ly, anchor = 'middle';
      const x1 = nx(fi)+BW, y1 = ncy(fi);        // 出点：fi 右缘中点
      const x2 = nx(ti), y2 = ncy(ti);           // 入点：ti 左缘中点
      if (rowOf(ti) === rowOf(fi) && ti >= fi) {
        // 同行前向：直线
        d = 'M '+x1+' '+y1+' L '+(x2-8)+' '+y2;
        lx = (x1+x2)/2; ly = y1-12;
      } else if (ti > fi) {
        // 跨行前向：右折→下→从 ti 顶部进入（避免与下一行同行连线在同一水平带重叠）
        d = 'M '+x1+' '+y1+' L '+(R+GUTTER/2)+' '+y1+' L '+(R+GUTTER/2)+' '+(ny(ti)-16)+' L '+ncx(ti)+' '+(ny(ti)-16)+' L '+ncx(ti)+' '+(ny(ti)+2);
        lx = R+GUTTER/2+6; ly = (y1+ny(ti)-16)/2; anchor = 'start';
      } else {
        // 回跳（LOOP/反向/驳回）：右折→上→进 ti 顶部
        d = 'M '+x1+' '+y1+' L '+(R+GUTTER/2)+' '+y1+' L '+(R+GUTTER/2)+' '+(ny(ti)-20)+' L '+(ncx(ti))+' '+(ny(ti)-20)+' L '+ncx(ti)+' '+(ny(ti)+2);
        lx = R+GUTTER/2+6; ly = (y1+ny(ti)-20)/2; anchor = 'start';
      }
      s += '<path d="'+d+'" fill="none" stroke="'+strokeColor+'" stroke-width="'+(isReject?2.4:2)+'"'
        + ((isReject||ft==='LOOP') ? ' stroke-dasharray="9,4"' : '') + dash
        + (isReject ? '' : ' marker-end="url(#arr'+ft+'_'+uid+')"') + '/>';
      // 类型 + 条件标签：跨行/回跳在右走廊左对齐分两行；同行在节点上方居中单行（均按像素截断，防溢出）
      if (anchor === 'start') {
        let yy = ly;
        const lbl = isReject ? '驳回' : label;
        s += '<text x="'+lx+'" y="'+yy+'" text-anchor="start" font-size="11" fill="'+(isReject?rjColor:color)+'" font-weight="600">'+esc(lbl)+'</text>';
        const cond = isReject ? (f.on_reject || f.condition || '') : f.condition;
        if (cond) s += '<text x="'+lx+'" y="'+(yy+14)+'" text-anchor="start" font-size="10.5" fill="#6B7280">'+esc(truncW(cond, 10.5, 84))+'</text>';
      } else {
        s += '<text x="'+lx+'" y="'+ly+'" text-anchor="middle" font-size="11" fill="'+color+'" font-weight="600">'+esc(label)
          + (f.condition ? ' · '+esc(truncW(f.condition, 11, 130)) : '') + '</text>';
      }
    });

    // 步骤节点
    steps.forEach((st, i) => {
      const x = nx(i), y = ny(i);
      const kind = st.kind || 'TASK';
      const kc = SK[kind] || '#999';
      // 像素感知标签：先试 12.5px 两行；末行出现省略号（两行仍放不下）则降 11px 重排
      let lblFs = 12.5;
      let lines = wrapLines(st.label, lblFs, BW-24, 2);
      if (lines.length && lines[lines.length-1].indexOf('…') >= 0 && lblFs > 10.5){
        lblFs = 11;
        lines = wrapLines(st.label, lblFs, BW-24, 2);
      }
      const lh = 17;
      const ty = (lines.length === 2) ? 33 : 41;
      s += '<g>'
        + '<rect x="'+x+'" y="'+y+'" width="'+BW+'" height="'+BH+'" rx="10" fill="#fff" stroke="'+kc+'" stroke-width="1.6"/>'
        + '<text x="'+(x+10)+'" y="'+(y+18)+'" font-size="10" fill="'+kc+'" font-weight="600">'+(i+1)+' · '+esc(KIND_LABEL[kind]||kind)+'</text>';
      lines.forEach((ln, li) => {
        s += '<text x="'+(x+12)+'" y="'+(y+ty+li*lh)+'" font-size="'+lblFs+'" fill="#1A1B1C" font-weight="500">'+esc(ln)+'</text>';
      });
      if (st.actor) s += '<text x="'+(x+12)+'" y="'+(y+ty+lines.length*lh+6)+'" font-size="10" fill="#6B7280">'+esc(truncW('执行：'+st.actor, 10, BW-24))+'</text>';
      // 审批结果徽标（v9 M6）：节点右上角小圆点 + title
      if (st.approval_outcome && st.approval_outcome !== 'OTHER') {
        const ac = AO[st.approval_outcome] || '#999';
        s += '<circle cx="'+(x+BW-10)+'" cy="'+(y+12)+'" r="5" fill="'+ac+'" stroke="#fff" stroke-width="1.5">'
          + '<title>'+esc(st.approval_outcome)+'</title></circle>';
      }
      s += '</g>';
    });
    s += '</svg>';
    return s;
  }

  function esc(str){ return String(str==null?'':str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

  function render(){
    host.innerHTML='';
    const q = (es?es.value.trim():'');
    procs.forEach((p, pi) => {
      const steps = p.steps || [], flows = p.flows || [];
      const hit = !q || (p.name||'').includes(q)
        || steps.some(s => (s.label||'').includes(q) || (s.actor||'').includes(q))
        || flows.some(f => (f.condition||'').includes(q));
      if (q && !hit) return;
      const card = document.createElement('div');
      card.style.cssText = 'border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px;';
      let html = '<div style="font-size:15px;font-weight:600;color:var(--accent);margin-bottom:2px;">'+(pi+1)+'. '+esc(p.name)
        + (p.flow_type ? ' <span class="badge" style="background:'+(PFT[p.flow_type]||'#999')+'">'+p.flow_type+'</span>' : '')
        + (p.approval_chain && p.approval_chain.length ? ' <span style="font-size:11px;color:#6B7280;font-weight:400;">审批链：'+esc(p.approval_chain.join(' → '))+'</span>' : '')
        + '</div>';
      if (p.description) html += '<div style="font-size:12px;color:var(--sub);margin-bottom:10px;">'+esc(p.description)+'</div>';
      // 流程级前后置条件
      const pre = p.preconditions || [], post = p.postconditions || [];
      if (pre.length || post.length) {
        html += '<div style="font-size:12px;margin:0 0 8px;background:#F7F6F2;border:1px solid var(--line);border-radius:8px;padding:8px 10px;">'
          + '<div style="color:var(--sub);font-weight:600;margin-bottom:2px;">前后置条件</div>'
          + '<div>前置：'+(pre.length?pre.map(esc).join('；'):'<span style="color:#bbb">—</span>')+'</div>'
          + '<div>后置：'+(post.length?post.map(esc).join('；'):'<span style="color:#bbb">—</span>')+'</div>'
          + '</div>';
      }
      html += '<div style="margin-bottom:4px;">'+svgFlow(p, pi)+'</div>';
      // 步骤表
      html += '<div style="font-size:12px;color:var(--sub);font-weight:600;margin:10px 0 4px;">步骤（'+steps.length+'）</div>'
        + '<table style="font-size:12.5px;"><thead><tr><th>#</th><th>步骤</th><th>类型</th><th>执行者</th><th>泳道</th><th>审批结果</th><th>驳回至</th><th>子流程引用</th><th>evidence</th></tr></thead><tbody>';
      steps.forEach(st => {
        const kind = st.kind||'TASK';
        html += '<tr><td>'+(st.index!=null?st.index:'<span style="color:#bbb">—</span>')
          +'</td><td>'+esc(st.label)+'</td>'
          +'<td><span class="badge" style="background:'+(SK[kind]||'#999')+'">'+(kind)+'</span></td>'
          +'<td>'+(st.actor?esc(st.actor):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(st.lane?esc(st.lane):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(st.approval_outcome ? '<span class="badge" style="background:'+(AO[st.approval_outcome]||'#999')+'">'+st.approval_outcome+'</span>' : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(st.reject_to!=null?st.reject_to:'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(st.sub_process_ref?esc(st.sub_process_ref):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(st.evidence||'')+'</td></tr>';
      });
      html += '</tbody></table>';
      // 控制流表
      html += '<div style="font-size:12px;color:var(--sub);font-weight:600;margin:10px 0 4px;">控制流（'+flows.length+'）</div>'
        + '<table style="font-size:12.5px;"><thead><tr><th>from</th><th>to</th><th>类型</th><th>条件</th><th>驳回条件</th><th>evidence</th></tr></thead><tbody>';
      flows.forEach(f => {
        const ft = f.type||'SEQUENCE';
        html += '<tr><td>'+(f.from!=null?f.from:'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(f.to!=null?f.to:'<span style="color:#bbb">—</span>')+'</td>'
          +'<td><span class="badge" style="background:'+(FT[ft]||'#999')+'">'+ft+'</span></td>'
          +'<td>'+(f.condition?esc(f.condition):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(f.on_reject?esc(f.on_reject):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(f.evidence||'')+'</td></tr>';
      });
      html += '</tbody></table>';
      card.innerHTML = html;
      host.appendChild(card);
    });
  }
  if (es) es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('states');
  if (!host) return;
  const sts = DATA.states || [];
  const es = document.getElementById('sts');
  const BW=170, BH=72, GX=44, GY=134, X0=16, Y0=54, PER_ROW=3;
  const GUTTER = 240;                            // 右走廊宽度：跨行迁移标签区（防文字被画布右缘裁剪）
  function svgSM(sm, uid){
    const st = sm.states || [];
    if (!st.length) return '';
    const n = st.length;
    const rows = Math.ceil(n/PER_ROW), cols = Math.min(n, PER_ROW);
    const W = X0 + cols*(BW+GX) - GX + X0 + GUTTER, H = Y0 + rows*GY + 36;
    const rowOf = i => Math.floor(i/PER_ROW), colOf = i => i%PER_ROW;
    const nx = i => X0 + colOf(i)*(BW+GX), ny = i => Y0 + rowOf(i)*GY;
    const ncx = i => nx(i)+BW/2, ncy = i => ny(i)+BH/2;
    const R = X0 + cols*(BW+GX) - GX;
    let s = '<svg viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg" style="width:100%;height:auto;display:block;">'
      + '<defs><marker id="arrSM_'+uid+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
      + '<path d="M0,0 L10,5 L0,10 z" fill="#6B7280"/></marker></defs>';
    (sm.transitions || []).forEach(t => {
      const fi = t.from, ti = t.to;
      if (fi<0 || ti<0 || fi>=n || ti>=n) return;
      const x1 = nx(fi)+BW, y1 = ncy(fi), x2 = nx(ti), y2 = ncy(ti);
      let d, lx, ly, anchor = 'middle';
      if (rowOf(ti)===rowOf(fi) && ti>=fi) {
        d = 'M '+x1+' '+y1+' L '+(x2-8)+' '+y2; lx=(x1+x2)/2; ly=y1-12;
      } else if (ti>fi) {
        // 跨行：右折→下→从 ti 顶部进入（避免与同行迁移线在同一水平带重叠）
        d = 'M '+x1+' '+y1+' L '+(R+GUTTER/2)+' '+y1+' L '+(R+GUTTER/2)+' '+(ny(ti)-16)+' L '+ncx(ti)+' '+(ny(ti)-16)+' L '+ncx(ti)+' '+(ny(ti)+2);
        lx=R+GUTTER/2+6; ly=(y1+ny(ti)-16)/2; anchor='start';
      } else {
        // 回跳
        d = 'M '+x1+' '+y1+' L '+(R+GUTTER/2)+' '+y1+' L '+(R+GUTTER/2)+' '+(ny(ti)-16)+' L '+ncx(ti)+' '+(ny(ti)-16)+' L '+ncx(ti)+' '+(ny(ti)+2);
        lx=R+GUTTER/2+6; ly=(y1+ny(ti)-16)/2; anchor='start';
      }
      s += '<path d="'+d+'" fill="none" stroke="#6B7280" stroke-width="1.6" marker-end="url(#arrSM_'+uid+')"/>';
      if (anchor === 'start') {
        // 跨行/回跳：右走廊左对齐分两行，避免被画布右缘裁剪
        let yy = ly;
        if (t.event){ s += '<text x="'+lx+'" y="'+yy+'" text-anchor="start" font-size="9.5" fill="#6B7280">'+esc('事件:'+truncW(t.event, 9.5, 88))+'</text>'; yy += 13; }
        if (t.condition){ s += '<text x="'+lx+'" y="'+yy+'" text-anchor="start" font-size="9.5" fill="#6B7280">'+esc('若'+truncW(t.condition, 9.5, 96))+'</text>'; }
      } else {
        const lbl = (t.event?('事件:'+truncW(t.event, 9.5, 90)):'') + (t.condition?(' 若'+truncW(t.condition, 9.5, 120)):'');
        if (lbl) s += '<text x="'+lx+'" y="'+ly+'" text-anchor="middle" font-size="9.5" fill="#6B7280">'+esc(lbl)+'</text>';
      }
    });
    st.forEach((x, i) => {
      const k = x.initial ? '#16A085' : '#3E7CB1';
      // 像素感知标签：先试 12.5px 两行；仍放不下降 11px 重排
      let lblFs = 12.5;
      let lines = wrapLines(x.label, lblFs, BW-24, 2);
      if (lines.length && lines[lines.length-1].indexOf('…') >= 0 && lblFs > 10.5){
        lblFs = 11;
        lines = wrapLines(x.label, lblFs, BW-24, 2);
      }
      const lh = 17;
      const ty = (lines.length === 2) ? 34 : 42;
      s += '<g>'
        + '<rect x="'+nx(i)+'" y="'+ny(i)+'" width="'+BW+'" height="'+BH+'" rx="12" fill="#fff" stroke="'+k+'" stroke-width="'+(x.initial?'2.4':'1.6')+'"/>'
        + '<text x="'+(nx(i)+10)+'" y="'+(ny(i)+18)+'" font-size="10" fill="'+k+'" font-weight="600">'+(i+1)+' · '+(x.initial?'初始 ':'')+'状态</text>';
      lines.forEach((ln, li) => {
        s += '<text x="'+(nx(i)+12)+'" y="'+(ny(i)+ty+li*lh)+'" font-size="'+lblFs+'" fill="#1A1B1C" font-weight="500">'+esc(ln)+'</text>';
      });
      s += '</g>';
    });
    s += '</svg>';
    return s;
  }
  function render(){
    host.innerHTML = '';
    const q = es ? es.value.trim() : '';
    sts.forEach((sm, i) => {
      const st = sm.states || [];
      const hit = !q || (sm.object||'').includes(q) || (sm.name||'').includes(q)
        || st.some(x => (x.label||'').includes(q)) || (sm.transitions||[]).some(t => (t.event||'').includes(q));
      if (q && !hit) return;
      const card = document.createElement('div');
      card.style.cssText = 'border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:16px;';
      let html = '<div style="font-size:15px;font-weight:600;color:var(--accent);margin-bottom:2px;">'+(i+1)+'. '+esc(sm.name)+'</div>';
      html += '<div style="font-size:12px;color:var(--sub);margin-bottom:10px;">对象：'+esc(sm.object)+'</div>';
      html += '<div style="margin-bottom:4px;">'+svgSM(sm, i)+'</div>';
      html += '<div style="font-size:12px;color:var(--sub);font-weight:600;margin:10px 0 4px;">状态（'+st.length+'）</div>'
        + '<table style="font-size:12.5px;"><thead><tr><th>#</th><th>状态</th><th>初始</th><th>evidence</th></tr></thead><tbody>';
      st.forEach(x => {
        html += '<tr><td>'+(i)+'</td><td>'+esc(x.label)+'</td>'
          +'<td>'+(x.initial?'<span class="badge" style="background:#16A085">初始</span>':'<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(x.evidence||'')+'</td></tr>';
      });
      html += '</tbody></table>';
      html += '<div style="font-size:12px;color:var(--sub);font-weight:600;margin:10px 0 4px;">迁移（'+(sm.transitions||[]).length+'）</div>'
        + '<table style="font-size:12.5px;"><thead><tr><th>from</th><th>to</th><th>事件</th><th>条件</th><th>动作</th><th>evidence</th></tr></thead><tbody>';
      (sm.transitions||[]).forEach(t => {
        html += '<tr><td>'+(t.from!=null?t.from:'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(t.to!=null?t.to:'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(t.event?esc(t.event):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(t.condition?esc(t.condition):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(t.action?esc(t.action):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(t.evidence||'')+'</td></tr>';
      });
      html += '</tbody></table>';
      card.innerHTML = html;
      host.appendChild(card);
    });
  }
  if (es) es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('fntab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const OC = DATA.func_output_color || {};
  const fns = DATA.functions || [];
  const es = document.getElementById('fns');
  function render(){
    const q = es.value.trim();
    tb.innerHTML='';
    fns.filter(f => !q || (f.name||'').includes(q) || (f.subject||'').includes(q) || (f.formula||'').includes(q))
      .forEach(f => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td><b>'+esc(f.name)+'</b></td>'
          +'<td>'+(f.subject?esc(f.subject):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+esc(f.formula)+'</td>'
          +'<td>'+(f.inputs&&f.inputs.length?esc(f.inputs.join('、')):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td><span class="badge" style="background:'+(OC[f.output_type]||'#999')+'">'+(f.output_type||'OTHER')+'</span></td>'
          +'<td class="ev">'+esc(f.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('tptab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const TK = DATA.temporal_kind_color || {};
  const tps = DATA.temporal || [];
  const es = document.getElementById('tps');
  function render(){
    const q = es.value.trim();
    tb.innerHTML='';
    tps.filter(t => !q || (t.subject||'').includes(q) || (t.value||'').includes(q))
      .forEach(t => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td>'+esc(t.subject)+'</td>'
          +'<td><span class="badge" style="background:'+(TK[t.kind]||'#999')+'">'+(t.kind||'VALIDITY')+'</span></td>'
          +'<td><b>'+esc(t.value)+'</b></td>'
          +'<td>'+(t.anchor?esc(t.anchor):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(t.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('actab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const AL = DATA.action_level_color || {};
  const acs = DATA.actions || [];
  const es = document.getElementById('acs');
  function render(){
    const q = es.value.trim();
    tb.innerHTML='';
    acs.filter(a => !q || (a.name||'').includes(q) || (a.actor||'').includes(q) || (a.trigger||'').includes(q))
      .forEach(a => {
        const tr = document.createElement('tr');
        // 前后置条件
        tr.innerHTML = '<td><b>'+esc(a.name)+'</b></td>'
          +'<td>'+(a.actor?esc(a.actor):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td><span class="badge" style="background:'+(AL[a.level]||'#999')+'">'+(a.level||'OBSERVE')+'</span></td>'
          +'<td>'+(a.target?esc(a.target):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(a.side_effect?esc(a.side_effect):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(a.trigger?esc(a.trigger):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(a.precondition?esc(a.precondition):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(a.postcondition?esc(a.postcondition):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(a.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('cntab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const CT = DATA.constraint_type_color || {};
  const cns = DATA.constraints || [];
  const es = document.getElementById('cns');
  function render(){
    const q = es.value.trim();
    tb.innerHTML='';
    cns.filter(c => !q || (c.subject||'').includes(q) || (c.description||'').includes(q))
      .forEach(c => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td>'+esc(c.subject)+'</td>'
          +'<td><span class="badge" style="background:'+(CT[c.type]||'#999')+'">'+(c.type||'CONSISTENCY')+'</span></td>'
          +'<td>'+esc(c.description)+'</td>'
          +'<td class="ev">'+esc(c.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('pmtab');
  if (!host) return;
  const tb = host.querySelector('tbody');
  const PE = DATA.permission_effect_color || {};
  const AT = DATA.actor_type_color || {};
  const DS = DATA.data_scope_color || {};
  const pms = DATA.permissions || [];
  const es = document.getElementById('pms');
  function render(){
    const q = es.value.trim();
    tb.innerHTML='';
    pms.filter(p => !q || (p.actor||'').includes(q) || (p.action||'').includes(q) || (p.scope||'').includes(q))
      .forEach(p => {
        const tr = document.createElement('tr');
        tr.innerHTML = '<td>'+esc(p.actor)+'</td>'
          +'<td><b>'+esc(p.action)+'</b></td>'
          +'<td><span class="badge" style="background:'+(PE[p.effect]||'#999')+'">'+(p.effect||'DENY')+'</span></td>'
          +'<td>'+(p.scope?esc(p.scope):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(p.role?esc(p.role):'<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(p.actor_type ? '<span class="badge" style="background:'+(AT[p.actor_type]||'#999')+'">'+p.actor_type+'</span>' : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td>'+(p.data_scope ? '<span class="badge" style="background:'+(DS[p.data_scope]||'#999')+'">'+p.data_scope+'</span>' : '<span style="color:#bbb">—</span>')+'</td>'
          +'<td class="ev">'+esc(p.evidence||'')+'</td>';
        tb.appendChild(tr);
      });
  }
  es.addEventListener('input', render);
  render();
})();

(function(){
  const host = document.getElementById('graph');
  const gLegend = document.getElementById('g-legend');
  const modeBtns = Array.prototype.slice.call(document.querySelectorAll('[data-gmode]'));
  if (!DATA.relations.length) { host.innerHTML = '<div style="padding:40px;text-align:center;color:#6B7280;font-size:13px;">无关系数据，图谱为空。</div>'; return; }

  // ---------- 数据准备：节点（canonical 归一化）+ 边 ----------
  const nodeMap = {};
  DATA.entities.forEach(e => {
    const id = e.canonical || e.surface;
    if (!nodeMap[id]) nodeMap[id] = { id: id, label: e.surface || id, type: e.type || 'OTHER', deg: 0, x: 0, y: 0 };
  });
  function normId(name){
    if (!name) return null;
    if (nodeMap[name]) return name;
    const hit = DATA.entities.find(e => (e.surface||'') === name);
    return hit ? (hit.canonical || hit.surface) : null;
  }
  const rels = [];
  DATA.relations.forEach(r => {
    const s = normId(r.subject), o = normId(r.object);
    if (!s || !o || s === o) return;
    rels.push({ s: s, o: o, p: r.predicate || '', pk: r.prop_kind || '' });
  });
  rels.forEach(r => { nodeMap[r.s].deg++; nodeMap[r.o].deg++; });
  const connected = new Set();
  rels.forEach(r => { connected.add(r.s); connected.add(r.o); });
  const graphNodes = Object.keys(nodeMap).filter(k => connected.has(k)).map(k => nodeMap[k]);
  if (!graphNodes.length) { host.innerHTML = '<div style="padding:40px;text-align:center;color:#6B7280;font-size:13px;">无有效实体-关系对，图谱为空。</div>'; return; }

  // ---------- 谓词稳定配色（哈希 → HSL） ----------
  const predCache = {};
  function predColor(p){
    if (predCache[p]) return predCache[p];
    let h = 0; const s = String(p||'');
    for (let i=0;i<s.length;i++) h = (h*31 + s.charCodeAt(i)) % 360;
    const c = 'hsl('+h+',52%,40%)';
    predCache[p] = c; return c;
  }

  // ---------- 连通分量（BFS，子图拆分） ----------
  function components(){
    const adj = {};
    graphNodes.forEach(n => adj[n.id] = new Set());
    rels.forEach(r => { adj[r.s].add(r.o); adj[r.o].add(r.s); });
    const seen = new Set(), comps = [];
    graphNodes.forEach(n => {
      if (seen.has(n.id)) return;
      const q = [n.id]; seen.add(n.id); const ids = [];
      while (q.length){ const c = q.shift(); ids.push(c); (adj[c]||[]).forEach(nb => { if (!seen.has(nb)){ seen.add(nb); q.push(nb); } }); }
      const idSet = new Set(ids);
      comps.push({ nodes: graphNodes.filter(x => idSet.has(x.id)), edges: rels.filter(r => idSet.has(r.s) && idSet.has(r.o)) });
    });
    return comps.sort(function(a,b){ return b.nodes.length - a.nodes.length; });
  }
  const comps = components();

  // ---------- 力导向模拟（斥力 + 弹簧 + 中心引力） ----------
  function simulate(nodes, edges, W, H){
    const n = nodes.length;
    nodes.forEach(nd => { nd.vx = 0; nd.vy = 0; });
    nodes.forEach((nd, i) => {
      if (!nd.x && !nd.y){ const a = (2*Math.PI*i)/Math.max(1,n); nd.x = W/2 + Math.cos(a)*W*0.3; nd.y = H/2 + Math.sin(a)*H*0.3; }
    });
    const K = 0.04, REST = 120, CHARGE = -1300, GRAV = 0.016, DAMP = 0.82;
    for (let it = 0; it < 300; it++){
      for (let i=0;i<n;i++) for (let j=i+1;j<n;j++){
        let dx = nodes[i].x - nodes[j].x, dy = nodes[i].y - nodes[j].y;
        let d2 = dx*dx + dy*dy; if (d2 < 1) d2 = 1;
        const f = CHARGE / d2, d = Math.sqrt(d2);
        nodes[i].vx += (dx/d)*f; nodes[i].vy += (dy/d)*f;
        nodes[j].vx -= (dx/d)*f; nodes[j].vy -= (dy/d)*f;
      }
      edges.forEach(e => {
        const a = nodeMap[e.s], b = nodeMap[e.o];
        if (!a || !b) return;
        const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(0.01, Math.sqrt(dx*dx+dy*dy));
        const f = K*(d - REST);
        a.vx += (dx/d)*f; a.vy += (dy/d)*f;
        b.vx -= (dx/d)*f; b.vy -= (dy/d)*f;
      });
      nodes.forEach(nd => {
        nd.vx += (W/2 - nd.x)*GRAV; nd.vy += (H/2 - nd.y)*GRAV;
        nd.vx *= DAMP; nd.vy *= DAMP;
        nd.x += nd.vx; nd.y += nd.vy;
        nd.x = Math.max(20, Math.min(W-20, nd.x)); nd.y = Math.max(20, Math.min(H-20, nd.y));
      });
    }
  }

  // ---------- 平行边曲率分组 ----------
  const edgeGroup = {};
  rels.forEach((r,i) => { const k = r.s+'|'+r.o; (edgeGroup[k] = edgeGroup[k] || []).push(i); });
  const curveOf = {};
  Object.keys(edgeGroup).forEach(k => {
    const arr = edgeGroup[k], m = arr.length;
    arr.forEach((idx, j) => { curveOf[idx] = (j - (m-1)/2) * 15; });
  });

  // ---------- 全图渲染（交互：拖拽/缩放/平移/聚焦/悬停） ----------
  const W = 1000, H = 620;
  let zoom = 1, panX = 0, panY = 0, focused = null, hovered = null;
  let svg = null;

  function defsHTML(){
    const colors = new Set();
    rels.forEach(r => colors.add(predColor(r.p)));
    let s = '';
    colors.forEach(c => {
      s += '<marker id="garr'+c.replace(/[^0-9a-zA-Z]/g,'')+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="'+c+'"/></marker>';
    });
    return s;
  }
  function midId(c){ return 'garr'+c.replace(/[^0-9a-zA-Z]/g,''); }

  function edgePath(r, idx){
    const a = nodeMap[r.s], b = nodeMap[r.o];
    const dx = b.x - a.x, dy = b.y - a.y, d = Math.max(1, Math.sqrt(dx*dx+dy*dy));
    const nx = -dy/d, ny = dx/d, off = curveOf[idx] || 0;
    const cx = (a.x+b.x)/2 + nx*off, cy = (a.y+b.y)/2 + ny*off;
    return 'M'+a.x.toFixed(1)+','+a.y.toFixed(1)+' Q'+cx.toFixed(1)+','+cy.toFixed(1)+' '+b.x.toFixed(1)+','+b.y.toFixed(1);
  }
  function trunc(s, n){ s = String(s||''); return s.length>n ? s.slice(0,n)+'…' : s; }
  function escAttr(s){ return esc(s).replace(/&quot;/g,'&quot;'); }

  function renderFull(){
    const connectedSet = focused ? new Set([focused]) : null;
    if (focused) rels.forEach(r => { if (r.s===focused) connectedSet.add(r.o); if (r.o===focused) connectedSet.add(r.s); });
    let s = '<svg id="gsvg" viewBox="0 0 '+W+' '+H+'" xmlns="http://www.w3.org/2000/svg">'+defsHTML()
      + '<g id="viewport" transform="translate('+panX+','+panY+') scale('+zoom+')">';
    rels.forEach((r, idx) => {
      const c = predColor(r.p);
      const dim = focused ? (r.s===focused || r.o===focused ? 1 : 0.06) : (hovered && hovered!==r.s && hovered!==r.o ? 0.12 : 1);
      const bold = focused && (r.s===focused || r.o===focused);
      const sw = Math.max(0.8, Math.min(3.5, (bold?2.4:1.5))) / zoom;
      s += '<path d="'+edgePath(r, idx)+'" fill="none" stroke="'+c+'" stroke-width="'+sw.toFixed(2)+'"'
        + ' stroke-opacity="'+dim+'" marker-end="url(#'+midId(c)+')"'
        + ' data-s="'+escAttr(r.s)+'" data-o="'+escAttr(r.o)+'" data-p="'+escAttr(r.p)+'">'
        + '<title>'+esc(r.s)+' — '+esc(r.p)+' → '+esc(r.o)+'</title></path>';
    });
    graphNodes.forEach(nd => {
      // 反缩放：clamp 必须作用在【屏幕目标尺寸】上（不随 zoom），再除以 zoom 得世界尺寸，
      // 才能保证任意缩放级别下节点在屏幕上恒定大小（此前 clamp 世界坐标导致大 zoom 下节点反而放大）
      const rs = Math.max(6, Math.min(26, 10 + Math.min(9, nd.deg*0.9)));
      const r = rs / zoom;
      const fs = 11 / zoom;
      const sw2 = Math.max(1, 2 / zoom);
      const c = TC[nd.type] || '#999';
      const dim = focused ? (connectedSet.has(nd.id) ? 1 : 0.12) : (hovered && nd.id!==hovered ? 0.35 : 1);
      s += '<g class="gnode" data-k="'+escAttr(nd.id)+'" transform="translate('+nd.x.toFixed(1)+','+nd.y.toFixed(1)+')" opacity="'+dim+'">'
        + '<circle r="'+r.toFixed(2)+'" fill="'+c+'" stroke="#fff" stroke-width="'+sw2.toFixed(2)+'"/>'
        + '<text y="'+(r+12/zoom).toFixed(2)+'" text-anchor="middle" font-size="'+fs.toFixed(2)+'" font-weight="600" fill="#1A1B1C">'+esc(trunc(nd.label,16))+'</text>'
        + '<title>'+esc(nd.label)+' · '+esc(nd.type)+' · 度 '+nd.deg+'</title></g>';
    });
    s += '</g></svg>';
    host.innerHTML = s;
    svg = document.getElementById('gsvg');
    bindEvents();
  }

  function svgPoint(evt){
    const rect = svg.getBoundingClientRect();
    return { x: (evt.clientX - rect.left) * (W / rect.width), y: (evt.clientY - rect.top) * (H / rect.height) };
  }

  function bindEvents(){
    let down = null, dragNode = null, moved = false;
    svg.addEventListener('mousedown', function(evt){
      const g = evt.target.closest ? evt.target.closest('.gnode') : null;
      down = { x: evt.clientX, y: evt.clientY };
      moved = false;
      if (g){
        dragNode = g.getAttribute('data-k');
      } else {
        dragNode = null; down.px = panX; down.py = panY;
      }
    });
    document.addEventListener('mousemove', function(evt){
      if (!down) return;
      if (dragNode){
        if (Math.abs(evt.clientX-down.x) + Math.abs(evt.clientY-down.y) > 3) moved = true;
        const pt = svgPoint(evt);
        const nd = nodeMap[dragNode];
        nd.x = (pt.x - panX)/zoom; nd.y = (pt.y - panY)/zoom;
        renderFull();
      } else {
        panX = down.px + (evt.clientX - down.x);
        panY = down.py + (evt.clientY - down.y);
        renderFull();
      }
    });
    document.addEventListener('mouseup', function(){
      if (!down) return;
      if (dragNode && !moved){
        const k = dragNode;
        focused = (focused === k) ? null : k;
        renderFull();
      }
      down = null; dragNode = null;
    });
    svg.addEventListener('wheel', function(evt){
      evt.preventDefault();
      const pt = svgPoint(evt);
      const f = evt.deltaY < 0 ? 1.12 : 0.89;
      const nz = Math.max(0.12, Math.min(8, zoom*f));
      panX = pt.x - (pt.x - panX)*(nz/zoom);
      panY = pt.y - (pt.y - panY)*(nz/zoom);
      zoom = nz;
      renderFull();
    }, { passive: false });
    svg.addEventListener('mouseover', function(evt){
      const g = evt.target.closest ? evt.target.closest('.gnode') : null;
      const k = g ? g.getAttribute('data-k') : null;
      if (k && k !== hovered){ hovered = k; renderFull(); }
    });
    svg.addEventListener('mouseout', function(evt){
      const g = evt.target.closest ? evt.target.closest('.gnode') : null;
      if (g && g.getAttribute('data-k') === hovered){ hovered = null; renderFull(); }
    });
  }

  // ---------- 子图拆分渲染（每连通分量一张独立力导向小图） ----------
  function renderSub(){
    let s = '';
    comps.forEach((c, ci) => {
      const n = c.nodes.length;
      const h = Math.max(170, Math.min(420, n*44));
      const w = 1000;
      const sn = c.nodes.map(nd => ({ id: nd.id, label: nd.label, type: nd.type, deg: nd.deg, x: 0, y: 0 }));
      const sm = {}; sn.forEach(nd => sm[nd.id] = nd);
      simulate(sn, c.edges, w, h);
      const egs = {};
      c.edges.forEach((r, i) => { const k = r.s+'|'+r.o; (egs[k] = egs[k]||[]).push(i); });
      const crv = {};
      Object.keys(egs).forEach(k => { const arr = egs[k], m = arr.length; arr.forEach((j, q) => { crv[j] = (q-(m-1)/2)*15; }); });
      function subPath(r, idx){
        const a = sm[r.s], b = sm[r.o];
        const dx = b.x-a.x, dy = b.y-a.y, d = Math.max(1, Math.sqrt(dx*dx+dy*dy));
        const nx = -dy/d, ny = dx/d, off = crv[idx]||0;
        return 'M'+a.x.toFixed(1)+','+a.y.toFixed(1)+' Q'+((a.x+b.x)/2+nx*off).toFixed(1)+','+((a.y+b.y)/2+ny*off).toFixed(1)+' '+b.x.toFixed(1)+','+b.y.toFixed(1);
      }
      const colors = new Set(); c.edges.forEach(r => colors.add(predColor(r.p)));
      let defs = '';
      colors.forEach(c2 => { defs += '<marker id="sa'+ci+'_'+c2.replace(/[^0-9a-zA-Z]/g,'')+'" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="'+c2+'"/></marker>'; });
      let sv = '<svg viewBox="0 0 '+w+' '+h+'" xmlns="http://www.w3.org/2000/svg">'+defs;
      c.edges.forEach((r, idx) => {
        const c2 = predColor(r.p);
        sv += '<path d="'+subPath(r, idx)+'" fill="none" stroke="'+c2+'" stroke-width="1.5" marker-end="url(#sa'+ci+'_'+c2.replace(/[^0-9a-zA-Z]/g,'')+')" data-s="'+escAttr(r.s)+'" data-o="'+escAttr(r.o)+'" data-p="'+escAttr(r.p)+'"><title>'+esc(r.s)+' — '+esc(r.p)+' → '+esc(r.o)+'</title></path>';
      });
      sn.forEach(nd => {
        const rr = 9 + Math.min(8, nd.deg*0.8);
        sv += '<g class="gnode" data-k="'+escAttr(nd.id)+'" transform="translate('+nd.x.toFixed(1)+','+nd.y.toFixed(1)+')">'
          + '<circle r="'+rr+'" fill="'+(TC[nd.type]||'#999')+'" stroke="#fff" stroke-width="2"/>'
          + '<text y="'+(rr+11)+'" text-anchor="middle" font-size="10.5" font-weight="600" fill="#1A1B1C">'+esc(trunc(nd.label,14))+'</text>'
          + '<title>'+esc(nd.label)+' · '+esc(nd.type)+'</title></g>';
      });
      sv += '</svg>';
      s += '<div class="subgraph-card"><div style="font-size:13px;font-weight:600;color:var(--accent);margin:0 0 2px;">子图 '+(ci+1)+' · '+n+' 节点 / '+c.edges.length+' 条边</div>'
        + '<div style="font-size:11px;color:var(--sub);margin-bottom:6px;">'+c.nodes.map(nd=>esc(nd.label)).join('、')+'</div>'
        + sv + '</div>';
    });
    host.innerHTML = s;
    document.querySelectorAll('#graph .subgraph-card').forEach(card => {
      const ns2 = card.querySelectorAll('.gnode');
      const es2 = card.querySelectorAll('path');
      ns2.forEach(g => {
        g.addEventListener('mouseenter', function(){
          const k = g.getAttribute('data-k');
          es2.forEach(e => { const a = e.getAttribute('data-s'), b = e.getAttribute('data-o'); e.style.opacity = (a===k||b===k) ? 1 : 0.12; });
          ns2.forEach(o => { o.style.opacity = (o===g) ? 1 : 0.35; });
        });
        g.addEventListener('mouseleave', function(){
          es2.forEach(e => e.style.opacity = 1);
          ns2.forEach(o => o.style.opacity = 1);
        });
      });
    });
  }

  // ---------- 模式切换 + 图例 ----------
  let mode = 'full';
  function render(){ if (mode === 'full') renderFull(); else renderSub(); }
  modeBtns.forEach(b => b.addEventListener('click', function(){
    mode = b.getAttribute('data-gmode');
    modeBtns.forEach(x => x.classList.remove('on'));
    b.classList.add('on');
    render();
  }));
  function buildLegend(){
    let h = '';
    const tc = {};
    graphNodes.forEach(n => tc[n.type] = (tc[n.type]||0)+1);
    Object.keys(tc).forEach(t => {
      h += '<span class="gl-item"><span class="dot" style="background:'+(TC[t]||'#999')+'"></span>'+t+' ×'+tc[t]+'</span>';
    });
    h += '<span style="opacity:0.4">｜</span>';
    const pc = {};
    rels.forEach(r => pc[r.p] = (pc[r.p]||0)+1);
    Object.keys(pc).forEach(p => {
      h += '<span class="gl-item"><span class="dot" style="background:'+predColor(p)+'"></span>'+esc(p)+' ×'+pc[p]+'</span>';
    });
    gLegend.innerHTML = h;
  }
  buildLegend();
  simulate(graphNodes, rels, W, H);
  render();
})();

function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

function switchTab(t){
  document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('on'));
  const btn = document.querySelector('.tabs button[data-t="'+t+'"]');
  if (btn) btn.classList.add('on');
  const p = document.getElementById('p-'+t);
  if (p) p.classList.add('on');
}
document.querySelectorAll('.tabs button').forEach(b => {
  b.addEventListener('click', () => {
    const t = b.getAttribute('data-t');
    switchTab(t);
    try{ location.hash = t; }catch(e){}
  });
});
switchTab((location.hash||'').replace('#','') || 'graph');
</script>
</body>
</html>"""


def _literal_types():
    return {"DATE", "MONEY", "PERCENT", "QUANTITY"}


def _render(entities, relations, attributes, rules, processes, title: str,
            six: dict | None = None, meta: dict | None = None) -> str:
    from collections import Counter
    type_cnt = Counter(e.get("type", "OTHER") for e in entities)
    # 字面量口径：关系中 DATE/MONEY/PERCENT/QUANTITY 宾语 + 属性清单（字面量数据属性）
    literal_cnt = sum(
        1 for r in relations if r.get("object_type") in _literal_types()
    ) + len(attributes)
    type_keys = sorted(type_cnt)
    six = six or {"states": [], "functions": [], "temporal": [],
                  "actions": [], "constraints": [], "permissions": []}
    counts6 = {
        "states": len(six["states"]), "functions": len(six["functions"]),
        "temporal": len(six["temporal"]), "actions": len(six["actions"]),
        "constraints": len(six["constraints"]), "permissions": len(six["permissions"]),
    }
    summ6 = "预测决策本体（六件套，不入实体-边图）：" + " / ".join(
        f"{k} {v}" for k, v in [
            ("状态机", counts6["states"]), ("函数指标", counts6["functions"]),
            ("时态", counts6["temporal"]), ("动作处置", counts6["actions"]),
            ("约束", counts6["constraints"]), ("授权", counts6["permissions"]),
        ])
    payload = {"entities": entities, "relations": relations,
               "attributes": attributes, "rules": rules,
               "processes": processes,
               "document_meta": meta or {},
               "value_type_color": VALUE_TYPE_COLOR,
               "modality_color": MODALITY_COLOR,
               "step_kind_color": STEP_KIND_COLOR,
               "flow_type_color": FLOW_TYPE_COLOR,
               "proc_flow_type_color": PROC_FLOW_TYPE_COLOR,
               "approval_outcome_color": APPROVAL_OUTCOME_COLOR,
               "data_scope_color": DATA_SCOPE_COLOR,
               "func_output_color": FUNC_OUTPUT_COLOR,
               "temporal_kind_color": TEMPORAL_KIND_COLOR,
               "action_level_color": ACTION_LEVEL_COLOR,
               "constraint_type_color": CONSTRAINT_TYPE_COLOR,
               "permission_effect_color": PERMISSION_EFFECT_COLOR,
               "prop_kind_color": PROP_KIND_COLOR,
               "rule_type_color": RULE_TYPE_COLOR,
               "certainty_color": CERTAINTY_COLOR,
               "actor_type_color": ACTOR_TYPE_COLOR}
    payload.update(six)
    html = (_HTML_TPL
            .replace("__TITLE__", title)
            .replace("__NE__", str(len(entities)))
            .replace("__NR__", str(len(relations)))
            .replace("__NA__", str(len(attributes)))
            .replace("__NU__", str(len(rules)))
            .replace("__NP__", str(len(processes)))
            .replace("__NT__", str(len(type_keys)))
            .replace("__NL__", str(literal_cnt))
            .replace("__SUMM6__", summ6)
            .replace("__META__", _meta_line(meta or {}))
            .replace("__SUMM__", f"{len(entities)} 实体 / {len(relations)} 关系 / "
                                 f"{len(attributes)} 属性 / {len(rules)} 规则 / "
                                 f"{len(processes)} 流程")
            .replace("__DATA__", json.dumps(payload, ensure_ascii=False))
            .replace("__TYPECOLOR__", json.dumps(TYPE_COLOR)))
    return html


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="render_viewer.py", description=__doc__)
    p.add_argument("--in", dest="inp", required=True, help="契约 JSON 路径")
    p.add_argument("--out", dest="out", default=None, help="输出 HTML 路径")
    p.add_argument("--title", default=None, help="页面标题")
    args = p.parse_args(argv)

    inp = Path(args.inp)
    if not inp.exists():
        print(f"[render_viewer] 输入不存在：{inp}", file=sys.stderr)
        return 2
    try:
        data = json.loads(inp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"[render_viewer] 读取 JSON 失败：{exc}", file=sys.stderr)
        return 2

    entities, relations, attributes, rules, processes = _norm(data)
    six = _norm_six(data)
    if not entities and not relations:
        print("[render_viewer] 未识别到实体/关系数据（需 entities/relations 或 mentions/triplets）",
              file=sys.stderr)
        return 2

    out = Path(args.out) if args.out else inp.with_suffix(".html")
    title = args.title or f"smini 契约查看器 · {inp.stem}"
    out.write_text(
        _render(entities, relations, attributes, rules, processes, title, six,
                data.get("document_meta") or {}),
        encoding="utf-8")
    print(f"[render_viewer] 已生成 {out}（{len(entities)} 实体 / {len(relations)} 关系"
          f" / {len(attributes)} 属性 / {len(rules)} 规则 / {len(processes)} 流程"
          f" / {len(six['states'])} 状态机 / {len(six['functions'])} 函数指标"
          f" / {len(six['temporal'])} 时态 / {len(six['actions'])} 动作"
          f" / {len(six['constraints'])} 约束 / {len(six['permissions'])} 授权）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
