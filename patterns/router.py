#!/usr/bin/env python3
"""Router pattern: classify a query, then dispatch it to a handler.

Intent classification runs in two layers:

    1. Keyword fast match -- zero cost, never calls the LLM.
    2. LLM classification -- only used when the keywords are inconclusive.

Supported intents and handlers:

    github_search   -> :func:`handle_github_search` (GitHub Search API)
    knowledge_query -> :func:`handle_knowledge_query` (local knowledge index)
    general_chat    -> :func:`handle_general_chat` (LLM answer)

Usage:
    python patterns/router.py "找几个热门的 RAG 开源项目"
    python patterns/router.py            # runs the built-in demo queries

Environment:
    GITHUB_TOKEN: Optional; raises the GitHub Search API rate limit.
    LLM_PROVIDER / *_API_KEY: Passed through to :mod:`workflows.model_client`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.model_client import chat, chat_json  # noqa: E402

logger = logging.getLogger("router")

INDEX_FILE = _REPO_ROOT / "knowledge" / "articles" / "index.json"

INTENT_GITHUB_SEARCH = "github_search"
INTENT_KNOWLEDGE_QUERY = "knowledge_query"
INTENT_GENERAL_CHAT = "general_chat"
INTENTS: tuple[str, ...] = (
    INTENT_GITHUB_SEARCH,
    INTENT_KNOWLEDGE_QUERY,
    INTENT_GENERAL_CHAT,
)

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_TIMEOUT = 30.0
GITHUB_MAX_RESULTS = 5

KNOWLEDGE_MAX_RESULTS = 5

# Layer 1: keyword rules. A query is matched against these substrings; the
# intent with the most hits wins, and a tie falls through to the LLM layer.
KEYWORD_RULES: dict[str, tuple[str, ...]] = {
    INTENT_GITHUB_SEARCH: (
        "github",
        "仓库",
        "开源项目",
        "开源",
        "repo",
        "repository",
        "trending",
        "star",
    ),
    INTENT_KNOWLEDGE_QUERY: (
        "知识库",
        "已收录",
        "收录",
        "索引",
        "文章",
        "之前",
        "knowledge",
        "index",
    ),
}

CLASSIFY_SYSTEM_PROMPT = "你是意图分类器，只负责判断用户问题的意图。"
CLASSIFY_TEMPLATE = (
    "把下面的用户问题分类为三种意图之一：\n"
    "- github_search：想查找 GitHub 仓库或开源项目\n"
    "- knowledge_query：想查询本地知识库中已经收录的文章\n"
    "- general_chat：其他通用对话或知识问答\n\n"
    '只输出 JSON，例如 {{"intent": "general_chat", "reason": "简短理由"}}\n\n'
    "用户问题：{query}"
)


# --- layer 1 + 2: intent classification --------------------------------------


def classify_by_keywords(query: str) -> str | None:
    """Classify a query using keyword rules only (no LLM call).

    Args:
        query: The user query.

    Returns:
        The matching intent, or ``None`` when the keywords are inconclusive
        (no hit at all, or a tie between intents).
    """
    lowered = query.lower()
    scores = {
        intent: sum(1 for keyword in keywords if keyword in lowered)
        for intent, keywords in KEYWORD_RULES.items()
    }
    scores = {intent: score for intent, score in scores.items() if score > 0}
    if not scores:
        return None

    best = max(scores.values())
    winners = [intent for intent, score in scores.items() if score == best]
    return winners[0] if len(winners) == 1 else None


def classify_by_llm(query: str) -> str:
    """Classify a query with the LLM as a fallback.

    Args:
        query: The user query.

    Returns:
        One of :data:`INTENTS`; defaults to ``general_chat`` on any failure so
        that routing never breaks.
    """
    try:
        result, _ = chat_json(
            CLASSIFY_TEMPLATE.format(query=query),
            system=CLASSIFY_SYSTEM_PROMPT,
            temperature=0.0,
        )
    except (RuntimeError, ValueError) as exc:
        logger.warning("LLM intent classification failed: %s", exc)
        return INTENT_GENERAL_CHAT

    intent = str(result.get("intent", "")).strip().lower()
    if intent not in INTENTS:
        logger.warning("LLM returned unknown intent %r; falling back", intent)
        return INTENT_GENERAL_CHAT
    return intent


def classify(query: str) -> str:
    """Classify a query using the two-layer strategy.

    Args:
        query: The user query.

    Returns:
        The detected intent, one of :data:`INTENTS`.
    """
    intent = classify_by_keywords(query)
    if intent is not None:
        logger.info("classified %r as %s (keyword)", query, intent)
        return intent

    intent = classify_by_llm(query)
    logger.info("classified %r as %s (llm)", query, intent)
    return intent


# --- handlers ----------------------------------------------------------------


def handle_github_search(query: str) -> str:
    """Search GitHub repositories for the query.

    The ``q`` parameter is URL-encoded with :func:`urllib.parse.urlencode`.

    Args:
        query: The user query, used as the GitHub search term.

    Returns:
        A formatted, human-readable result string.
    """
    params = urllib.parse.urlencode(
        {
            "q": query,
            "sort": "stars",
            "order": "desc",
            "per_page": GITHUB_MAX_RESULTS,
        }
    )
    url = f"{GITHUB_SEARCH_URL}?{params}"

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "knowledge-router",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"

    request = urllib.request.Request(url, headers=headers)
    logger.info("github_search: GET %s", url)
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.error("github_search HTTP error: %s", exc)
        return f"GitHub 搜索失败（HTTP {exc.code}）：{exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("github_search network error: %s", exc)
        return f"GitHub 搜索失败（网络错误）：{exc}"
    except json.JSONDecodeError as exc:
        logger.error("github_search invalid JSON: %s", exc)
        return "GitHub 搜索失败：返回内容不是合法 JSON。"

    items = payload.get("items", [])
    if not items:
        return f"GitHub 上没有找到与「{query}」相关的仓库。"

    lines = [f"GitHub 搜索「{query}」的前 {len(items)} 个结果："]
    for position, repo in enumerate(items, start=1):
        stars = repo.get("stargazers_count", 0)
        description = repo.get("description") or "（无描述）"
        lines.append(
            f"{position}. {repo.get('full_name', '')}  ⭐{stars}\n"
            f"   {repo.get('html_url', '')}\n"
            f"   {description}"
        )
    return "\n".join(lines)


def _tokenize(text: str) -> list[str]:
    """Split text into lowercase tokens of at least two characters.

    Args:
        text: The text to split.

    Returns:
        A de-duplicated list of lowercase tokens.
    """
    parts = re.split(r"[\s,，、。？?！!；;：:（）()\[\]\"']+", text.lower())
    seen: list[str] = []
    for part in parts:
        if len(part) >= 2 and part not in seen:
            seen.append(part)
    return seen


def handle_knowledge_query(query: str) -> str:
    """Search the local knowledge index for matching articles.

    Args:
        query: The user query.

    Returns:
        A formatted list of the best matching articles, or a hint when the
        index is missing or nothing matches.
    """
    if not INDEX_FILE.exists():
        return f"本地知识库索引不存在：{INDEX_FILE}"

    try:
        index = json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("cannot read knowledge index: %s", exc)
        return f"读取知识库索引失败：{exc}"

    entries = index.get("entries", [])
    tokens = _tokenize(query)
    if not tokens:
        return "请输入更具体的关键词。"

    scored: list[tuple[int, dict]] = []
    for entry in entries:
        haystack = " ".join(
            [
                str(entry.get("title", "")),
                " ".join(str(tag) for tag in entry.get("tags", [])),
            ]
        ).lower()
        score = sum(1 for token in tokens if token in haystack)
        if score > 0:
            scored.append((score, entry))

    if not scored:
        return f"知识库中没有与「{query}」匹配的文章。"

    scored.sort(key=lambda pair: (pair[0], pair[1].get("score") or 0), reverse=True)
    top = scored[:KNOWLEDGE_MAX_RESULTS]

    lines = [f"知识库中与「{query}」最相关的 {len(top)} 篇文章："]
    for position, (_, entry) in enumerate(top, start=1):
        tags = "、".join(str(tag) for tag in entry.get("tags", []))
        lines.append(
            f"{position}. {entry.get('title', '')}（score={entry.get('score')}）\n"
            f"   {entry.get('source_url', '')}\n"
            f"   tags: {tags}\n"
            f"   文件: knowledge/articles/{entry.get('file', '')}"
        )
    return "\n".join(lines)


def handle_general_chat(query: str) -> str:
    """Answer a general query directly with the LLM.

    Args:
        query: The user query.

    Returns:
        The assistant reply text.
    """
    text, usage = chat(query)
    logger.info("general_chat answered using %d token(s)", usage.total_tokens)
    return text


HANDLERS: dict[str, Callable[[str], str]] = {
    INTENT_GITHUB_SEARCH: handle_github_search,
    INTENT_KNOWLEDGE_QUERY: handle_knowledge_query,
    INTENT_GENERAL_CHAT: handle_general_chat,
}


# --- entry point -------------------------------------------------------------


def route(query: str) -> str:
    """Classify a query and dispatch it to the matching handler.

    Args:
        query: The user query.

    Returns:
        The handler's response, always a string.
    """
    if not query or not query.strip():
        return "请输入有效的问题。"

    query = query.strip()
    intent = classify(query)
    handler = HANDLERS.get(intent, handle_general_chat)
    return handler(query)


DEMO_QUERIES: tuple[str, ...] = (
    "找几个热门的 RAG 开源仓库",
    "知识库里收录过关于 MCP 的文章吗",
    "用一句话解释什么是向量数据库",
)


def main(argv: list[str] | None = None) -> int:
    """Run the router against CLI args or the built-in demo queries.

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
    queries = args or list(DEMO_QUERIES)

    for query in queries:
        print(f"\n>>> {query}")
        print(route(query))
    return 0


if __name__ == "__main__":
    sys.exit(main())
