#!/usr/bin/env python3
"""HumanFlag 节点 —— 审核循环的异常终点（需人工介入）。

当审核循环达到 ``MAX_ITERATIONS`` 仍未通过时，说明问题可能不在「质量」而在
「数据」，继续自动重试没有意义。本节点把问题条目写入独立目录
``knowledge/pending_review/``，**不污染主知识库** ``knowledge/articles/``，
并返回 ``{"needs_human_review": True}`` 作为循环的出口信号。

Usage:
    # 由 graph.py 在「超限未通过」分支调用
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.state import KBState  # noqa: E402

logger = logging.getLogger("human_flag")

PENDING_DIR = _REPO_ROOT / "knowledge" / "pending_review"


def _utc_timestamp() -> str:
    """返回用于文件名的 UTC 时间戳 ``YYYY-MM-DD-HHMMSS``。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def human_flag_node(state: KBState) -> dict:
    """兜底节点：把未通过的条目写入 ``knowledge/pending_review/``。

    Args:
        state: 当前共享状态（读取 ``analyses``、``iteration``、
            ``review_feedback``）。

    Returns:
        部分状态更新 ``{"needs_human_review": True}``。
    """
    analyses = state.get("analyses", [])
    iteration = state.get("iteration", 0)
    feedback = state.get("review_feedback", "")

    print(f"[HumanFlag] 达到 {iteration} 次审核仍未通过，转人工复核")
    print(f"[HumanFlag] 最后反馈：{feedback[:200]}")

    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _utc_timestamp()
    filepath = PENDING_DIR / f"pending-{stamp}.json"
    payload = {
        "timestamp": stamp,
        "iterations_used": iteration,
        "last_feedback": feedback,
        "analyses": analyses,
    }
    filepath.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.warning(
        "human review pending: %d item(s) -> %s", len(analyses), filepath
    )
    print(f"[HumanFlag] 已保存到 {filepath}")

    return {"needs_human_review": True}


__all__ = ["PENDING_DIR", "human_flag_node"]
