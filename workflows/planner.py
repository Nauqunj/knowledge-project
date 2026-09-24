#!/usr/bin/env python3
"""Planner 节点：根据目标采集量产出采集/分析策略。

``plan_strategy`` 按目标量返回三档策略之一（数值越低越「精确优先」，
越高越「覆盖优先」）：

    lite     target < 10      每源 5 条，阈值 0.70，最多 1 轮修订
    standard 10 <= target < 20 每源 10 条，阈值 0.50，最多 2 轮修订
    full     target >= 20     每源 20 条，阈值 0.40，最多 3 轮修订

策略写入 ``state["plan"]``，供下游 collector / organizer / reviewer 读取。
目标量来自参数，缺省时读环境变量 ``PLANNER_TARGET_COUNT``（默认 10）。

依赖：``workflows.state.KBState``。
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.state import KBState  # noqa: E402

logger = logging.getLogger("planner")

ENV_TARGET_COUNT = "PLANNER_TARGET_COUNT"
DEFAULT_TARGET_COUNT = 10

# 各档策略的固定参数；tier / target_count 由 plan_strategy 动态补上。
STRATEGY_TIERS: dict[str, dict] = {
    "lite": {
        "per_source_limit": 5,
        "relevance_threshold": 0.7,
        "max_iterations": 1,
        "rationale": "目标量小，追求精确：每源少取、相关性阈值高、只允许 1 轮修订，"
        "以最低成本拿到高置信样本。",
    },
    "standard": {
        "per_source_limit": 10,
        "relevance_threshold": 0.5,
        "max_iterations": 2,
        "rationale": "中等目标，平衡覆盖与精度：每源取 10 条、阈值 0.5、允许 2 轮修订，"
        "兼顾召回与质量。",
    },
    "full": {
        "per_source_limit": 20,
        "relevance_threshold": 0.4,
        "max_iterations": 3,
        "rationale": "目标量大，覆盖优先：每源多取、阈值放宽到 0.4、允许 3 轮修订，"
        "必要时用更多迭代换取深度与广度。",
    },
}


def _resolve_target_count(target_count: int | str | None) -> int:
    """解析目标采集量：参数优先，其次环境变量，最后默认值。

    Args:
        target_count: 显式目标量；``None`` 时读 ``PLANNER_TARGET_COUNT``。

    Returns:
        至少为 1 的整数目标量。
    """
    raw: int | str | None = target_count
    if raw is None:
        raw = os.getenv(ENV_TARGET_COUNT, "").strip() or DEFAULT_TARGET_COUNT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("invalid target_count %r; using %d", raw, DEFAULT_TARGET_COUNT)
        return DEFAULT_TARGET_COUNT
    return max(1, value)


def _tier_for(target_count: int) -> str:
    """按目标量选择策略档位。"""
    if target_count < 10:
        return "lite"
    if target_count < 20:
        return "standard"
    return "full"


def plan_strategy(target_count: int | str | None = None) -> dict:
    """根据目标采集量返回策略 dict。

    Args:
        target_count: 目标采集量；缺省读环境变量 ``PLANNER_TARGET_COUNT``
            （默认 10）。

    Returns:
        策略 dict，含 ``tier``、``target_count``、``per_source_limit``、
        ``relevance_threshold``、``max_iterations`` 与 ``rationale``。
    """
    target = _resolve_target_count(target_count)
    tier = _tier_for(target)
    strategy = {"tier": tier, "target_count": target, **STRATEGY_TIERS[tier]}
    logger.info(
        "plan strategy: tier=%s target=%d per_source_limit=%d "
        "relevance_threshold=%.2f max_iterations=%d",
        tier,
        target,
        strategy["per_source_limit"],
        strategy["relevance_threshold"],
        strategy["max_iterations"],
    )
    return strategy


def planner_node(state: KBState) -> dict:
    """Planner 节点：生成策略并写入 ``plan`` 字段。

    Args:
        state: 当前共享状态（本节点不修改其它字段）。

    Returns:
        部分状态更新 ``{"plan": dict}``。
    """
    strategy = plan_strategy()
    print(
        f"[planner_node] tier={strategy['tier']} target={strategy['target_count']} "
        f"per_source_limit={strategy['per_source_limit']} "
        f"relevance_threshold={strategy['relevance_threshold']} "
        f"max_iterations={strategy['max_iterations']}"
    )
    print(f"[planner_node] rationale: {strategy['rationale']}")
    return {"plan": strategy}


__all__ = ["plan_strategy", "planner_node", "STRATEGY_TIERS"]
