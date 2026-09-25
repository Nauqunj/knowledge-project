#!/usr/bin/env python3
"""知识条目分发格式化 — 把 v3 Organizer 产出的 JSON 条目转成各渠道消息。

纯函数模块：只做「数据 → 文本 / 卡片」的结构转换，不发起任何网络请求。

输入条目字段（v3 LangGraph Organizer 产出）：
    id / title / source / url / collected_at / summary /
    tags / relevance_score / category / key_insight

对外接口：
    json_to_markdown(article)   -> Markdown 字符串
    json_to_telegram(article)   -> Telegram MarkdownV2 字符串
    json_to_feishu(article)     -> 飞书 interactive 卡片消息 dict
    generate_daily_digest(...)  -> 当日 Top N 的多渠道汇总
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCORE_GREEN = 0.8
SCORE_YELLOW = 0.6

SCORE_GREEN_EMOJI = "🟢"
SCORE_YELLOW_EMOJI = "🟡"
SCORE_RED_EMOJI = "🔴"

FEISHU_GREEN = "green"
FEISHU_YELLOW = "yellow"
FEISHU_RED = "red"

DEFAULT_KNOWLEDGE_DIR = "knowledge/articles"
DEFAULT_DIGEST_TOP_N = 10
EMPTY_DIGEST_TEMPLATE = "📭 {date} 暂无新增知识条目"

DIGEST_TITLE_TEMPLATE = "📰 {date} 技术知识速递（{count} 条）"

_TELEGRAM_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _as_text(value: Any) -> str:
    """把任意字段值规整为非空字符串。

    Args:
        value: 原始字段值，可能为 ``None``。

    Returns:
        去除首尾空白后的字符串；``None`` 转为空串。
    """
    if value is None:
        return ""
    return str(value).strip()


def _as_float(value: Any, default: float = 0.0) -> float:
    """把相关性评分安全地转成浮点数。

    Args:
        value: 原始评分，可能是数字或数字字符串。
        default: 无法解析时的回退值。

    Returns:
        解析出的浮点数；非法输入返回 ``default``。
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_tags(value: Any) -> list[str]:
    """把标签字段规整为字符串列表。

    Args:
        value: 标签字段，可能是列表或逗号分隔字符串。

    Returns:
        去空白、去空项后的标签列表。
    """
    if isinstance(value, str):
        parts = re.split(r"[,，]", value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    return [text for text in (_as_text(item) for item in parts) if text]


def _article_date(article: dict[str, Any]) -> str:
    """提取 ``collected_at`` 的日期部分（前 10 位）。

    Args:
        article: 知识条目。

    Returns:
        ``YYYY-MM-DD``；缺失时返回空串。
    """
    return _as_text(article.get("collected_at"))[:10]


def _score_emoji(score: float) -> str:
    """按评分返回红黄绿信号灯。

    Args:
        score: 0-1 的相关性评分。

    Returns:
        ``>= 0.8`` 返回 🟢，``>= 0.6`` 返回 🟡，否则返回 🔴。
    """
    if score >= SCORE_GREEN:
        return SCORE_GREEN_EMOJI
    if score >= SCORE_YELLOW:
        return SCORE_YELLOW_EMOJI
    return SCORE_RED_EMOJI


def _score_color(score: float) -> str:
    """按评分返回飞书卡片头部的颜色模板。

    Args:
        score: 0-1 的相关性评分。

    Returns:
        ``green`` / ``yellow`` / ``red`` 之一。
    """
    if score >= SCORE_GREEN:
        return FEISHU_GREEN
    if score >= SCORE_YELLOW:
        return FEISHU_YELLOW
    return FEISHU_RED


def _escape_markdown_v2(text: str) -> str:
    """转义 Telegram MarkdownV2 保留字符。

    Args:
        text: 待转义的原始文本。

    Returns:
        对 ``_*[]()~`>#+-=|{}.!`` 及反斜杠做了转义的文本。
    """
    return _TELEGRAM_ESCAPE_RE.sub(r"\\\1", text)


def _escape_url(url: str) -> str:
    """转义 Telegram MarkdownV2 链接目标中的特殊字符。

    Args:
        url: 原始链接。

    Returns:
        仅对 ``\\`` 与 ``)`` 转义后的链接。
    """
    return url.replace("\\", "\\\\").replace(")", "\\)")


def json_to_markdown(article: dict[str, Any]) -> str:
    """把单篇知识条目渲染为 Markdown。

    Args:
        article: 知识条目 dict，字段见模块 docstring。

    Returns:
        Markdown 字符串，包含标题链接、来源、日期、相关性、分类、
        摘要、标签与关键洞察。
    """
    title = _as_text(article.get("title")) or "(无标题)"
    url = _as_text(article.get("url")) or _as_text(article.get("source_url"))
    source = _as_text(article.get("source")) or "未知"
    date = _article_date(article) or "未知"
    score = _as_float(article.get("relevance_score"))
    emoji = _score_emoji(score)
    summary = _as_text(article.get("summary"))
    category = _as_text(article.get("category"))
    tags = _as_tags(article.get("tags"))
    insight = _as_text(article.get("key_insight"))

    lines: list[str] = [f"## [{title}]({url})" if url else f"## {title}", ""]
    meta = (
        f"- **来源**：{source}  |  **日期**：{date}"
        f"  |  **相关性**：{emoji} {score:.2f}"
    )
    lines.append(meta)
    if category:
        lines.append(f"- **分类**：{category}")
    if summary:
        lines += ["", f"**摘要**：{summary}"]
    if tags:
        tags_text = "  ".join(f"`{tag}`" for tag in tags)
        lines += ["", f"**标签**：{tags_text}"]
    if insight:
        lines += ["", f"> 💡 {insight}"]

    return "\n".join(lines).strip()


def json_to_telegram(article: dict[str, Any]) -> str:
    """把单篇知识条目渲染为 Telegram MarkdownV2 消息。

    Args:
        article: 知识条目 dict，字段见模块 docstring。

    Returns:
        MarkdownV2 字符串，标题为链接（无链接时加粗），其余动态文本
        均已转义；标签中的空格替换为下划线。
    """
    title = _as_text(article.get("title")) or "(无标题)"
    url = _as_text(article.get("url")) or _as_text(article.get("source_url"))
    source = _as_text(article.get("source")) or "未知"
    date = _article_date(article) or "未知"
    score = _as_float(article.get("relevance_score"))
    emoji = _score_emoji(score)
    summary = _as_text(article.get("summary"))
    category = _as_text(article.get("category"))
    tags = _as_tags(article.get("tags"))
    insight = _as_text(article.get("key_insight"))

    if url:
        heading = f"[{_escape_markdown_v2(title)}]({_escape_url(url)})"
    else:
        heading = f"*{_escape_markdown_v2(title)}*"

    lines: list[str] = [heading]
    lines.append(
        _escape_markdown_v2(
            f"来源：{source}  |  日期：{date}  |  相关性：{score:.2f} {emoji}"
        )
    )
    if category:
        lines.append(_escape_markdown_v2(f"分类：{category}"))
    if summary:
        lines.append(_escape_markdown_v2(f"摘要：{summary}"))
    if tags:
        tags_text = " ".join(tag.replace(" ", "_") for tag in tags)
        lines.append(_escape_markdown_v2(f"标签：{tags_text}"))
    if insight:
        lines.append(_escape_markdown_v2(f"💡 {insight}"))

    return "\n".join(lines)


def json_to_feishu(article: dict[str, Any]) -> dict[str, Any]:
    """把单篇知识条目构建为飞书 interactive 卡片消息。

    Args:
        article: 知识条目 dict，字段见模块 docstring。

    Returns:
        可直接发送的消息 dict，形如
        ``{"msg_type": "interactive", "card": {...}}``；卡片头部颜色
        按评分取 ``green`` / ``yellow`` / ``red``。
    """
    title = _as_text(article.get("title")) or "(无标题)"
    url = _as_text(article.get("url")) or _as_text(article.get("source_url"))
    source = _as_text(article.get("source")) or "未知"
    date = _article_date(article) or "未知"
    score = _as_float(article.get("relevance_score"))
    emoji = _score_emoji(score)
    summary = _as_text(article.get("summary"))
    category = _as_text(article.get("category"))
    tags = _as_tags(article.get("tags"))
    insight = _as_text(article.get("key_insight"))

    elements: list[dict[str, Any]] = []
    if summary:
        elements.append(
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"**摘要**：{summary}"},
            }
        )

    meta_lines = [
        f"**来源**：{source}  |  **日期**：{date}"
        f"  |  **相关性**：{emoji} {score:.2f}"
    ]
    if category:
        meta_lines.append(f"**分类**：{category}")
    if tags:
        tags_text = " ".join(f"`{tag}`" for tag in tags)
        meta_lines.append(f"**标签**：{tags_text}")
    if insight:
        meta_lines.append(f"**洞察**：{insight}")
    elements.append(
        {
            "tag": "div",
            "text": {"tag": "lark_md", "content": "\n".join(meta_lines)},
        }
    )

    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看原文"},
                        "url": url,
                        "type": "primary",
                    }
                ],
            }
        )

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _score_color(score),
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }
    return {"msg_type": "interactive", "card": card}


def load_articles(
    knowledge_dir: Path | str, date: str
) -> list[dict[str, Any]]:
    """读取指定日期的知识条目文件。

    Args:
        knowledge_dir: 知识条目目录。
        date: ``YYYY-MM-DD`` 日期。

    Returns:
        解析成功的条目列表；损坏或非对象文件会被跳过。
    """
    directory = Path(knowledge_dir)
    articles: list[dict[str, Any]] = []
    for path in sorted(directory.glob(f"{date}-*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            articles.append(payload)
        elif isinstance(payload, list):
            articles.extend(item for item in payload if isinstance(item, dict))
    return articles


def build_digest(
    articles: list[dict[str, Any]],
    date: str,
    top_n: int = DEFAULT_DIGEST_TOP_N,
) -> dict[str, Any] | str:
    """把一组条目构建为多渠道速递。

    调用方负责筛选（如过滤低相关度），本函数只做排序、截断与格式化。

    Args:
        articles: 已加载的知识条目列表。
        date: ``YYYY-MM-DD`` 日期，用于标题。
        top_n: 按 ``relevance_score`` 降序保留的条数上限。

    Returns:
        正常时返回 ``{"markdown": str, "telegram": str, "feishu": list[dict]}``；
        条目为空时返回提示字符串 ``"📭 {date} 暂无新增知识条目"``。
    """
    if not articles:
        return EMPTY_DIGEST_TEMPLATE.format(date=date)

    ranked = sorted(
        articles,
        key=lambda item: _as_float(item.get("relevance_score")),
        reverse=True,
    )
    selected = ranked[: max(0, top_n)]
    if not selected:
        return EMPTY_DIGEST_TEMPLATE.format(date=date)

    title = DIGEST_TITLE_TEMPLATE.format(date=date, count=len(selected))

    markdown = f"# {title}\n\n" + "\n\n---\n\n".join(
        json_to_markdown(article) for article in selected
    )
    telegram = f"*{_escape_markdown_v2(title)}*\n\n" + "\n\n".join(
        json_to_telegram(article) for article in selected
    )
    feishu = [json_to_feishu(article) for article in selected]

    return {"markdown": markdown, "telegram": telegram, "feishu": feishu}


def generate_daily_digest(
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
    date: str | None = None,
    top_n: int = DEFAULT_DIGEST_TOP_N,
) -> dict[str, Any] | str:
    """汇总某日知识条目，生成多渠道每日速递。

    Args:
        knowledge_dir: 知识条目目录，默认 ``knowledge/articles``。
        date: ``YYYY-MM-DD``；默认取 UTC 当天。
        top_n: 按 ``relevance_score`` 降序保留的条数上限。

    Returns:
        正常时返回 ``{"markdown": str, "telegram": str, "feishu": list[dict]}``，
        其中 ``feishu`` 为每篇一条的 interactive 卡片列表；当日无文章时
        返回提示字符串 ``"📭 {date} 暂无新增知识条目"``。
    """
    digest_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    articles = load_articles(knowledge_dir, digest_date)
    return build_digest(articles, digest_date, top_n)


__all__ = [
    "build_digest",
    "generate_daily_digest",
    "json_to_feishu",
    "json_to_markdown",
    "json_to_telegram",
    "load_articles",
]
