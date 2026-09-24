#!/usr/bin/env python3
"""Supervisor pattern: a Worker drafts, a Supervisor reviews, loop until pass.

Flow:

    1. The Worker agent receives the task and returns a JSON analysis report.
    2. The Supervisor agent scores that report on three dimensions
       (accuracy / depth / format, each 1-10) and returns a verdict:
           {"passed": bool, "score": int, "feedback": str}
    3. Review loop:
           - passed (score >= 7) -> return the result
           - failed             -> redo, feeding the feedback back to the Worker
           - exceeded max_retries -> force-return with a warning

Usage:
    python patterns/supervisor.py "分析 MCP 协议的优缺点"
    python patterns/supervisor.py            # runs the built-in demo task

Environment:
    LLM_PROVIDER / *_API_KEY: Passed through to :mod:`workflows.model_client`.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.model_client import chat  # noqa: E402

logger = logging.getLogger("supervisor")

PASS_THRESHOLD = 7
SCORE_MIN = 1
SCORE_MAX = 10

WORKER_SYSTEM_PROMPT = (
    "你是一名资深技术分析员。请针对用户任务撰写结构化的分析报告，"
    "只输出一个 JSON 对象，不要输出任何多余文字或 Markdown 代码块。"
    "字段：title（标题）、summary（不超过 80 字的摘要）、"
    "key_points（3-5 条关键要点的字符串数组）、"
    "risks（可选，风险或局限的字符串数组）、"
    "conclusion（结论）。"
)

SUPERVISOR_SYSTEM_PROMPT = (
    "你是严格的质量审核员。请根据任务要求审核 Worker 的分析报告，"
    "从准确性(accuracy)、深度(depth)、格式(format) 三个维度各打 1-10 分，"
    "再给出是否通过(passed)、综合分(score，取三个维度的整数均值)与改进反馈(feedback)。"
    "只输出一个 JSON 对象，不要输出任何多余文字或 Markdown 代码块，字段："
    '{"passed": bool, "score": int, "accuracy": int, "depth": int, '
    '"format": int, "feedback": "..."}'
)

_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


# --- helpers -----------------------------------------------------------------


def _extract_json(text: str) -> dict | None:
    """Extract the first JSON object from a model response.

    Args:
        text: Raw model output, possibly wrapped in Markdown fences.

    Returns:
        The parsed object, or ``None`` when parsing fails.
    """
    cleaned = _FENCE_RE.sub("", text).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _as_score(value: Any, default: int = 0) -> int:
    """Clamp a model-provided score into the 1-10 integer range.

    Args:
        value: Raw value from the model.
        default: Value returned when ``value`` is not numeric.

    Returns:
        An integer in ``[1, 10]``, or ``default`` when unusable.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(SCORE_MIN, min(SCORE_MAX, int(round(value))))


def _normalize_verdict(verdict: dict) -> dict:
    """Normalise a raw supervisor verdict.

    The pass flag is derived from ``score >= PASS_THRESHOLD`` so the loop is
    deterministic even if the model disagrees with itself.

    Args:
        verdict: Raw JSON object from the supervisor.

    Returns:
        A dict with ``passed``, ``score``, ``accuracy``, ``depth``, ``format``
        and ``feedback`` keys.
    """
    accuracy = _as_score(verdict.get("accuracy"))
    depth = _as_score(verdict.get("depth"))
    format_score = _as_score(verdict.get("format"))

    score = verdict.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        dimensions = [value for value in (accuracy, depth, format_score) if value > 0]
        score = round(sum(dimensions) / len(dimensions)) if dimensions else 0
    score = max(0, min(SCORE_MAX, int(round(score))))

    passed = score >= PASS_THRESHOLD
    if isinstance(verdict.get("passed"), bool) and verdict["passed"] != passed:
        logger.warning(
            "supervisor said passed=%s but score=%d; using score",
            verdict["passed"],
            score,
        )

    return {
        "passed": passed,
        "score": score,
        "accuracy": accuracy,
        "depth": depth,
        "format": format_score,
        "feedback": str(verdict.get("feedback", "")).strip(),
    }


# --- agents ------------------------------------------------------------------


def run_worker(task: str, feedback: str | None = None) -> dict:
    """Have the Worker draft (or redraft) a JSON analysis report.

    Args:
        task: The analysis task.
        feedback: Optional supervisor feedback from a previous round.

    Returns:
        The Worker's report as a dict; raw text is wrapped under ``content``
        when the model did not return valid JSON.
    """
    prompt = f"任务：{task}"
    if feedback:
        prompt += f"\n\n上一版报告未通过审核，请根据以下反馈重做：\n{feedback}"

    text, usage = chat(prompt, system=WORKER_SYSTEM_PROMPT)
    logger.info("worker replied using %d token(s)", usage.total_tokens)

    report = _extract_json(text)
    if report is None:
        logger.warning("worker output is not valid JSON; wrapping raw text")
        report = {"content": text.strip()}
    return report


def run_supervisor(task: str, report: dict[str, Any]) -> dict:
    """Have the Supervisor review a report and return a normalised verdict.

    Args:
        task: The original analysis task.
        report: The Worker's JSON report.

    Returns:
        A normalised verdict with ``passed``, ``score`` and ``feedback``.
    """
    prompt = (
        f"任务：{task}\n\n"
        f"待审核的分析报告（JSON）：\n"
        f"{json.dumps(report, ensure_ascii=False, indent=2)}"
    )
    text, usage = chat(prompt, system=SUPERVISOR_SYSTEM_PROMPT, temperature=0.0)
    logger.info("supervisor replied using %d token(s)", usage.total_tokens)

    verdict = _extract_json(text)
    if verdict is None:
        logger.warning("supervisor output is not valid JSON; treating as failed")
        verdict = {}
    return _normalize_verdict(verdict)


# --- orchestration -----------------------------------------------------------


def supervisor(task: str, max_retries: int = 3) -> dict:
    """Run the Worker/Supervisor review loop.

    Args:
        task: The analysis task.
        max_retries: Maximum number of review rounds (at least 1).

    Returns:
        A result dict with ``output``, ``attempts``, ``final_score``, ``passed``
        and ``feedback``; ``warning`` is added when the loop exhausts its
        retries without passing.
    """
    if not task or not task.strip():
        raise ValueError("task must be a non-empty string")

    rounds = max(1, int(max_retries))
    task = task.strip()

    report: dict[str, Any] = {}
    verdict: dict = {"passed": False, "score": 0, "feedback": ""}
    feedback: str | None = None

    for attempt in range(1, rounds + 1):
        logger.info("round %d/%d: worker drafting", attempt, rounds)
        report = run_worker(task, feedback)

        logger.info("round %d/%d: supervisor reviewing", attempt, rounds)
        verdict = run_supervisor(task, report)

        if verdict["passed"]:
            logger.info("round %d passed with score %d", attempt, verdict["score"])
            return {
                "output": report,
                "attempts": attempt,
                "final_score": verdict["score"],
                "passed": True,
                "feedback": verdict["feedback"],
            }

        feedback = verdict["feedback"]
        logger.warning(
            "round %d rejected (score=%d): %s",
            attempt,
            verdict["score"],
            feedback or "（无反馈）",
        )

    warning = (
        f"报告在 {rounds} 轮内未通过审核（最高 {verdict['score']} 分），已强制返回。"
    )
    logger.warning(warning)
    return {
        "output": report,
        "attempts": rounds,
        "final_score": verdict["score"],
        "passed": False,
        "feedback": verdict["feedback"],
        "warning": warning,
    }


DEFAULT_TASK = "分析 MCP（Model Context Protocol）在企业落地中的价值与风险"


def main(argv: list[str] | None = None) -> int:
    """Run the supervisor on CLI args or the built-in demo task.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code (always ``0``).
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args = sys.argv[1:] if argv is None else argv
    task = " ".join(args).strip() or DEFAULT_TASK

    print(f">>> {task}")
    result = supervisor(task)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
