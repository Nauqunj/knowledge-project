#!/usr/bin/env python3
"""LangGraph 工作流的共享状态定义。

本模块遵循「报告式通信」原则：状态中的每个字段都是**结构化摘要**，
而不是上游的原始数据。节点之间只传递提炼后的报告，避免把整段 API
响应、网页正文或模型原始输出塞进图里。

字段只描述「报告长什么样」，不负责校验；校验由各节点/工具完成。

数据流向（V3 完整 · 7 节点）：

    plan → sources → analyses → review ─[通过]────→ organize → END
                          ▲       │
                          │       ├──[未通过]──────→ revise（循环回 review）
                          │       │
                          └───────┴──[超过 max]───→ human_flag → END
"""

from __future__ import annotations

from typing import TypedDict

# 审核循环上限：超过该次数仍未通过则转人工复核。
MAX_ITERATIONS = 3


class KBState(TypedDict):
    """知识库流水线在 LangGraph 中流转的共享状态。

    「报告式通信」：所有字段均为结构化摘要，不存放原始数据。
    """

    # 采集/分析策略（Planner 输出；collector / organizer / reviewer 通过
    # ``state["plan"]`` 读取策略）。可为空 dict。
    # 格式：dict，例如 {"topic": str, "goals": list[str], "sources": list[str]}
    # ← 11-3 新增
    plan: dict

    # 采集到的原始数据（已做初步过滤/去重的条目摘要，不含完整 API 响应）。
    # 格式：list[dict]，每项至少含
    #   {"id": str, "title": str, "source_url": str, "source": str,
    #    "description": str, "collected_at": str, "metadata": dict}
    sources: list[dict]

    # LLM 分析后的结构化结果（每条只保留提炼后的字段，不含模型原文）。
    # 格式：list[dict]，每项含
    #   {"id": str, "summary": str, "score": int, "score_reason": str,
    #    "tags": list[str], "highlights": list[str], "analyzed_at": str}
    analyses: list[dict]

    # 格式化、去重后、待落盘的知识条目（对外契约 schema 的摘要视图）。
    # 格式：list[dict]，每项含
    #   {"id": str, "title": str, "source_url": str, "summary": str,
    #    "tags": list[str], "status": str, "score": int, "organized_at": str}
    articles: list[dict]

    # 审核（Supervisor）给出的反馈意见，用于下一轮重做；通过时通常为空串。
    # 格式：str
    review_feedback: str

    # 审核是否通过；未通过则带着 review_feedback 进入下一轮。
    # 格式：bool
    review_passed: bool

    # 当前审核循环次数，最多 MAX_ITERATIONS 次；超限则转人工复核。
    # 格式：int，从 0 开始递增
    iteration: int

    # 是否已转人工复核：审核循环超限未通过时由 HumanFlag 节点置为 True。
    # 格式：bool
    needs_human_review: bool

    # Token 用量与成本追踪（汇总后的报告，不是逐次调用的原始明细）。
    # 格式：dict，例如
    #   {"calls": int, "prompt_tokens": int, "completion_tokens": int,
    #    "total_tokens": int, "cost_cny": float, "by_provider": dict}
    cost_tracker: dict


def initial_state(
    *,
    plan: dict | None = None,
    sources: list[dict] | None = None,
    analyses: list[dict] | None = None,
    articles: list[dict] | None = None,
) -> KBState:
    """构造一份带默认值的初始状态，便于建图与测试。

    Args:
        plan: 采集/分析策略；默认空 dict。
        sources: 初始采集数据；默认空列表。
        analyses: 初始分析结果；默认空列表。
        articles: 初始知识条目；默认空列表。

    Returns:
        所有字段均已填充的 :class:`KBState`。
    """
    return KBState(
        plan=dict(plan or {}),
        sources=list(sources or []),
        analyses=list(analyses or []),
        articles=list(articles or []),
        review_feedback="",
        review_passed=False,
        iteration=0,
        needs_human_review=False,
        cost_tracker={
            "calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost_cny": 0.0,
            "by_provider": {},
        },
    )


__all__ = ["KBState", "initial_state", "MAX_ITERATIONS"]
