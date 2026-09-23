#!/usr/bin/env python3
"""Validate knowledge-entry JSON files.

Usage:
    python hooks/validate_json.py <json_file> [json_file2 ...]

Each argument may be a path or a glob pattern (e.g. "knowledge/articles/*.json").
Exits with code 0 when every file is valid, 1 when any file fails or a pattern
matches nothing.

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

REQUIRED_FIELDS: dict[str, type] = {
    "id": str,
    "title": str,
    "source_url": str,
    "summary": str,
    "tags": list,
    "status": str,
}

VALID_STATUS: frozenset[str] = frozenset({"draft", "review", "published", "archived"})
VALID_AUDIENCE: frozenset[str] = frozenset({"beginner", "intermediate", "advanced"})

# {source}-{YYYYMMDD}-{NNN}, e.g. github-20260317-001
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*-\d{8}-\d{3}$")
URL_RE = re.compile(r"^https?://\S+$")

MIN_SUMMARY_LEN = 20
MIN_TAGS = 1
SCORE_MIN = 1
SCORE_MAX = 10


def expand_inputs(patterns: list[str]) -> tuple[list[Path], list[str]]:
    """Expand glob patterns into a sorted, de-duplicated list of paths.

    Returns a ``(paths, errors)`` tuple; ``errors`` lists patterns that matched
    no files.
    """
    paths: list[Path] = []
    errors: list[str] = []
    seen: set[Path] = set()

    for pattern in patterns:
        matches = [Path(match) for match in glob.glob(pattern, recursive=True)]
        if not matches:
            errors.append(f"{pattern}: no files matched")
            continue
        for path in sorted(matches):
            if path.is_dir():
                errors.append(f"{path}: is a directory, not a JSON file")
            elif path not in seen:
                seen.add(path)
                paths.append(path)

    return paths, errors


def _check_required_fields(data: dict) -> list[str]:
    errors: list[str] = []
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in data:
            errors.append(f"missing required field: {field}")
        elif not isinstance(data[field], expected_type):
            actual = type(data[field]).__name__
            errors.append(
                f"field {field!r} must be {expected_type.__name__}, got {actual}"
            )
    return errors


def _check_id(data: dict) -> list[str]:
    value = data.get("id")
    if isinstance(value, str) and not ID_RE.match(value):
        return [
            f"id {value!r} must match {{source}}-{{YYYYMMDD}}-{{NNN}}, "
            "e.g. github-20260317-001"
        ]
    return []


def _check_status(data: dict) -> list[str]:
    value = data.get("status")
    if isinstance(value, str) and value not in VALID_STATUS:
        allowed = "/".join(sorted(VALID_STATUS))
        return [f"status {value!r} must be one of {allowed}"]
    return []


def _check_source_url(data: dict) -> list[str]:
    value = data.get("source_url")
    if isinstance(value, str) and not URL_RE.match(value):
        return [f"source_url {value!r} must start with http:// or https://"]
    return []


def _check_summary(data: dict) -> list[str]:
    value = data.get("summary")
    if isinstance(value, str) and len(value) < MIN_SUMMARY_LEN:
        return [
            f"summary must be at least {MIN_SUMMARY_LEN} characters, "
            f"got {len(value)}"
        ]
    return []


def _check_tags(data: dict) -> list[str]:
    value = data.get("tags")
    if isinstance(value, list) and len(value) < MIN_TAGS:
        return [f"tags must contain at least {MIN_TAGS} item(s), got {len(value)}"]
    return []


def _check_score(data: dict) -> list[str]:
    if "score" not in data:
        return []
    value = data["score"]
    is_number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if not is_number or not (SCORE_MIN <= value <= SCORE_MAX):
        return [f"score must be a number between {SCORE_MIN} and {SCORE_MAX}, got {value!r}"]
    return []


def _check_audience(data: dict) -> list[str]:
    if "audience" not in data:
        return []
    value = data["audience"]
    if not isinstance(value, str) or value not in VALID_AUDIENCE:
        allowed = "/".join(sorted(VALID_AUDIENCE))
        return [f"audience must be one of {allowed}, got {value!r}"]
    return []


def validate_entry(data: dict) -> list[str]:
    """Return a list of validation errors for a single knowledge entry."""
    errors: list[str] = []
    errors += _check_required_fields(data)
    errors += _check_id(data)
    errors += _check_status(data)
    errors += _check_source_url(data)
    errors += _check_summary(data)
    errors += _check_tags(data)
    errors += _check_score(data)
    errors += _check_audience(data)
    return errors


def validate_file(path: Path) -> list[str]:
    """Read and validate one JSON file, returning its errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [f"cannot read file: {exc}"]

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return [f"invalid JSON: {exc}"]

    if not isinstance(data, dict):
        return [f"top-level JSON value must be an object, got {type(data).__name__}"]

    return validate_entry(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate knowledge-entry JSON files.",
    )
    parser.add_argument(
        "files",
        nargs="+",
        help="JSON files or glob patterns, e.g. knowledge/articles/*.json",
    )
    args = parser.parse_args(argv)

    paths, expand_errors = expand_inputs(args.files)

    failed_files = 0
    total_errors = len(expand_errors)

    for pattern_error in expand_errors:
        print(f"FAIL {pattern_error}")

    for path in paths:
        errors = validate_file(path)
        if errors:
            failed_files += 1
            total_errors += len(errors)
            print(f"FAIL {path}")
            for error in errors:
                print(f"  - {error}")
        else:
            print(f"OK   {path}")

    checked = len(paths)
    passed = checked - failed_files
    invalid_units = failed_files + len(expand_errors)
    print(
        f"\nSummary: {checked} file(s) checked, {passed} passed, "
        f"{failed_files} failed, {len(expand_errors)} unmatched pattern(s), "
        f"{total_errors} error(s)"
    )

    return 1 if invalid_units else 0


if __name__ == "__main__":
    sys.exit(main())
