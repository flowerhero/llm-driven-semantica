"""smini.cli — 命令行入口。零依赖，可在 WorkBuddy / 任意终端运行。

用法
----
    # 跑完整 8 步，内置样例文本
    python -m smini.cli build "text://特斯拉公司成立于2003年，总部位于美国。马斯克是特斯拉的CEO。"

    # 带查询，输出可直接喂给 Agent 的上下文包
    python -m smini.cli build "file:///path/to/doc.md" -q "特斯拉 总部"

    # 宿主 agent（豆包）充当 LLM：把按契约产出的 entities/relations JSON
    # 交卷文件传入，Python 薄壳只做确定性映射（无需任何 SMINI_LLM_* 凭证）
    python -m smini.cli build "file:///path/to/doc.md" --host-contract contract.json

    # 只跑到某一步（调试）
    python -m smini.cli build "text://..." --stop extract

    # 导出 JSON（整条 state，便于审计/复现）
    python -m smini.cli build "text://..." --json out.json

退出码：0 成功，1 流水线失败，2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .pipeline_factory import build_pipeline, make_context
from .protocols import RunContext
from .types import to_dict


_SAMPLE = (
    "特斯拉公司成立于2003年，总部位于美国加利福尼亚州。马斯克是特斯拉的CEO。"
    "该公司2023年营收约967亿美元，估值一度超过1万亿美元。"
    "OpenAI 是一家人工智能公司，总部位于美国旧金山。山姆·奥特曼是 OpenAI 的 CEO。"
)

# Skill-First 架构下的确定性运行时原子命令，由 Skill 调用。
_RUNTIME_CMDS = frozenset(
    {"ingest", "ids", "spans", "validate", "normalize", "graph", "deliver", "fallback"}
)


def _build(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="smini build", description="运行 semantica 精简流水线")
    p.add_argument("sources", nargs="*", help="数据源 spec：text://... / file://... / 路径 / inline")
    p.add_argument("-q", "--query", default=None, help="交付查询（触发 Deliver 步）")
    p.add_argument("--stop", default=None, help="只跑到该步（含），如 extract")
    p.add_argument("--seed", type=int, default=0, help="随机种子（注入 ctx）")
    p.add_argument("--json", default=None, help="把整条 state 导出为 JSON 文件")
    p.add_argument("--sample", action="store_true", help="使用内置样例文本")
    p.add_argument("--host-contract", default=None,
                   help="宿主 agent（豆包）充当 LLM：宿主按契约产出的抽取 JSON 路径"
                        "（{entities:[{surface,canonical,type}], relations:[{subject,predicate,object,object_type,evidence}]}）；"
                        "不传则抽取为空（零规则架构无确定性兜底）")
    p.add_argument("--quiet", action="store_true", help="只输出最终结果")
    args = p.parse_args(argv)

    sources = list(args.sources)
    if args.sample:
        sources = [_SAMPLE]
    if not sources:
        p.error("至少需要一个数据源，或用 --sample")

    llm = None
    if args.host_contract:
        try:
            with open(args.host_contract, encoding="utf-8") as f:
                contract = json.load(f)
        except (OSError, ValueError) as exc:
            print(f"[llm] 读取宿主契约失败：{exc}", file=sys.stderr)
            return 2
        from .llm import HostAgentLLMProvider
        llm = HostAgentLLMProvider(contract=contract)
    else:
        print("[extract] 宿主未喂入契约（--host-contract）：宿主 agent 充当 LLM 时需交卷"
              "，零规则架构下抽取将为空（无确定性兜底）", file=sys.stderr)

    ctx = make_context(sources, args.query, seed=args.seed, llm=llm)
    pipeline = build_pipeline(sources, query=args.query, llm=llm)

    try:
        state, results = pipeline.run(ctx=ctx, stop_after=args.stop)
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 失败：{exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(pipeline.summary(results))
        print("-" * 40)

    # 图谱统计
    if state.graph is not None:
        s = state.graph.stats
        print(f"图谱：{s.entity_count} 实体 / {s.edge_count} 边 "
              f"(字面量 {s.literal_edge_count}，孤立 {s.orphan_entity_count})")
        if s.by_type:
            top = sorted(s.by_type.items(), key=lambda kv: kv[1], reverse=True)[:6]
            print("  类型分布:", ", ".join(f"{k}={v}" for k, v in top))
    # QA 摘要
    if state.qa is not None:
        m = state.qa.metrics
        print(f"QA：{m.conflict_count} 冲突 / {m.duplicate_cluster_count} 重复集群 "
              f"/ 合并 {m.entities_merged} 实体（未决 {m.unresolved_count}）")
        for c in state.qa.conflicts:
            if c.resolution:
                print(f"  冲突 {c.subject_id}/{c.predicate}: 采纳「{c.resolution.chosen}」"
                      f" — {c.resolution.rationale}")
    # 回执
    if state.receipt is not None:
        r = state.receipt
        print(f"存储：backend={r.backend} 实体+{r.entities_written}/~{r.entities_updated} "
              f"边+{r.edges_written}/~{r.edges_updated} 向量+{r.vectors_written}")

    # 交付
    if state.delivered is not None:
        print("=" * 40)
        print(state.delivered.render())

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(to_dict(state), f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[json] 已导出 state → {args.json}")
    return 0


def _print_usage() -> None:
    print(
        "用法: python -m smini.cli <子命令> [参数]\n"
        "\n"
        "流水线（端到端，确定性回退路径）:\n"
        "  build / run   运行 semantica 精简流水线（--sample 用内置样例，"
        "-q 指定查询，--help 看全部参数）\n"
        "\n"
        "确定性运行时（Skill 做不到的计算，由 Python 接管）:\n"
        "  ingest        把数据源摄入为 RawDocument（text:// / file:// / 路径）\n"
        "  fallback       跑某步的确定性兜底实现（parse / normalize / extract）\n"
        "  ids           补算/校验内容寻址 ID（raw / extract）\n"
        "  spans         由 LLM 提议的区块文本+类型补算字符坐标（parse）\n"
        "  validate       按 JSON Schema + 领域约束校验产物\n"
        "  graph          build / qa / store 三个子操作\n"
        "  deliver        在图谱上检索并组装 ContextPackage\n"
        "  normalize      对归一化 patch 提议做几何校验与坐标重映射\n"
        "\n"
        "示例:\n"
        "  python -m smini.cli build --sample -q \"特斯拉 总部\"\n"
        "  python -m smini.cli build --help\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["build", "--sample"]
    cmd, *rest = argv
    if cmd in ("--help", "-h"):
        _print_usage()
        return 0
    if cmd in ("build", "run"):
        return _build(rest)
    # 确定性运行时：ingest / ids / validate / normalize / graph / deliver / fallback / spans
    if cmd in _RUNTIME_CMDS:
        from .runtime import run as _runtime_run
        return _runtime_run(argv)
    print(f"未知子命令：{cmd}", file=sys.stderr)
    _print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
