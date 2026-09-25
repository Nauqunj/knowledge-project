#!/usr/bin/env python3
"""组装 LangGraph 工作流（V3 完整版）。

拓扑：

    plan → collect → analyze → review ─[通过]────────────────→ organize → save → END
                                  ▲      │
                                  │      ├──[未通过, iteration < max]──→ revise ─┘（循环）
                                  │      │
                                  └──────┴──[未通过, iteration >= max]→ human_flag → END

    - 入口点：plan（Planner 输出策略写入 ``state["plan"]``）
    - plan → collect：策略先行，collector / organizer / reviewer 读 state["plan"]
    - analyze → review：分析完进入审核
    - review 三路条件路由 ``route_after_review``（max 取自 plan["max_iterations"]）：
        passed                     → organize
        未通过且 iteration < max    → revise（改写 analyses 后回到 review，形成循环）
        未通过且 iteration >= max   → human_flag（写 knowledge/pending_review/，转人工）
    - organize → save → END：审核通过后落盘主知识库
    - human_flag → END：异常出口，不污染主知识库

Usage:
    python workflows/graph.py

Environment:
    GITHUB_TOKEN / LLM_PROVIDER / *_API_KEY: 透传给节点内的采集与 LLM 调用。
    PLANNER_TARGET_COUNT: 目标采集量，决定 plan 档位。
    BUDGET_YUAN: LLM 成本预算（元），超预算抛 BudgetExceededError 并中断；
        结束时无论成败都会打印按节点分组的成本报告。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from langgraph.graph import END, StateGraph  # noqa: E402

from tests.cost_guard import BudgetExceededError  # noqa: E402
from workflows.human_flag import human_flag_node  # noqa: E402
from workflows.model_client import get_cost_guard  # noqa: E402
from workflows.nodes import (  # noqa: E402
    analyze_node,
    collect_node,
    organize_node,
    save_node,
)
from workflows.planner import planner_node  # noqa: E402
from workflows.reviewer import review_node  # noqa: E402
from workflows.reviser import revise_node  # noqa: E402
from workflows.state import KBState, MAX_ITERATIONS, initial_state  # noqa: E402

logger = logging.getLogger("graph")


def _max_iterations(state: KBState) -> int:
    """读取策略里的最大审核轮次，非法/缺失时回退到 ``MAX_ITERATIONS``。"""
    plan = state.get("plan", {}) or {}
    try:
        return int(plan.get("max_iterations", MAX_ITERATIONS))
    except (TypeError, ValueError):
        logger.warning("invalid plan.max_iterations %r; using %d", plan, MAX_ITERATIONS)
        return MAX_ITERATIONS


def route_after_review(state: KBState) -> str:
    """条件边：review 之后的三路分流。

    Args:
        state: 当前共享状态（读取 ``review_passed``、``iteration``、
            ``plan["max_iterations"]``）。

    Returns:
        - ``"organize"``：审核已通过，进入整理落盘；
        - ``"revise"``：未通过且 ``iteration < max_iterations``，改写后重审；
        - ``"human_flag"``：未通过且 ``iteration >= max_iterations``，转人工。
    """
    if state.get("review_passed"):
        return "organize"

    max_iterations = _max_iterations(state)
    if state.get("iteration", 0) >= max_iterations:
        logger.warning(
            "达到策略上限 max_iterations=%d，仍未通过，转人工复核", max_iterations
        )
        return "human_flag"
    return "revise"


def build_graph() -> Any:
    """构建并编译 LangGraph 工作流。

    Returns:
        编译后的可执行图（``CompiledStateGraph``），可用 ``.invoke`` /
        ``.stream`` 运行。
    """
    graph = StateGraph(KBState)

    graph.add_node("plan", planner_node)  # 【新增】入口：产出策略
    graph.add_node("collect", collect_node)
    graph.add_node("analyze", analyze_node)
    graph.add_node("review", review_node)
    graph.add_node("revise", revise_node)
    graph.add_node("organize", organize_node)
    graph.add_node("save", save_node)
    graph.add_node("human_flag", human_flag_node)

    graph.set_entry_point("plan")  # 【修改】入口从 collect 改为 plan
    graph.add_edge("plan", "collect")  # 【新增】plan → collect
    graph.add_edge("collect", "analyze")
    graph.add_edge("analyze", "review")
    graph.add_conditional_edges(
        "review",
        route_after_review,
        {"organize": "organize", "revise": "revise", "human_flag": "human_flag"},
    )
    graph.add_edge("revise", "review")  # review ⇄ revise 循环
    graph.add_edge("organize", "save")
    graph.add_edge("save", END)
    graph.add_edge("human_flag", END)

    return graph.compile()


def _describe(node_name: str, update: dict) -> str:
    """把某个节点的部分状态更新压缩成一行关键输出。"""
    update = update or {}
    if node_name == "plan":
        plan = update.get("plan", {}) or {}
        return (
            f"tier={plan.get('tier')} target={plan.get('target_count')} "
            f"max_iterations={plan.get('max_iterations')}"
        )
    if node_name == "collect":
        return f"sources={len(update.get('sources', []))}"
    if node_name == "analyze":
        return f"analyses={len(update.get('analyses', []))}"
    if node_name == "review":
        return (
            f"passed={update.get('review_passed')} "
            f"iteration={update.get('iteration')} "
            f"feedback={update.get('review_feedback', '')[:60]!r}"
        )
    if node_name == "revise":
        return f"analyses={len(update.get('analyses', []))}"
    if node_name == "organize":
        return f"articles={len(update.get('articles', []))}"
    if node_name == "save":
        return "已写入 knowledge/articles/"
    if node_name == "human_flag":
        return f"needs_human_review={update.get('needs_human_review')}"
    return str(update)


def _print_cost_report(guard: Any) -> None:
    """打印按节点分组的 LLM 成本报告（收尾必打，即使中途异常）。"""
    report = guard.get_report()
    totals = report["totals"]
    print("\n=== LLM 成本报告 ===")
    if not report["by_node"]:
        print("无 LLM 调用记录。")
        return

    print(f"{'node':<12}{'calls':>7}{'prompt':>12}{'completion':>14}{'cost(元)':>14}")
    print("-" * 60)
    for name, bucket in report["by_node"].items():
        print(
            f"{name:<12}{bucket['calls']:>7}{bucket['prompt_tokens']:>12}"
            f"{bucket['completion_tokens']:>14}{bucket['cost_yuan']:>14.6f}"
        )
    print("-" * 60)
    print(
        f"{'TOTAL':<12}{totals['calls']:>7}{totals['prompt_tokens']:>12}"
        f"{totals['completion_tokens']:>14}{totals['cost_yuan']:>14.6f}"
    )
    print(
        f"预算 {report['budget_yuan']:.6f} 元，"
        f"预警线 {report['alert_threshold']:.0%}，"
        f"已用 {totals['cost_yuan'] / report['budget_yuan']:.1%}。"
    )


def main(argv: list[str] | None = None) -> int:
    """流式执行工作流并打印每个节点的关键输出。

    Args:
        argv: 未使用（保留 CLI 兼容）。

    Returns:
        进程退出码（始终为 ``0``）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    app = build_graph()
    print(f"graph nodes: {sorted(app.get_graph().nodes)}")

    guard = get_cost_guard()
    state = initial_state()
    try:
        for step, chunk in enumerate(app.stream(state, stream_mode="updates"), start=1):
            for node_name, update in (chunk or {}).items():
                print(f"[step {step}] {node_name:<8} {_describe(node_name, update)}")
    except BudgetExceededError as exc:
        logger.error(
            "成本超预算，流水线中断：已用 %.6f 元 / 预算 %.6f 元",
            exc.report["used_yuan"],
            exc.report["budget_yuan"],
        )
    finally:
        _print_cost_report(guard)

    print("工作流结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
