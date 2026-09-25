#!/usr/bin/env python3
"""Reviser 节点：根据审核反馈改写 ``state["analyses"]``。

流程：读取 ``analyses`` 与 ``review_feedback``，把反馈注入修改 prompt，
调用 LLM 返回改进后的列表（按位置合并回原条目）。``analyses`` 或
``feedback`` 为空时直接跳过（返回 ``{}``，不产生状态更新）。

为控制 token，仅重写前 ``REVISE_LIMIT`` 条（与 reviewer 的审核范围一致），
其余条目原样保留。``temperature=0.4`` 允许创造性的改写。

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
from workflows.state import KBState  # noqa: E402

logger = logging.getLogger("reviser")

REVISE_LIMIT = 5
REVISE_TEMPERATURE = 0.4

# 这些字段标识条目身份，合并时以原值为准，避免模型改写。
IDENTITY_FIELDS = ("id", "source", "source_id", "source_url")

REVISE_SYSTEM_PROMPT = (
    "你是知识条目编辑。请根据审核反馈改进给定的分析结果，"
    "只修改反馈指出的问题，其余字段保持原样，保持条目数量与顺序不变。"
    "只输出一个 JSON 对象，不要输出任何多余文字或 Markdown 代码块，格式："
    '{"analyses": [ ...改进后的条目... ]}'
)


def _revise_batch(
    subset: list[dict],
    feedback: str,
) -> tuple[list[dict] | None, Any | None]:
    """对一批条目调用 LLM 做定向改写。

    Args:
        subset: 待改写的条目（通常是前 ``REVISE_LIMIT`` 条）。
        feedback: 审核反馈。

    Returns:
        ``(improved, usage)``：改进后的条目列表（已按位置合并回原条目）与
        token 用量；调用失败或无可用结果时返回 ``(None, None)``。
    """
    prompt = (
        f"审核反馈：\n{feedback}\n\n"
        f"请根据反馈改进以下 {len(subset)} 条分析结果，"
        f"只修改反馈指出的问题，保持顺序与数量不变：\n"
        f"{json.dumps(subset, ensure_ascii=False, indent=2)}"
    )

    try:
        data, usage = chat_json(
            prompt,
            system=REVISE_SYSTEM_PROMPT,
            temperature=REVISE_TEMPERATURE,
            node_name="revise",
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("revise failed, keeping originals: %s", exc)
        return None, None

    items = data.get("analyses")
    if not isinstance(items, list) or not items:
        logger.warning("revise returned no usable analyses, keeping originals")
        return None, None

    merged: list[dict] = []
    for index, original in enumerate(subset):
        candidate = items[index] if index < len(items) else None
        item = {**original, **(candidate if isinstance(candidate, dict) else {})}
        for field in IDENTITY_FIELDS:
            if original.get(field) is not None:
                item[field] = original[field]
        merged.append(item)
    return merged, usage


def revise_node(state: KBState) -> dict:
    """改写节点：根据 ``review_feedback`` 改进 ``analyses``。

    Args:
        state: 当前共享状态（读取 ``analyses``、``review_feedback``、
            ``cost_tracker``）。

    Returns:
        ``{"analyses": list[dict], "cost_tracker": dict}``；
        无 analyses 或无 feedback 时返回 ``{}``（跳过，不改状态）。
    """
    analyses = state.get("analyses", [])
    feedback = (state.get("review_feedback") or "").strip()
    print(
        f"[revise_node] analyses={len(analyses)}, "
        f"feedback={'有' if feedback else '无'}"
    )

    if not analyses or not feedback:
        print("[revise_node] 无可修正内容，跳过")
        return {}

    tracker = copy.deepcopy(state.get("cost_tracker") or {})
    subset = analyses[:REVISE_LIMIT]

    improved_subset, usage = _revise_batch(subset, feedback)
    if improved_subset is None:
        print("[revise_node] LLM 未返回可用结果，保留原 analyses")
        return {}

    if usage is not None:
        accumulate_usage(tracker, usage)

    improved = improved_subset + list(analyses[REVISE_LIMIT:])
    print(
        f"[revise_node] 已修正前 {len(improved_subset)} 条，"
        f"其余 {len(analyses) - len(improved_subset)} 条保持不变"
    )
    return {"analyses": improved, "cost_tracker": tracker}


__all__ = ["REVISE_LIMIT", "revise_node"]
