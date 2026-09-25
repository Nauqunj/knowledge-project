#!/usr/bin/env python3
"""每日知识速递推送入口。

流程：

1. 用 :func:`distribution.formatter.load_articles` 读取当天条目；
2. 过滤 ``relevance_score`` 不达标的低质量文章；
3. 用 :func:`distribution.formatter.build_digest` 构建三种格式；
4. 调 :func:`distribution.publisher.publish_daily_digest` 并发推送到所有渠道；
5. 打印成功 / 失败渠道数汇总。

用法：
    python daily_digest.py                 # 推 UTC 当天
    python daily_digest.py 2026-09-24      # 推指定日期
    python daily_digest.py 2026-09-24 --top 1  # 只推最相关的 1 篇
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from distribution.formatter import (  # noqa: E402
    DEFAULT_DIGEST_TOP_N,
    DEFAULT_KNOWLEDGE_DIR,
    build_digest,
    load_articles,
)
from distribution.publisher import (  # noqa: E402
    PublishResult,
    publish_daily_digest,
)

MIN_RELEVANCE_SCORE = 60.0


def _utc_today() -> str:
    """返回 UTC 当天的 ``YYYY-MM-DD``。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def normalized_score(article: dict[str, Any]) -> float:
    """把条目的相关度归一到 0-100。

    兼容三种量纲：``0-1``（乘 100）、``1-10``（乘 10）、``0-100``（原样）。
    优先读 ``relevance_score``，缺失时回退旧字段 ``score``。

    Args:
        article: 知识条目。

    Returns:
        0-100 的相关度；无法解析时为 ``0.0``。
    """
    raw = article.get("relevance_score")
    if raw is None:
        raw = article.get("score")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if value <= 1.0:
        value *= 100.0
    elif value <= 10.0:
        value *= 10.0
    return value


def filter_high_quality(
    articles: list[dict[str, Any]],
    min_score: float = MIN_RELEVANCE_SCORE,
) -> list[dict[str, Any]]:
    """过滤掉相关度低于阈值的文章。

    Args:
        articles: 待筛选的条目列表。
        min_score: 最低相关度（0-100），默认 60。

    Returns:
        相关度 ``>= min_score`` 的条目，按相关度降序。
    """
    kept = [
        article
        for article in articles
        if normalized_score(article) >= min_score
    ]
    return sorted(kept, key=normalized_score, reverse=True)


def _print_summary(results: list[PublishResult]) -> int:
    """打印推送结果汇总。

    Args:
        results: 各渠道的发布结果。

    Returns:
        退出码：全部成功 ``0``，存在失败 ``2``。
    """
    success = sum(1 for item in results if item.success)
    failed = len(results) - success
    print(f"[daily_digest] 推送完成：成功 {success} 个渠道，失败 {failed} 个渠道")
    for item in results:
        status = "OK" if item.success else "FAIL"
        print(
            f"  [{status}] {item.channel} "
            f"count={item.count} id={item.message_id} "
            f"error={item.error}"
        )
    return 0 if failed == 0 else 2


def parse_args(argv: list[str]) -> tuple[str | None, int]:
    """解析命令行参数。

    支持位置参数 ``YYYY-MM-DD`` 与 ``--top N``（或 ``--top=N``）。

    Args:
        argv: 参数列表（不含脚本名）。

    Returns:
        ``(date, top_n)``；未给日期时为 ``None``。

    Raises:
        ValueError: ``--top`` 的值不是正整数。
    """
    date: str | None = None
    top_n = DEFAULT_DIGEST_TOP_N
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--top" and index + 1 < len(argv):
            value = argv[index + 1]
            index += 2
        elif arg.startswith("--top="):
            value = arg.split("=", 1)[1]
            index += 1
        elif not arg.startswith("-") and date is None:
            date = arg
            index += 1
            continue
        else:
            index += 1
            continue
        try:
            top_n = int(value)
        except ValueError as exc:
            raise ValueError(f"--top 需要整数，收到 {value!r}") from exc
        if top_n <= 0:
            raise ValueError(f"--top 必须为正整数，收到 {top_n}")
    return date, top_n


async def main(argv: list[str] | None = None) -> int:
    """执行一次每日推送。

    Args:
        argv: 命令行参数；可含 ``YYYY-MM-DD`` 日期与 ``--top N``，
            日期默认 UTC 当天，``N`` 默认 10。

    Returns:
        退出码：全部成功 ``0``；无高质量文章或无可用渠道 ``1``；
        存在失败渠道 ``2``。
    """
    args = list(sys.argv[1:] if argv is None else argv)
    date, top_n = parse_args(args)
    digest_date = date or _utc_today()

    articles = load_articles(Path(DEFAULT_KNOWLEDGE_DIR), digest_date)
    high_quality = filter_high_quality(articles)
    print(
        f"[daily_digest] {digest_date}：共 {len(articles)} 条，"
        f"高质量（>= {MIN_RELEVANCE_SCORE:g}）{len(high_quality)} 条，"
        f"取 Top {top_n}"
    )

    if not high_quality:
        print(
            "[daily_digest] [WARN] 没有达标的高质量文章，跳过推送。"
        )
        return 1

    digest = build_digest(high_quality, digest_date, top_n=top_n)
    results = await publish_daily_digest(date=digest_date, digest=digest)

    if not results:
        print("[daily_digest] [WARN] 没有已配置的发布渠道，跳过推送。")
        return 1

    return _print_summary(results)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
