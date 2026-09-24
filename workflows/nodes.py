#!/usr/bin/env python3
"""LangGraph 工作流的 5 个节点函数。

每个节点都是「纯函数」式接口：接收 :class:`~workflows.state.KBState`，只读取，
返回一个 dict 作为**部分状态更新**（由 LangGraph 合并回全局状态），不直接修改入参。

节点列表：
    collect_node  采集 GitHub AI 相关仓库（GitHub Search API）
    analyze_node  用 LLM 为每条数据生成中文摘要、标签、评分（0-1）
    organize_node 过滤低分(< 0.6)、按 URL 去重；有审核反馈时用 LLM 定向修正
    review_node   LLM 四维度评分（摘要质量/标签准确/分类合理/一致性）并推进 iteration
    save_node     把 articles 写入 knowledge/articles/ 并更新 index.json

依赖：
    workflows.model_client.chat / chat_json / accumulate_usage
    workflows.state.KBState / MAX_ITERATIONS
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.model_client import (  # noqa: E402
    accumulate_usage,
    chat_json,
)
from workflows.state import KBState, MAX_ITERATIONS  # noqa: E402

logger = logging.getLogger("nodes")

ARTICLES_DIR = _REPO_ROOT / "knowledge" / "articles"
INDEX_FILE = ARTICLES_DIR / "index.json"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_QUERY = 'AI OR LLM OR agent OR "large language model" OR RAG OR MCP'
GITHUB_TIMEOUT = 30.0
COLLECT_LIMIT = int(os.getenv("COLLECT_LIMIT", "20"))

# 分析评分为 0-1；低于该值的条目不进入 articles。
MIN_SCORE = 0.6
# 审核 overall_score 达到该值判定通过。
REVIEW_PASS_THRESHOLD = 0.8

ANALYZE_SYSTEM_PROMPT = (
    "你是技术情报分析助手。只输出一个 JSON 对象，不要任何多余文字或 Markdown。"
    '字段：summary（不超过 50 字的中文摘要）、score（0-1 的小数质量分）、'
    "score_reason（简明理由）、tags（3-5 个英文小写连字符标签）、"
    "highlights（2-3 个基于事实的技术亮点）。"
)

REVISE_SYSTEM_PROMPT = (
    "你是知识条目编辑。根据审核反馈对给定条目做**定向修改**，"
    "只改动反馈指出的问题，其余字段保持原样，输出修改后的完整 JSON 对象，"
    "不要输出任何多余文字或 Markdown。"
)

REVIEW_SYSTEM_PROMPT = (
    "你是知识库质量审核员。请对给定条目从四个维度打分（每个维度 0-1 小数）："
    "summary_quality（摘要质量）、tag_accuracy（标签准确）、"
    "classification（分类合理）、consistency（一致性）。"
    "只输出一个 JSON 对象："
    '{"passed": bool, "overall_score": 0-1 小数, "feedback": "改进意见", '
    '"dimensions": {"summary_quality": 0-1, "tag_accuracy": 0-1, '
    '"classification": 0-1, "consistency": 0-1}}'
)

_FENCE_RE = re.compile(r"```(?:json)?")


# --- shared helpers ----------------------------------------------------------


def _utc_now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today_iso() -> str:
    """返回今天的 ``YYYY-MM-DD``（UTC）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _today_compact() -> str:
    """返回今天的 ``YYYYMMDD``（UTC）。"""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _slugify(text: str, max_length: int = 50) -> str:
    """把标题转成 URL 友好的 slug。"""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-") or "untitled"


def _tracker(state: KBState) -> dict:
    """深拷贝状态里的 cost_tracker，避免就地修改入参。"""
    return copy.deepcopy(state.get("cost_tracker") or {})


def _unit_score(value: Any, default: float = 0.0) -> float:
    """把模型给出的分数归一到 0-1（大于 1 视为 1-10 分制）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    score = float(value)
    if score > 1.0:
        score /= 10.0
    return max(0.0, min(1.0, score))


def _extract_json(text: str) -> dict | None:
    """从模型回复中提取第一个 JSON 对象。"""
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


# --- collect_node ------------------------------------------------------------


def _fetch_github_repos(limit: int) -> list[dict]:
    """调用 GitHub Search API 获取 AI 相关仓库。

    Args:
        limit: 最多返回的仓库数。

    Returns:
        规范化的采集条目列表；请求失败时返回空列表。
    """
    params = urllib.parse.urlencode(
        {
            "q": GITHUB_QUERY,
            "sort": "stars",
            "order": "desc",
            "per_page": limit,
        }
    )
    url = f"{GITHUB_SEARCH_URL}?{params}"

    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "knowledge-nodes",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token.strip()}"

    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        logger.error("GitHub API HTTP error: %s", exc)
        return []
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.error("GitHub API network error: %s", exc)
        return []
    except json.JSONDecodeError as exc:
        logger.error("GitHub API returned invalid JSON: %s", exc)
        return []

    collected_at = _utc_now()
    items: list[dict] = []
    for repo in payload.get("items", [])[:limit]:
        items.append(
            {
                "id": repo.get("full_name", ""),
                "title": repo.get("name", ""),
                "source_url": repo.get("html_url", ""),
                "source": "github",
                "description": repo.get("description") or "",
                "collected_at": collected_at,
                "metadata": {
                    "stars": repo.get("stargazers_count"),
                    "language": repo.get("language"),
                    "topics": repo.get("topics", []),
                },
            }
        )
    return items


def collect_node(state: KBState) -> dict:
    """采集节点：拉取 GitHub AI 相关仓库。

    Args:
        state: 当前共享状态。

    Returns:
        部分状态更新 ``{"sources": list[dict]}``。
    """
    print(f"[collect_node] 采集 GitHub 仓库（最多 {COLLECT_LIMIT} 条）")
    logger.info("collect_node: querying GitHub Search API")

    sources = _fetch_github_repos(COLLECT_LIMIT)
    logger.info("collect_node: collected %d repo(s)", len(sources))
    print(f"[collect_node] 采集完成：{len(sources)} 条")
    return {"sources": sources}


# --- analyze_node ------------------------------------------------------------


def _analyze_one(item: dict[str, Any]) -> tuple[dict, Any | None]:
    """对单条数据调用 LLM 生成摘要、标签与评分。

    Args:
        item: 采集条目。

    Returns:
        ``(analysis, usage)``；解析失败时 usage 为 ``None``。
    """
    prompt = (
        f"标题：{item.get('title', '')}\n"
        f"描述：{item.get('description', '')}\n"
        f"来源：{item.get('source', '')}\n"
        f"链接：{item.get('source_url', '')}\n"
        f"元数据：{json.dumps(item.get('metadata', {}), ensure_ascii=False)}"
    )
    try:
        data, usage = chat_json(prompt, system=ANALYZE_SYSTEM_PROMPT, temperature=0.3)
        return data, usage
    except (RuntimeError, ValueError) as exc:
        logger.warning("analyze failed for %s: %s", item.get("source_url"), exc)
        fallback = {
            "summary": (item.get("description") or item.get("title", ""))[:50],
            "score": 0.5,
            "score_reason": "LLM 输出不可解析，使用启发式兜底。",
            "tags": [item.get("source", "ai")],
            "highlights": [],
        }
        return fallback, None


def analyze_node(state: KBState) -> dict:
    """分析节点：为每条 sources 生成中文摘要、标签、0-1 评分。

    Args:
        state: 当前共享状态。

    Returns:
        部分状态更新 ``{"analyses": list[dict], "cost_tracker": dict}``。
    """
    sources = state.get("sources", [])
    print(f"[analyze_node] 分析 {len(sources)} 条数据")
    tracker = _tracker(state)

    analyses: list[dict] = []
    for index, item in enumerate(sources, start=1):
        logger.info("analyze_node: %d/%d %s", index, len(sources), item.get("title"))
        analysis, usage = _analyze_one(item)
        if usage is not None:
            accumulate_usage(tracker, usage)

        analyses.append(
            {
                **item,
                "summary": str(analysis.get("summary", "")).strip(),
                "score": _unit_score(analysis.get("score"), default=0.5),
                "score_reason": str(analysis.get("score_reason", "")).strip(),
                "tags": [str(tag).lower() for tag in (analysis.get("tags") or [])][:5],
                "highlights": analysis.get("highlights") or [],
                "analyzed_at": _utc_now(),
            }
        )

    print(f"[analyze_node] 分析完成：{len(analyses)} 条")
    return {"analyses": analyses, "cost_tracker": tracker}


# --- organize_node -----------------------------------------------------------


def _revise_one(item: dict[str, Any], feedback: str) -> tuple[dict, Any | None]:
    """根据审核反馈对单条条目做定向修改。

    Args:
        item: 待修改条目。
        feedback: 审核反馈。

    Returns:
        ``(revised_item, usage)``；失败时返回原条目且 usage 为 ``None``。
    """
    prompt = (
        f"审核反馈：\n{feedback}\n\n"
        f"待修改条目（JSON）：\n{json.dumps(item, ensure_ascii=False)}"
    )
    try:
        data, usage = chat_json(prompt, system=REVISE_SYSTEM_PROMPT, temperature=0.2)
        return {**item, **data}, usage
    except (RuntimeError, ValueError) as exc:
        logger.warning("revise failed for %s: %s", item.get("source_url"), exc)
        return item, None


def _existing_entries() -> list[dict]:
    """读取索引中已有的条目（用于跨运行去重与 id 续号）。"""
    return [
        entry for entry in _load_index().get("entries", []) if isinstance(entry, dict)
    ]


def _known_source_urls(entries: list[dict]) -> set[str]:
    """收集索引里已存在的 ``source_url``。"""
    return {str(entry["source_url"]) for entry in entries if entry.get("source_url")}


def _next_sequence(date_compact: str, entries: list[dict]) -> int:
    """返回当天下一个可用序号，延续已有的 ``{source}-{date}-{NNN}``。"""
    pattern = re.compile(rf"-{date_compact}-(\d{{3}})$")
    sequences = [
        int(match.group(1))
        for entry in entries
        if (match := pattern.search(str(entry.get("id", ""))))
    ]
    return max(sequences, default=0) + 1


def _to_articles(
    items: list[dict], date_compact: str, start_sequence: int = 1
) -> list[dict]:
    """把分析结果规范化为对外契约的知识条目。

    Args:
        items: 分析结果列表。
        date_compact: 当天日期 ``YYYYMMDD``。
        start_sequence: 今天的起始序号（避免与已有条目 id 冲突）。

    Returns:
        规范化后的知识条目列表。
    """
    articles: list[dict] = []
    for offset, item in enumerate(items):
        sequence = start_sequence + offset
        source = _slugify(str(item.get("source", "item")))
        quality = _unit_score(item.get("score"), default=0.5)
        articles.append(
            {
                "id": f"{source}-{date_compact}-{sequence:03d}",
                "title": str(item.get("title", "")),
                "source_url": str(item.get("source_url", "")),
                "source": item.get("source", "github"),
                "summary": str(item.get("summary", "")).strip(),
                "score": max(1, min(10, round(quality * 10))),
                "score_reason": str(item.get("score_reason", "")).strip(),
                "tags": item.get("tags", [])[:5],
                "highlights": item.get("highlights", []),
                "status": "published",
                "collected_at": item.get("collected_at", ""),
                "analyzed_at": item.get("analyzed_at", ""),
                "organized_at": _utc_now(),
            }
        )
    return articles


def organize_node(state: KBState) -> dict:
    """整理节点：过滤低分、按 URL 去重，必要时用 LLM 定向修正。

    Args:
        state: 当前共享状态（读取 ``analyses``、``iteration``、
            ``review_feedback``）。

    Returns:
        部分状态更新 ``{"articles": list[dict], "cost_tracker": dict}``。
    """
    analyses = state.get("analyses", [])
    iteration = state.get("iteration", 0)
    feedback = (state.get("review_feedback") or "").strip()
    print(
        f"[organize_node] 整理 {len(analyses)} 条"
        f"（iteration={iteration}, feedback={'有' if feedback else '无'}）"
    )
    tracker = _tracker(state)

    entries = _existing_entries()
    known_urls = _known_source_urls(entries)

    kept = [
        item for item in analyses if _unit_score(item.get("score"), default=0.0) >= MIN_SCORE
    ]
    logger.info("organize_node: %d/%d kept (score >= %.2f)", len(kept), len(analyses), MIN_SCORE)

    deduped: list[dict] = []
    seen_urls: set[str] = set()
    skipped_known = 0
    for item in kept:
        url = str(item.get("source_url", ""))
        if not url:
            continue
        if url in known_urls:
            skipped_known += 1
            continue
        if url not in seen_urls:
            seen_urls.add(url)
            deduped.append(item)
    logger.info(
        "organize_node: %d new item(s) after de-duplication, %d already indexed",
        len(deduped),
        skipped_known,
    )

    if iteration > 0 and feedback and not state.get("review_passed") and deduped:
        print("[organize_node] 检测到审核反馈，调用 LLM 定向修正")
        revised: list[dict] = []
        for item in deduped:
            new_item, usage = _revise_one(item, feedback)
            if usage is not None:
                accumulate_usage(tracker, usage)
            revised.append(new_item)
        deduped = revised

    date_compact = _today_compact()
    start_sequence = _next_sequence(date_compact, entries)
    articles = _to_articles(deduped, date_compact, start_sequence)
    print(f"[organize_node] 整理完成：{len(articles)} 条")
    return {"articles": articles, "cost_tracker": tracker}


# --- review_node -------------------------------------------------------------


def _review_articles(articles: list[dict]) -> tuple[dict, Any | None]:
    """调用 LLM 对 articles 做四维度审核。

    Args:
        articles: 待审核的知识条目。

    Returns:
        ``(verdict, usage)``；调用失败时 usage 为 ``None``。
    """
    digest = [
        {
            "id": item.get("id"),
            "title": item.get("title"),
            "summary": item.get("summary"),
            "tags": item.get("tags"),
        }
        for item in articles
    ]
    prompt = (
        f"请审核以下 {len(digest)} 条知识条目：\n"
        f"{json.dumps(digest, ensure_ascii=False, indent=2)}"
    )
    try:
        data, usage = chat_json(prompt, system=REVIEW_SYSTEM_PROMPT, temperature=0.0)
        return _normalize_review(data), usage
    except (RuntimeError, ValueError) as exc:
        logger.warning("review failed: %s", exc)
        return {
            "passed": False,
            "overall_score": 0.0,
            "feedback": f"审核调用失败：{exc}",
        }, None


def _normalize_review(data: dict) -> dict:
    """规范化审核结果，``passed`` 由 ``overall_score`` 决定。"""
    dimensions = data.get("dimensions") or {}
    dim_scores = [
        _unit_score(dimensions.get(key))
        for key in ("summary_quality", "tag_accuracy", "classification", "consistency")
        if dimensions.get(key) is not None
    ]

    overall = data.get("overall_score")
    if isinstance(overall, (int, float)) and not isinstance(overall, bool):
        overall_score = _unit_score(overall)
    elif dim_scores:
        overall_score = sum(dim_scores) / len(dim_scores)
    else:
        overall_score = 0.0

    passed = overall_score >= REVIEW_PASS_THRESHOLD
    if isinstance(data.get("passed"), bool) and data["passed"] != passed:
        logger.warning(
            "review said passed=%s but overall_score=%.2f; using score",
            data["passed"],
            overall_score,
        )

    return {
        "passed": passed,
        "overall_score": round(overall_score, 4),
        "feedback": str(data.get("feedback", "")).strip(),
    }


def review_node(state: KBState) -> dict:
    """审核节点：四维度评分并推进 iteration。

    Args:
        state: 当前共享状态（读取 ``articles``、``iteration``）。

    Returns:
        部分状态更新 ``{"review_passed": bool, "review_feedback": str,
        "iteration": int, "cost_tracker": dict}``。
    """
    articles = state.get("articles", [])
    iteration = state.get("iteration", 0)
    print(f"[review_node] 审核第 {iteration + 1}/{MAX_ITERATIONS} 轮：{len(articles)} 条")
    tracker = _tracker(state)

    verdict, usage = _review_articles(articles)
    if usage is not None:
        accumulate_usage(tracker, usage)

    logger.info(
        "review_node: passed=%s overall=%.2f feedback=%s",
        verdict["passed"],
        verdict["overall_score"],
        verdict["feedback"] or "（无）",
    )
    return {
        "review_passed": verdict["passed"],
        "review_feedback": verdict["feedback"],
        "iteration": iteration + 1,
        "cost_tracker": tracker,
    }


# --- save_node ---------------------------------------------------------------


def _load_index() -> dict:
    """读取 ``knowledge/articles/index.json``，缺失或损坏时返回空索引。"""
    if not INDEX_FILE.exists():
        return {"entries": []}
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("index.json unreadable (%s); starting fresh", exc)
        return {"entries": []}


def save_node(state: KBState) -> dict:
    """落盘节点：写入每条 article 的 JSON 文件并刷新 index.json。

    Args:
        state: 当前共享状态（读取 ``articles``）。

    Returns:
        空的部分状态更新 ``{}``。
    """
    articles = state.get("articles", [])
    print(f"[save_node] 写入 {len(articles)} 条到 knowledge/articles/")

    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    date = _today_iso()
    index = _load_index()
    entries: list[dict] = [
        entry for entry in index.get("entries", []) if isinstance(entry, dict)
    ]
    known_ids = {entry.get("id") for entry in entries}

    written = 0
    for article in articles:
        if article.get("id") in known_ids:
            logger.info("save_node: skip already-indexed %s", article.get("id"))
            continue

        filename = f"{date}-{_slugify(str(article.get('source', 'item')))}-{_slugify(str(article.get('title', '')))}.json"
        path = ARTICLES_DIR / filename
        path.write_text(
            json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written += 1

        entries.append(
            {
                "id": article.get("id"),
                "title": article.get("title"),
                "file": filename,
                "source_url": article.get("source_url"),
                "tags": article.get("tags", []),
                "score": article.get("score"),
                "status": article.get("status"),
                "organized_at": article.get("organized_at"),
            }
        )
        known_ids.add(article.get("id"))
        logger.info("save_node: wrote %s", path)

    if written == 0:
        print(f"[save_node] 无新增条目，保持索引不变（共 {len(entries)} 条）")
        return {}

    entries.sort(key=lambda entry: entry.get("organized_at", ""), reverse=True)
    INDEX_FILE.write_text(
        json.dumps(
            {
                "last_updated": _utc_now(),
                "total_count": len(entries),
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"[save_node] 完成：写入 {written} 个文件，索引共 {len(entries)} 条")
    return {}


__all__ = [
    "collect_node",
    "analyze_node",
    "organize_node",
    "review_node",
    "save_node",
]
