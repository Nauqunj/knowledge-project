#!/usr/bin/env python3
"""Four-step knowledge pipeline: collect, analyze, organize and save.

Steps:
    1. Collect: fetch AI-related items from the GitHub Search API and RSS feeds.
    2. Analyze: ask the configured LLM for a summary, score and tags per item.
    3. Organize: deduplicate, normalise to the article schema and validate.
    4. Save: write each article as its own JSON file under ``knowledge/articles``.

Examples:
    python pipeline/pipeline.py --sources github,rss --limit 20
    python pipeline/pipeline.py --sources github --limit 5
    python pipeline/pipeline.py --sources rss --limit 10
    python pipeline/pipeline.py --sources github --limit 5 --dry-run
    python pipeline/pipeline.py --verbose

Environment:
    GITHUB_TOKEN: Optional; raises the GitHub API rate limit.
    LLM_PROVIDER / DEEPSEEK_API_KEY / DASHSCOPE_API_KEY / OPENAI_API_KEY:
        Passed through to :mod:`model_client` for the analyze step.

Only the standard library and ``httpx`` (via :mod:`model_client`) are used.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx

try:  # optional dependency, needed only for the RSS source config
    import yaml
except ImportError:  # pragma: no cover - reported at runtime
    yaml = None

try:  # running as a script: ``python pipeline/pipeline.py``
    from model_client import chat_with_retry, create_provider, estimate_cost
except ImportError:  # running as a package module
    from .model_client import chat_with_retry, create_provider, estimate_cost

logger = logging.getLogger("pipeline")

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "knowledge" / "raw"
ARTICLES_DIR = REPO_ROOT / "knowledge" / "articles"
INDEX_FILE = ARTICLES_DIR / "index.json"

GITHUB_SEARCH_URL = "https://api.github.com/search/repositories"
GITHUB_QUERY = 'AI OR LLM OR agent OR "large language model" OR RAG OR MCP'
DEFAULT_TIMEOUT = 60.0

RSS_SOURCES_FILE = Path(__file__).resolve().parent / "rss_sources.yaml"

SUPPORTED_SOURCES = ("github", "rss")

REQUIRED_FIELDS = ("id", "title", "source_url", "summary", "tags", "status")
VALID_STATUS = frozenset({"draft", "review", "published", "archived"})
MIN_SUMMARY_LEN = 20

SYSTEM_PROMPT = (
    "你是技术情报分析助手。只输出一个 JSON 对象，不要任何多余文字或 Markdown 代码块。"
    "字段：summary（不超过 50 字的中文摘要）、score（1-10 的整数）、"
    "score_reason（简明理由）、tags（3-5 个英文小写连字符标签）、"
    "highlights（2-3 个基于事实的技术亮点）、"
    "audience（可选，beginner/intermediate/advanced 之一）。"
)

_ITEM_RE = re.compile(r"<item\b[^>]*>(.*?)</item>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<{tag}\b[^>]*>(.*?)</{tag}>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CDATA_RE = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.DOTALL)


# --- shared helpers ----------------------------------------------------------


def utc_now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today_iso() -> str:
    """Return today's date as ``YYYY-MM-DD`` (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def today_compact() -> str:
    """Return today's date as ``YYYYMMDD`` (UTC)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def strip_html(text: str) -> str:
    """Remove HTML tags and collapse whitespace.

    Args:
        text: Raw markup text.

    Returns:
        Plain text with tags stripped.
    """
    return re.sub(r"\s+", " ", _HTML_TAG_RE.sub(" ", text)).strip()


def slugify(title: str, max_length: int = 50) -> str:
    """Build a URL-friendly slug from a title.

    Args:
        title: Source title.
        max_length: Maximum slug length.

    Returns:
        Lowercase hyphenated slug, or ``untitled`` when nothing survives.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    slug = slug[:max_length].rstrip("-")
    return slug or "untitled"


# --- step 1: collect ---------------------------------------------------------


def collect_github(limit: int, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Collect repositories from the GitHub Search API.

    Args:
        limit: Maximum repositories to return.
        timeout: HTTP timeout in seconds.

    Returns:
        A list of normalised collection items.
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "knowledge-pipeline",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    params = {
        "q": GITHUB_QUERY,
        "sort": "stars",
        "order": "desc",
        "per_page": str(limit),
    }

    logger.info("collecting github: limit=%d", limit)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        response = client.get(GITHUB_SEARCH_URL, headers=headers, params=params)
        response.raise_for_status()
        payload = response.json()

    collected_at = utc_now()
    items: list[dict] = []
    for repo in payload.get("items", [])[:limit]:
        items.append(
            {
                "id": repo.get("full_name", ""),
                "title": repo.get("name", ""),
                "source_url": repo.get("html_url", ""),
                "description": repo.get("description") or "",
                "source": "github",
                "collected_at": collected_at,
                "metadata": {
                    "stars": repo.get("stargazers_count"),
                    "language": repo.get("language"),
                    "topics": repo.get("topics", []),
                    "created_at": repo.get("created_at"),
                    "pushed_at": repo.get("pushed_at"),
                },
            }
        )
    logger.info("collected %d github repositories", len(items))
    return items


def _extract_tag(block: str, tag: str) -> str:
    """Extract and clean the text of the first matching XML tag."""
    match = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", block, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    value = match.group(1).strip()
    cdata = _CDATA_RE.match(value)
    if cdata:
        value = cdata.group(1)
    return html.unescape(strip_html(value))


def _extract_link(block: str) -> str:
    """Extract an RSS ``<link>`` value, supporting Atom ``href`` form."""
    href = re.search(r"<link\b[^>]*href=[\"']([^\"']+)[\"']", block, re.IGNORECASE)
    if href:
        return href.group(1).strip()
    return _extract_tag(block, "link")


def parse_rss(
    xml_text: str,
    source: str,
    collected_at: str,
    feed: dict | None = None,
) -> list[dict]:
    """Parse RSS ``<item>`` entries from raw XML.

    Args:
        xml_text: Raw RSS XML.
        source: Feed key used to tag items.
        collected_at: ISO timestamp applied to each item.
        feed: Optional feed metadata (``name`` / ``category``) to attach.

    Returns:
        A list of normalised collection items.
    """
    feed_name = (feed or {}).get("name")
    category = (feed or {}).get("category")
    items: list[dict] = []
    for block in _ITEM_RE.findall(xml_text):
        title = _extract_tag(block, "title")
        link = _extract_link(block)
        if not title or not link:
            continue
        items.append(
            {
                "id": link,
                "title": title,
                "source_url": link,
                "description": _extract_tag(block, "description"),
                "source": source,
                "collected_at": collected_at,
                "metadata": {
                    "feed": feed_name,
                    "category": category,
                    "pub_date": _extract_tag(block, "pubDate"),
                },
            }
        )
    return items


def source_key(feed: dict) -> str:
    """Derive a machine-friendly source key from a feed entry.

    Falls back to the URL host when the name has no ASCII slug (e.g. Chinese).

    Args:
        feed: A feed entry with ``name`` and ``url``.

    Returns:
        A lowercase, hyphenated key safe for ids and filenames.
    """
    key = slugify(str(feed.get("name", "")))
    if key != "untitled":
        return key
    host = urlparse(str(feed.get("url", ""))).netloc
    return slugify(host) or "rss"


def load_rss_sources(path: Path = RSS_SOURCES_FILE) -> list[dict]:
    """Load RSS source definitions from the YAML config.

    Args:
        path: Path to the YAML file.

    Returns:
        The raw list of source entries; empty on any read/parse problem.
    """
    if yaml is None:
        logger.error(
            "PyYAML is not installed; cannot read %s (pip install pyyaml)", path
        )
        return []
    if not path.exists():
        logger.warning("rss sources file not found: %s", path)
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        logger.error("invalid YAML in %s: %s", path, exc)
        return []

    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list):
        logger.error("%s has no 'sources' list", path)
        return []
    return [entry for entry in sources if isinstance(entry, dict)]


def collect_rss(limit: int, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Collect items from the enabled RSS feeds defined in the YAML config.

    Args:
        limit: Maximum items to return across all feeds.
        timeout: HTTP timeout in seconds.

    Returns:
        A list of normalised collection items.
    """
    feeds = [feed for feed in load_rss_sources() if feed.get("enabled")]
    logger.info(
        "rss sources: %d enabled feed(s) selected from %s",
        len(feeds),
        RSS_SOURCES_FILE.name,
    )
    if not feeds:
        logger.warning("no enabled RSS feeds to collect")
        return []

    collected_at = utc_now()
    items: list[dict] = []
    headers = {"User-Agent": "knowledge-pipeline"}

    with httpx.Client(timeout=timeout, headers=headers, follow_redirects=True) as client:
        for feed in feeds:
            feed_name = str(feed.get("name", "unknown"))
            feed_url = str(feed.get("url", ""))
            if not feed_url:
                logger.warning("skipping rss feed %s: missing url", feed_name)
                continue
            try:
                logger.info("collecting rss feed %s (%s)", feed_name, feed_url)
                response = client.get(feed_url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("rss feed %s failed: %s", feed_name, exc)
                continue
            feed_items = parse_rss(
                response.text, source_key(feed), collected_at, feed
            )
            logger.info("rss feed %s yielded %d item(s)", feed_name, len(feed_items))
            items.extend(feed_items)

    logger.info("collected %d rss items", min(len(items), limit))
    return items[:limit]


def collect(sources: list[str], limit: int, timeout: float = DEFAULT_TIMEOUT) -> list[dict]:
    """Run the collect step for the requested sources.

    Args:
        sources: Any of ``github`` and ``rss``.
        limit: Per-source item cap.
        timeout: HTTP timeout in seconds.

    Returns:
        Deduplicated collection items keyed by ``source_url``.
    """
    items: list[dict] = []
    if "github" in sources:
        items.extend(collect_github(limit, timeout))
    if "rss" in sources:
        items.extend(collect_rss(limit, timeout))

    seen: set[str] = set()
    unique: list[dict] = []
    for item in items:
        url = item.get("source_url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(item)
    logger.info("collect step: %d item(s) after de-duplication", len(unique))
    return unique


def save_raw(items: list[dict]) -> list[Path]:
    """Persist collected items to ``knowledge/raw`` per source.

    Args:
        items: Collected items.

    Returns:
        The list of written file paths.
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    date = today_iso()
    written: list[Path] = []

    by_source: dict[str, list[dict]] = {}
    for item in items:
        by_source.setdefault(item.get("source", "unknown"), []).append(item)

    for source, group in by_source.items():
        path = RAW_DIR / f"pipeline-{source}-{date}.json"
        payload = {
            "source": source,
            "collected_at": utc_now(),
            "count": len(group),
            "items": group,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(path)
        logger.info("saved %d raw item(s) to %s", len(group), path)
    return written


# --- step 2: analyze ---------------------------------------------------------


def parse_model_json(text: str) -> dict | None:
    """Extract a JSON object from a model response.

    Args:
        text: Raw model output, possibly wrapped in Markdown fences.

    Returns:
        The parsed object, or ``None`` when parsing fails.
    """
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clamp_score(value: object, default: int = 5) -> int:
    """Coerce a model-provided score into the 1-10 integer range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(1, min(10, int(round(value))))


def _fallback_analysis(item: dict) -> dict:
    """Build a heuristic analysis when the LLM output is unusable."""
    seed = item.get("description") or item.get("title") or ""
    summary = strip_html(seed)[:50] or item.get("title", "")[:50]
    return {
        "summary": summary,
        "score": 5,
        "score_reason": "LLM 输出不可解析，使用启发式兜底评分。",
        "tags": [item.get("source", "ai")],
        "highlights": [],
    }


def analyze_item(item: dict, provider: object) -> tuple[dict, object]:
    """Analyze one item with the LLM and merge the result.

    Args:
        item: A collection item.
        provider: An :class:`~model_client.OpenAICompatibleProvider`.

    Returns:
        A ``(article, response)`` tuple where ``article`` is the merged item
        and ``response`` is the raw LLM response (for usage accounting).
    """
    user_prompt = (
        f"标题：{item.get('title', '')}\n"
        f"来源：{item.get('source', '')}\n"
        f"链接：{item.get('source_url', '')}\n"
        f"描述：{item.get('description', '')}\n"
        f"元数据：{json.dumps(item.get('metadata', {}), ensure_ascii=False)}"
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    response = chat_with_retry(
        messages, provider=provider, temperature=0.3, max_tokens=600
    )

    analysis = parse_model_json(response.content)
    if analysis is None:
        logger.warning("unparseable LLM output for %s; using fallback", item.get("id"))
        analysis = _fallback_analysis(item)

    tags = analysis.get("tags")
    if not isinstance(tags, list) or not tags:
        tags = [item.get("source", "ai")]

    article = dict(item)
    article.update(
        {
            "summary": str(analysis.get("summary", "")).strip(),
            "score": _clamp_score(analysis.get("score")),
            "score_reason": str(analysis.get("score_reason", "")).strip(),
            "tags": [str(tag).lower() for tag in tags][:5],
            "highlights": analysis.get("highlights") or [],
            "analyzed_at": utc_now(),
        }
    )
    audience = analysis.get("audience")
    if isinstance(audience, str) and audience in {"beginner", "intermediate", "advanced"}:
        article["audience"] = audience
    return article, response


def analyze(items: list[dict], provider: object) -> list[dict]:
    """Run the analyze step across all items.

    Args:
        items: Collection items.
        provider: The LLM provider.

    Returns:
        Analyzed articles.
    """
    articles: list[dict] = []
    total_tokens = 0
    total_cost = 0.0

    for index, item in enumerate(items, start=1):
        logger.info("analyzing %d/%d: %s", index, len(items), item.get("title"))
        article, response = analyze_item(item, provider)
        usage = getattr(response, "usage", None)
        if usage is not None:
            total_tokens += usage.total_tokens
            total_cost += estimate_cost(usage, provider_name=response.provider)
        articles.append(article)

    logger.info(
        "analyze step: %d article(s), %d tokens, est. $%.6f",
        len(articles),
        total_tokens,
        total_cost,
    )
    return articles


# --- step 3: organize --------------------------------------------------------


def load_index() -> dict:
    """Load ``knowledge/articles/index.json`` if present."""
    if not INDEX_FILE.exists():
        return {"last_updated": utc_now(), "total_count": 0, "entries": []}
    try:
        return json.loads(INDEX_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        logger.warning("index.json is invalid; starting a fresh index")
        return {"last_updated": utc_now(), "total_count": 0, "entries": []}


def existing_source_urls() -> set[str]:
    """Collect ``source_url`` values from already-saved articles."""
    urls: set[str] = set()
    if not ARTICLES_DIR.exists():
        return urls
    for path in ARTICLES_DIR.glob("*.json"):
        if path.name == "index.json":
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        url = data.get("source_url") or data.get("url")
        if isinstance(url, str) and url:
            urls.add(url)
    return urls


def next_sequence(date_compact: str, existing_ids: set[str]) -> int:
    """Return the next ID sequence number for the given day."""
    pattern = re.compile(rf"-{date_compact}-(\d{{3}})$")
    sequences = [
        int(match.group(1))
        for identifier in existing_ids
        if (match := pattern.search(identifier))
    ]
    return max(sequences, default=0) + 1


def validate_article(article: dict) -> list[str]:
    """Validate an article against the required contract.

    Args:
        article: The article to check.

    Returns:
        A list of human-readable problems; empty when valid.
    """
    errors: list[str] = []
    for field_name in REQUIRED_FIELDS:
        if not article.get(field_name):
            errors.append(f"missing {field_name}")

    summary = article.get("summary")
    if isinstance(summary, str) and len(summary) < MIN_SUMMARY_LEN:
        errors.append(f"summary shorter than {MIN_SUMMARY_LEN} characters")

    tags = article.get("tags")
    if not isinstance(tags, list) or not tags:
        errors.append("tags must be a non-empty list")

    status = article.get("status")
    if status not in VALID_STATUS:
        errors.append(f"status {status!r} is not one of {sorted(VALID_STATUS)}")

    source_url = article.get("source_url")
    if isinstance(source_url, str) and not source_url.startswith(("http://", "https://")):
        errors.append(f"source_url {source_url!r} is not an http(s) URL")

    return errors


def organize(articles: list[dict]) -> tuple[list[dict], list[dict]]:
    """Deduplicate, normalise and validate analyzed articles.

    Args:
        articles: Analyzed articles from step 2.

    Returns:
        A ``(accepted, rejected)`` tuple; ``rejected`` entries carry a
        ``reasons`` key.
    """
    index = load_index()
    known_urls = existing_source_urls()
    date_compact = today_compact()

    existing_ids = {
        entry.get("id", "") for entry in index.get("entries", []) if entry.get("id")
    }
    sequence = next_sequence(date_compact, existing_ids)

    accepted: list[dict] = []
    rejected: list[dict] = []
    seen_urls: set[str] = set()

    for article in articles:
        url = article.get("source_url", "")
        if url in known_urls or url in seen_urls:
            rejected.append({"id": article.get("id"), "reasons": ["duplicate url"]})
            continue
        seen_urls.add(url)

        article = dict(article)
        article["id"] = f"{article.get('source', 'item')}-{date_compact}-{sequence:03d}"
        article["status"] = "published"
        article["organized_at"] = utc_now()
        sequence += 1

        errors = validate_article(article)
        if errors:
            rejected.append({"id": article.get("id"), "reasons": errors})
            continue
        accepted.append(article)

    logger.info(
        "organize step: %d accepted, %d rejected", len(accepted), len(rejected)
    )
    return accepted, rejected


# --- step 4: save ------------------------------------------------------------


def save_articles(articles: list[dict]) -> list[Path]:
    """Write each article to its own JSON file and refresh the index.

    Args:
        articles: Accepted articles.

    Returns:
        The list of created article file paths.
    """
    ARTICLES_DIR.mkdir(parents=True, exist_ok=True)
    date = today_iso()
    index = load_index()
    entries: list[dict] = list(index.get("entries", []))

    written: list[Path] = []
    for article in articles:
        filename = f"{date}-{article.get('source', 'item')}-{slugify(article['title'])}.json"
        path = ARTICLES_DIR / filename
        path.write_text(
            json.dumps(article, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append(path)

        entries.append(
            {
                "id": article["id"],
                "title": article["title"],
                "file": filename,
                "source_url": article["source_url"],
                "tags": article.get("tags", []),
                "score": article.get("score"),
                "status": article["status"],
                "organized_at": article["organized_at"],
            }
        )
        logger.info("saved article %s -> %s", article["id"], path)

    entries.sort(key=lambda entry: entry.get("organized_at", ""), reverse=True)
    INDEX_FILE.write_text(
        json.dumps(
            {
                "last_updated": utc_now(),
                "total_count": len(entries),
                "entries": entries,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("index updated: %d entr(ies)", len(entries))
    return written


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        The parsed namespace.
    """
    parser = argparse.ArgumentParser(description="Four-step knowledge pipeline.")
    parser.add_argument(
        "--sources",
        default="github,rss",
        help="Comma-separated sources to collect (github, rss). Default: github,rss",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum items per source. Default: 20",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Collect only; skip LLM analysis and file writes.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug-level logging.",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Execute the pipeline.

    Args:
        args: Parsed CLI arguments.

    Returns:
        Process exit code: ``0`` on success, ``1`` on failure.
    """
    sources = [part.strip().lower() for part in args.sources.split(",") if part.strip()]
    unknown = [source for source in sources if source not in SUPPORTED_SOURCES]
    if unknown:
        logger.error("unknown source(s): %s", ", ".join(unknown))
        return 1
    if not sources:
        logger.error("no sources selected")
        return 1

    logger.info("step 1/4 collect: sources=%s limit=%d", sources, args.limit)
    try:
        items = collect(sources, args.limit)
    except httpx.HTTPError as exc:
        logger.error("collect step failed: %s", exc)
        return 1
    if not items:
        logger.warning("no items collected; nothing to do")
        return 0

    if args.dry_run:
        logger.info(
            "dry-run: collected %d item(s); skipping analysis and writes", len(items)
        )
        return 0

    save_raw(items)

    logger.info("step 2/4 analyze")
    try:
        provider = create_provider()
    except (ValueError, RuntimeError) as exc:
        logger.error("cannot initialise LLM provider: %s", exc)
        return 1
    articles = analyze(items, provider)

    logger.info("step 3/4 organize")
    accepted, rejected = organize(articles)
    for entry in rejected:
        logger.warning("rejected %s: %s", entry.get("id"), "; ".join(entry["reasons"]))

    logger.info("step 4/4 save")
    written = save_articles(accepted)

    logger.info(
        "pipeline done: %d collected, %d analyzed, %d accepted, %d rejected, %d written",
        len(items),
        len(articles),
        len(accepted),
        len(rejected),
        len(written),
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point wiring argument parsing and logging.

    Args:
        argv: Argument list; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
