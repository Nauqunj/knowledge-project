#!/usr/bin/env python3
"""Reviewer 节点：对 ``state["analyses"]`` 做五维度加权审核。

与 ``workflows.nodes.review_node`` 的区别：本审核的对象是**分析结果**
``analyses``（articles 由 organizer 生成），且总分由代码按权重重算，
不信任模型的算术；LLM 调用失败时**自动通过**，不阻塞流程。

维度与权重（各维度 1-10 整数分）：
    summary_quality  摘要质量  25%
    technical_depth  技术深度  25%
    relevance        相关性    20%
    originality      原创性    15%
    formatting       格式规范  15%

加权总分 >= 7.0 判定通过；仅审核前 ``REVIEW_LIMIT`` 条以控制 token。

依赖：``workflows.model_client.chat_json`` / ``accumulate_usage``，
``workflows.state.KBState``。
"""

from __future__ import annotations

import copy
import json
import logging
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.model_client import accumulate_usage, chat_json  # noqa: E402
from workflows.state import KBState, MAX_ITERATIONS  # noqa: E402

logger = logging.getLogger("reviewer")

# 维度权重（合计 1.0）；用代码重算总分，不采用模型给出的总分。
DIMENSION_WEIGHTS: dict[str, float] = {
    "summary_quality": 0.25,
    "technical_depth": 0.25,
    "relevance": 0.20,
    "originality": 0.15,
    "formatting": 0.15,
}

PASS_THRESHOLD = 7.0
REVIEW_LIMIT = 5
DIM_MIN = 1
DIM_MAX = 10
REVIEW_TEMPERATURE = 0.1

REVIEW_SYSTEM_PROMPT = (
    "你是知识库质量审核员。请对给定的分析结果从五个维度各打 1-10 的整数分，"
    "并给出可执行的改进反馈。只输出一个 JSON 对象，不要输出任何多余文字或 "
    "Markdown 代码块，格式："
    '{"dimensions": {"summary_quality": int, "technical_depth": int, '
    '"relevance": int, "originality": int, "formatting": int}, '
    '"feedback": "..."}'
)


def _dim_score(value: Any) -> float:
    """把模型给出的单维度分数夹到 1-10；缺失/非数字计 0 分。

    Args:
        value: 模型返回的原始维度分。

    Returns:
        1-10 的分数；无法解析时返回 ``0.0``（按不达标处理）。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(max(DIM_MIN, min(DIM_MAX, value)))


def weighted_total(dimensions: dict[str, Any]) -> tuple[float, dict[str, float]]:
    """用代码按权重重算加权总分。

    Args:
        dimensions: 形如 ``{"summary_quality": 8, ...}`` 的维度分。

    Returns:
        ``(total, scores)``：加权总分（1-10）与各维度归一化后的分数。
    """
    scores = {name: _dim_score(dimensions.get(name)) for name in DIMENSION_WEIGHTS}
    total = sum(DIMENSION_WEIGHTS[name] * scores[name] for name in DIMENSION_WEIGHTS)
    return round(total, 4), scores


def _auto_pass(reason: str) -> dict:
    """构造「自动通过」的判定（LLM 失败或无内容时不阻塞流程）。"""
    logger.warning("review auto-passed: %s", reason)
    return {"passed": True, "total": None, "scores": {}, "feedback": reason}


def _review_analyses(
    analyses: list[dict],
    plan: dict,
) -> tuple[dict, Any | None]:
    """调用 LLM 审核前若干条 analyses，并重算加权总分。

    Args:
        analyses: 待审核的分析结果。
        plan: 采集/分析计划，用于判断相关性。

    Returns:
        ``(verdict, usage)``；LLM 调用失败时 ``verdict`` 为自动通过、usage 为
        ``None``。
    """
    sample = analyses[:REVIEW_LIMIT]
    digest = [
        {
            "title": item.get("title"),
            "summary": item.get("summary"),
            "tags": item.get("tags"),
            "score": item.get("score"),
            "highlights": item.get("highlights"),
        }
        for item in sample
    ]

    plan_text = ""
    if plan:
        plan_text = f"采集/分析计划：{json.dumps(plan, ensure_ascii=False)}\n\n"
    prompt = (
        f"{plan_text}"
        f"请审核以下 {len(digest)} 条分析结果"
        f"（共 {len(analyses)} 条，仅审核前 {REVIEW_LIMIT} 条）：\n"
        f"{json.dumps(digest, ensure_ascii=False, indent=2)}"
    )

    try:
        data, usage = chat_json(
            prompt, system=REVIEW_SYSTEM_PROMPT, temperature=REVIEW_TEMPERATURE
        )
    except (RuntimeError, ValueError) as exc:
        return _auto_pass(f"审核调用失败，自动通过：{exc}"), None

    total, scores = weighted_total(data.get("dimensions") or {})
    breakdown = " ".join(f"{name}={scores[name]:.0f}" for name in DIMENSION_WEIGHTS)
    feedback = str(data.get("feedback", "")).strip()
    detail = f"[加权总分 {total:.2f}/10；{breakdown}]"
    return {
        "passed": total >= PASS_THRESHOLD,
        "total": total,
        "scores": scores,
        "feedback": f"{detail} {feedback}".strip(),
    }, usage


def review_node(state: KBState) -> dict:
    """审核节点：五维度加权审核 ``analyses`` 并推进 ``iteration``。

    Args:
        state: 当前共享状态（读取 ``plan``、``analyses``、``iteration``、
            ``cost_tracker``）。

    Returns:
        部分状态更新 ``{"review_passed": bool, "review_feedback": str,
        "iteration": int, "cost_tracker": dict}``。LLM 失败或无内容时
        ``review_passed`` 为 ``True``（自动通过）。
    """
    analyses = state.get("analyses", [])
    plan = state.get("plan") or {}
    iteration = state.get("iteration", 0)
    print(
        f"[review_node] 审核第 {iteration + 1}/{MAX_ITERATIONS} 轮："
        f"analyses={len(analyses)}（前 {REVIEW_LIMIT} 条）"
    )
    tracker = copy.deepcopy(state.get("cost_tracker") or {})

    if not analyses:
        verdict = _auto_pass("无可审核的 analyses，自动通过。")
    else:
        verdict, usage = _review_analyses(analyses, plan)
        if usage is not None:
            accumulate_usage(tracker, usage)

    next_iteration = iteration + 1
    total = verdict["total"]
    total_display = f"{total:.2f}" if isinstance(total, (int, float)) else "n/a"
    print(
        f"[review_node] iteration={iteration}(本轮) -> {next_iteration}, "
        f"加权总分={total_display}, review_passed={verdict['passed']}"
    )
    logger.info(
        "review_node: passed=%s total=%s feedback=%s",
        verdict["passed"],
        total_display,
        verdict["feedback"] or "（无）",
    )

    return {
        "review_passed": verdict["passed"],
        "review_feedback": verdict["feedback"],
        "iteration": next_iteration,
        "cost_tracker": tracker,
    }


__all__ = [
    "DIMENSION_WEIGHTS",
    "PASS_THRESHOLD",
    "REVIEW_LIMIT",
    "review_node",
    "weighted_total",
]
