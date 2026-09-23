#!/usr/bin/env python3
"""Score knowledge entries on five weighted quality dimensions.

Usage:
    python hooks/check_quality.py <json_file> [json_file2 ...]

Each argument may be a path or a glob pattern (e.g. "knowledge/articles/*.json").
Prints a progress bar and per-dimension score for every file, then a summary.

Grades: A >= 80, B >= 60, C < 60.
Exit code: 1 if any file grades C (or cannot be scored), 0 otherwise.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# --- scoring constants -------------------------------------------------------

SUMMARY_MAX = 25.0
DEPTH_MAX = 25.0
FORMAT_MAX = 20.0
TAGS_MAX = 15.0
CLICHE_MAX = 15.0

GRADE_A_THRESHOLD = 80.0
GRADE_B_THRESHOLD = 60.0

SUMMARY_FULL_LEN = 50
SUMMARY_BASIC_LEN = 20
SUMMARY_KEYWORD_BONUS = 5.0

TECH_KEYWORDS: frozenset[str] = frozenset(
    {
        "llm",
        "rag",
        "agent",
        "transformer",
        "mcp",
        "embedding",
        "fine-tune",
        "finetune",
        "inference",
        "quantization",
        "pytorch",
        "tensorflow",
        "gpu",
        "多模态",
        "微调",
        "推理",
        "向量",
        "神经网络",
        "深度学习",
        "大模型",
        "扩散模型",
        "蒸馏",
    }
)

STANDARD_TAGS: frozenset[str] = frozenset(
    {
        "large-language-model",
        "llm",
        "rag",
        "agent",
        "agent-framework",
        "agent-harness",
        "multi-agent",
        "mcp",
        "prompt-engineering",
        "fine-tuning",
        "quantization",
        "inference",
        "llm-inference",
        "code-generation",
        "data-analysis",
        "tool-use",
        "evaluation",
        "safety",
        "alignment",
        "robotics",
        "openai",
        "anthropic",
        "langchain",
        "python",
        "typescript",
        "golang",
        "rust",
        "transformer",
        "diffusion-model",
        "embedding",
        "vector-database",
        "structured-output",
        "model-routing",
        "context-compaction",
    }
)

FORMAT_FIELDS: tuple[str, ...] = ("id", "title", "source_url", "status")
TIMESTAMP_FIELDS: tuple[str, ...] = (
    "collected_at",
    "analyzed_at",
    "organized_at",
    "published_at",
)
FORMAT_FIELD_POINTS = FORMAT_MAX / (len(FORMAT_FIELDS) + 1)  # 5 checks * 4 points

EMPTY_WORDS_ZH: tuple[str, ...] = (
    "赋能",
    "抓手",
    "闭环",
    "打通",
    "全链路",
    "底层逻辑",
    "颗粒度",
    "对齐",
    "拉通",
    "沉淀",
    "强大的",
    "颠覆性",
    "生态化",
    "降本增效",
    "方法论",
    "全方位",
)

EMPTY_WORDS_EN: tuple[str, ...] = (
    "groundbreaking",
    "revolutionary",
    "game-changing",
    "game changing",
    "cutting-edge",
    "cutting edge",
    "disruptive",
    "world-class",
    "world class",
    "best-in-class",
    "state-of-the-art",
    "next-generation",
    "paradigm-shifting",
    "synergy",
    "holistic",
    "seamless",
)

EMPTY_WORD_PENALTY = 5.0
BAR_WIDTH = 20


# --- data structures ---------------------------------------------------------


@dataclass(frozen=True)
class DimensionScore:
    """Score awarded for one quality dimension."""

    name: str
    score: float
    max_score: float
    detail: str = ""

    @property
    def ratio(self) -> float:
        if self.max_score <= 0:
            return 0.0
        return max(0.0, min(1.0, self.score / self.max_score))

    def bar(self, width: int = BAR_WIDTH) -> str:
        filled = round(self.ratio * width)
        return "[" + "\u2588" * filled + "\u2591" * (width - filled) + "]"


@dataclass
class QualityReport:
    """Aggregate quality report for a single knowledge entry."""

    path: Path
    dimensions: list[DimensionScore] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(dimension.score for dimension in self.dimensions)

    @property
    def max_total(self) -> float:
        return sum(dimension.max_score for dimension in self.dimensions)

    @property
    def grade(self) -> str:
        if self.total >= GRADE_A_THRESHOLD:
            return "A"
        if self.total >= GRADE_B_THRESHOLD:
            return "B"
        return "C"


# --- helpers -----------------------------------------------------------------


def configure_stdout() -> None:
    """Force UTF-8 output so progress bars render on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def expand_inputs(patterns: list[str]) -> tuple[list[Path], list[str]]:
    """Expand glob patterns into a sorted, de-duplicated list of paths."""
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()

    for pattern in patterns:
        matches = sorted(Path(match) for match in glob.glob(pattern, recursive=True))
        if not matches:
            errors.append(f"{pattern}: no files matched")
            continue
        for path in matches:
            if path.is_dir():
                errors.append(f"{path}: is a directory, not a JSON file")
            elif path not in seen:
                seen.add(path)
                paths.append(path)

    return paths, errors


def collect_text(entry: dict) -> str:
    """Join the human-readable fields for keyword and cliche scanning."""
    parts: list[str] = []
    for key in ("title", "summary"):
        value = entry.get(key)
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


# --- dimension scorers -------------------------------------------------------


def score_summary(entry: dict) -> DimensionScore:
    text = entry.get("summary")
    if not isinstance(text, str) or not text:
        return DimensionScore("摘要质量", 0.0, SUMMARY_MAX, "missing summary")

    length = len(text)
    if length >= SUMMARY_FULL_LEN:
        base = SUMMARY_MAX
    elif length >= SUMMARY_BASIC_LEN:
        base = 15.0
    else:
        base = 5.0

    lowered = text.lower()
    has_keyword = any(keyword in lowered for keyword in TECH_KEYWORDS)
    bonus = SUMMARY_KEYWORD_BONUS if has_keyword else 0.0
    score = min(SUMMARY_MAX, base + bonus)

    detail = f"{length} chars"
    if has_keyword:
        detail += ", +keyword"
    return DimensionScore("摘要质量", score, SUMMARY_MAX, detail)


def score_depth(entry: dict) -> DimensionScore:
    value = entry.get("score")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return DimensionScore("技术深度", 0.0, DEPTH_MAX, "missing score")

    clamped = max(1.0, min(10.0, float(value)))
    score = clamped / 10.0 * DEPTH_MAX
    return DimensionScore("技术深度", score, DEPTH_MAX, f"score={value}")


def score_format(entry: dict) -> DimensionScore:
    awarded = 0.0
    missing: list[str] = []

    for field_name in FORMAT_FIELDS:
        value = entry.get(field_name)
        if isinstance(value, str) and value.strip():
            awarded += FORMAT_FIELD_POINTS
        else:
            missing.append(field_name)

    has_timestamp = any(
        isinstance(entry.get(key), str) and entry.get(key, "").strip()
        for key in TIMESTAMP_FIELDS
    )
    if has_timestamp:
        awarded += FORMAT_FIELD_POINTS
    else:
        missing.append("timestamp")

    detail = "complete" if not missing else "missing: " + ", ".join(missing)
    return DimensionScore("格式规范", awarded, FORMAT_MAX, detail)


def score_tags(entry: dict) -> DimensionScore:
    tags = entry.get("tags")
    if not isinstance(tags, list) or not tags:
        return DimensionScore("标签精度", 0.0, TAGS_MAX, "no tags")

    count = len(tags)
    valid = sum(1 for tag in tags if isinstance(tag, str) and tag in STANDARD_TAGS)

    if count <= 3:
        if valid == count:
            score = TAGS_MAX
        elif valid > 0:
            score = 10.0
        else:
            score = 6.0
    else:
        score = TAGS_MAX - (count - 3) * 3.0
        if valid < count:
            score -= 3.0
        score = max(0.0, score)

    detail = f"{count} tag(s), {valid} standard"
    return DimensionScore("标签精度", score, TAGS_MAX, detail)


def find_empty_words(text: str) -> list[str]:
    hits: list[str] = []
    for word in EMPTY_WORDS_ZH:
        if word in text:
            hits.append(word)

    lowered = text.lower()
    for word in EMPTY_WORDS_EN:
        pattern = r"\b" + re.escape(word) + r"\b"
        if re.search(pattern, lowered):
            hits.append(word)
    return hits


def score_cliche(entry: dict) -> DimensionScore:
    hits = find_empty_words(collect_text(entry))
    score = max(0.0, CLICHE_MAX - len(hits) * EMPTY_WORD_PENALTY)
    detail = "clean" if not hits else "hits: " + ", ".join(hits)
    return DimensionScore("空洞词检测", score, CLICHE_MAX, detail)


# --- report assembly ---------------------------------------------------------


def build_report(path: Path) -> QualityReport:
    report = QualityReport(path=path)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        report.notes.append(f"cannot read file: {exc}")
        return report

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        report.notes.append(f"invalid JSON: {exc}")
        return report

    if not isinstance(data, dict):
        report.notes.append(
            f"top-level JSON value must be an object, got {type(data).__name__}"
        )
        return report

    report.dimensions = [
        score_summary(data),
        score_depth(data),
        score_format(data),
        score_tags(data),
        score_cliche(data),
    ]
    return report


def render_report(report: QualityReport) -> None:
    print(f"\n{report.path}  grade {report.grade}  {report.total:.1f}/{report.max_total:.0f}")
    for dimension in report.dimensions:
        print(
            f"  {dimension.name:<6} {dimension.bar()} "
            f"{dimension.score:5.1f}/{dimension.max_score:.0f}  {dimension.detail}"
        )
    for note in report.notes:
        print(f"  ! {note}")


def main(argv: list[str] | None = None) -> int:
    configure_stdout()

    parser = argparse.ArgumentParser(
        description="Score knowledge entries on five quality dimensions.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="JSON files or glob patterns, e.g. knowledge/articles/*.json",
    )
    args = parser.parse_args(argv)

    paths, expand_errors = expand_inputs(args.files)

    for pattern_error in expand_errors:
        print(f"FAIL {pattern_error}")

    grade_counts = {"A": 0, "B": 0, "C": 0}
    for path in paths:
        report = build_report(path)
        render_report(report)
        grade_counts[report.grade] += 1

    checked = len(paths)
    passing = grade_counts["A"] + grade_counts["B"]
    has_failure = grade_counts["C"] > 0 or bool(expand_errors)

    print(
        f"\nSummary: {checked} file(s) checked, {passing} passed, "
        f"{grade_counts['C']} graded C, {len(expand_errors)} unmatched pattern(s); "
        f"A={grade_counts['A']} B={grade_counts['B']} C={grade_counts['C']}"
    )

    return 1 if has_failure else 0


if __name__ == "__main__":
    sys.exit(main())
