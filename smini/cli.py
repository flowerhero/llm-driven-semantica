"""smini.cli — 命令行入口。零依赖，可在宿主 agent / 任意终端运行。

用法
----
    # 跑 extract-only 流水线（ingest → extract → build_kg），内置样例文本
    python -m smini.cli build --sample

    # 自定义文本 / 文件
    python -m smini.cli build "text://特斯拉公司成立于2003年，总部位于美国。马斯克是特斯拉的CEO。"
    python -m smini.cli build "file:///path/to/doc.md"

    # 宿主 agent（豆包）充当 LLM：把按契约产出的 entities/relations/...
    # JSON 交卷文件传入，Python 薄壳只做确定性映射（无需任何 SMINI_LLM_* 凭证）
    python -m smini.cli build "file:///path/to/doc.md" --host-contract contract.json

    # 导出 JSON（整条 state，便于审计/复现）
    python -m smini.cli build "text://..." --json out.json

    # 只落盘抽取结果（04-extraction.json 单篇契约，可直接进 ids/validate）
    python -m smini.cli build "file:///path/to/doc.md" --host-contract contract.json \\
        --extract-out runs/<run_id>/04-extraction.json

退出码：0 成功，1 流水线失败，2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .pipeline_factory import build_pipeline, make_context
from .types import to_dict


_SAMPLE = (
    "特斯拉公司成立于2003年，总部位于美国加利福尼亚州。马斯克是特斯拉的CEO。"
    "该公司2023年营收约967亿美元，估值一度超过1万亿美元。"
    "OpenAI 是一家人工智能公司，总部位于美国旧金山。山姆·奥特曼是 OpenAI 的 CEO。"
)

# extract-only 架构下确定性运行时原子命令，由 Skill 调用。
_RUNTIME_CMDS = frozenset({"ids", "validate", "graph"})


def _build(argv: Sequence[str]) -> int:
    p = argparse.ArgumentParser(prog="smini build", description="运行 extract-only 抽取流水线")
    p.add_argument("sources", nargs="*", help="数据源 spec：text://... / file://... / 路径 / inline")
    p.add_argument("--stop", default=None, help="只跑到该步（含），如 extract")
    p.add_argument("--seed", type=int, default=0, help="随机种子（注入 ctx）")
    p.add_argument("--json", default=None, help="把整条 state 导出为 JSON 文件")
    p.add_argument("--extract-out", default=None,
                   help="把抽取结果落盘为 04-extraction.json（单篇 ExtractionResult 契约；"
                        "多篇文档时仅导出第一篇，其余请用 --json 导出 state）")
    p.add_argument("--sample", action="store_true", help="使用内置样例文本")
    p.add_argument("--host-contract", default=None,
                   help="宿主 agent（豆包）充当 LLM：宿主按契约产出的抽取 JSON 路径"
                        "（{entities, relations, attributes, rules, processes, ...} 十一件套）；"
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

    ctx = make_context(sources, seed=args.seed, llm=llm)
    pipeline = build_pipeline(sources, llm=llm)

    try:
        state, results = pipeline.run(ctx=ctx, stop_after=args.stop)
    except Exception as exc:  # noqa: BLE001
        print(f"[pipeline] 失败：{exc}", file=sys.stderr)
        return 1

    if not args.quiet:
        print(pipeline.summary(results))
        print("-" * 40)

    # 图谱统计（build_kg 消费层产出）
    if state.graph is not None:
        s = state.graph.stats
        print(f"图谱：{s.entity_count} 实体 / {s.edge_count} 边 "
              f"(字面量 {s.literal_edge_count}，孤立 {s.orphan_entity_count})")
        if s.by_type:
            top = sorted(s.by_type.items(), key=lambda kv: kv[1], reverse=True)[:6]
            print("  类型分布:", ", ".join(f"{k}={v}" for k, v in top))

    if args.extract_out:
        exs = state.extractions
        if not exs:
            print("[extract-out] 无抽取结果可导出（宿主未交卷 --host-contract 时抽取为空）",
                  file=sys.stderr)
        else:
            if len(exs) > 1:
                print(f"[extract-out] 警告：共 {len(exs)} 篇文档，仅导出第一篇 → {args.extract_out}",
                      file=sys.stderr)
            with open(args.extract_out, "w", encoding="utf-8") as f:
                json.dump(to_dict(exs[0]), f, ensure_ascii=False, indent=2, default=str)
            print(f"\n[extract-out] 已导出抽取结果 → {args.extract_out}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(to_dict(state), f, ensure_ascii=False, indent=2, default=str)
        print(f"\n[json] 已导出 state → {args.json}")
    return 0


def _print_usage() -> None:
    print(
        "用法: python -m smini.cli <子命令> [参数]\n"
        "\n"
        "流水线（extract-only，契约即规则）:\n"
        "  build / run   运行抽取流水线（--sample 用内置样例，--host-contract 交卷契约，"
        "--help 看全部参数）\n"
        "\n"
        "确定性运行时（Skill 做不到的计算，由 Python 接管）:\n"
        "  ids           补算内容寻址 ID（extract）\n"
        "  validate      按 JSON Schema + 领域约束校验产物（extract）\n"
        "  graph         build 建图（惰性锚点消费层）\n"
        "\n"
        "示例:\n"
        "  python -m smini.cli build --sample\n"
        "  python -m smini.cli build --sample --host-contract contract.json\n"
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
    # 确定性运行时：ids / validate / graph
    if cmd in _RUNTIME_CMDS:
        from .runtime import run as _runtime_run
        return _runtime_run(argv)
    print(f"未知子命令：{cmd}", file=sys.stderr)
    _print_usage()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
