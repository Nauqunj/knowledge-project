#!/usr/bin/env python3
"""Minimal MCP server exposing the local knowledge base over stdio.

Implements JSON-RPC 2.0 over stdio and the MCP ``initialize``, ``tools/list``
and ``tools/call`` methods, backed by the JSON articles under
``knowledge/articles``. Uses only the Python standard library.

Tools:
    search_articles(keyword, limit=5): search titles and summaries.
    get_article(article_id): fetch one article by id.
    knowledge_stats(): totals, source distribution and top tags.

Diagnostics are written to stderr; stdout carries only JSON-RPC messages.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

SERVER_NAME = "knowledge-base"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_SEARCH_LIMIT = 5
MAX_SEARCH_LIMIT = 50

ROOT = Path(__file__).resolve().parent
ARTICLES_DIR = ROOT / "knowledge" / "articles"

logger = logging.getLogger("mcp-knowledge")


class ToolError(Exception):
    """Raised when a tool call cannot be satisfied."""


# --- knowledge access --------------------------------------------------------


def load_articles() -> list[dict]:
    """Load all article JSON files from the knowledge base.

    Returns:
        A list of article dictionaries; unreadable or malformed files are
        skipped with a warning.
    """
    articles: list[dict] = []
    if not ARTICLES_DIR.exists():
        logger.warning("articles directory not found: %s", ARTICLES_DIR)
        return articles

    for path in sorted(ARTICLES_DIR.glob("*.json")):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("skipping %s: %s", path.name, exc)
            continue
        if isinstance(data, dict) and data.get("id"):
            articles.append(data)
    return articles


def _as_int(value: object, default: int) -> int:
    """Best-effort integer coercion."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# --- tools -------------------------------------------------------------------


def search_articles(arguments: dict) -> dict:
    """Search articles by keyword across titles and summaries."""
    keyword = str(arguments.get("keyword", "")).strip()
    if not keyword:
        raise ToolError("keyword is required")

    limit = _as_int(arguments.get("limit", DEFAULT_SEARCH_LIMIT), DEFAULT_SEARCH_LIMIT)
    limit = max(1, min(limit, MAX_SEARCH_LIMIT))

    needle = keyword.lower()
    matches: list[dict] = []
    for article in load_articles():
        title = str(article.get("title", ""))
        summary = str(article.get("summary", ""))
        if needle in title.lower() or needle in summary.lower():
            matches.append(
                {
                    "id": article.get("id"),
                    "title": title,
                    "source": article.get("source"),
                    "source_url": article.get("source_url") or article.get("url"),
                    "summary": summary,
                    "score": article.get("score"),
                    "tags": article.get("tags", []),
                }
            )

    return {
        "keyword": keyword,
        "count": len(matches),
        "limit": limit,
        "results": matches[:limit],
    }


def get_article(arguments: dict) -> dict:
    """Return the full article for the given id."""
    article_id = str(arguments.get("article_id", "")).strip()
    if not article_id:
        raise ToolError("article_id is required")

    for article in load_articles():
        if str(article.get("id")) == article_id:
            return article
    raise ToolError(f"article not found: {article_id}")


def knowledge_stats(arguments: dict) -> dict:
    """Return totals, source distribution and top tags."""
    articles = load_articles()

    sources: dict[str, int] = {}
    tags: dict[str, int] = {}
    for article in articles:
        source = str(article.get("source", "unknown"))
        sources[source] = sources.get(source, 0) + 1
        article_tags = article.get("tags")
        if isinstance(article_tags, list):
            for tag in article_tags:
                if isinstance(tag, str) and tag:
                    tags[tag] = tags.get(tag, 0) + 1

    top_tags = sorted(tags.items(), key=lambda item: (-item[1], item[0]))[:10]
    return {
        "total_articles": len(articles),
        "sources": dict(sorted(sources.items())),
        "top_tags": [{"tag": tag, "count": count} for tag, count in top_tags],
    }


TOOLS: list[dict] = [
    {
        "name": "search_articles",
        "description": "按关键词搜索知识库文章的标题与摘要。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "搜索关键词"},
                "limit": {
                    "type": "integer",
                    "description": "返回条数上限，默认 5",
                    "default": DEFAULT_SEARCH_LIMIT,
                },
            },
            "required": ["keyword"],
        },
    },
    {
        "name": "get_article",
        "description": "按 ID 获取一篇文章的完整内容。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "article_id": {"type": "string", "description": "文章 ID"},
            },
            "required": ["article_id"],
        },
    },
    {
        "name": "knowledge_stats",
        "description": "返回知识库统计信息：文章总数、来源分布、热门标签。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

TOOL_HANDLERS = {
    "search_articles": search_articles,
    "get_article": get_article,
    "knowledge_stats": knowledge_stats,
}


def call_tool(name: str, arguments: dict) -> tuple[dict, bool]:
    """Dispatch a tool call.

    Args:
        name: Tool name.
        arguments: Tool arguments.

    Returns:
        A ``(payload, is_error)`` tuple.
    """
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown tool: {name}"}, True
    try:
        return handler(arguments), False
    except ToolError as exc:
        return {"error": str(exc)}, True


# --- JSON-RPC over stdio -----------------------------------------------------


def _send(message: dict) -> None:
    """Write a JSON-RPC message to stdout as a single line."""
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def configure_stdio() -> None:
    """Force UTF-8 on stdio so JSON-RPC stays valid on Windows pipes."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _result(request_id: object, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: object, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _initialize(params: dict) -> dict:
    return {
        "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _tools_list() -> dict:
    return {"tools": TOOLS}


def _tools_call(params: dict) -> dict:
    name = params.get("name", "")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        arguments = {}

    payload, is_error = call_tool(str(name), arguments)
    return {
        "content": [
            {"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}
        ],
        "isError": is_error,
    }


def handle_message(message: dict) -> dict | None:
    """Handle one JSON-RPC message.

    Args:
        message: Parsed JSON-RPC message.

    Returns:
        A response dict, or ``None`` for notifications (no reply expected).
    """
    method = message.get("method")
    request_id = message.get("id")
    params = message.get("params") or {}
    is_notification = "id" not in message

    if is_notification:
        if method == "notifications/initialized":
            logger.info("client initialised")
        else:
            logger.debug("ignoring notification: %s", method)
        return None

    if method == "initialize":
        return _result(request_id, _initialize(params))
    if method == "tools/list":
        return _result(request_id, _tools_list())
    if method == "tools/call":
        return _result(request_id, _tools_call(params))
    if method == "ping":
        return _result(request_id, {})
    return _error(request_id, -32601, f"Method not found: {method}")


def serve() -> int:
    """Run the stdio JSON-RPC loop until stdin closes.

    Returns:
        Process exit code.
    """
    logger.info("serving %s v%s over stdio", SERVER_NAME, SERVER_VERSION)
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _send(_error(None, -32700, "Parse error"))
            continue
        if not isinstance(message, dict):
            _send(_error(None, -32600, "Invalid Request"))
            continue

        try:
            response = handle_message(message)
        except Exception as exc:  # noqa: BLE001 - keep the server alive
            logger.exception("error handling message")
            if "id" in message:
                response = _error(message.get("id"), -32603, f"Internal error: {exc}")
            else:
                response = None

        if response is not None:
            _send(response)

    logger.info("stdin closed; shutting down")
    return 0


def main() -> int:
    """Configure logging and start the server."""
    configure_stdio()
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return serve()


if __name__ == "__main__":
    sys.exit(main())
