#!/usr/bin/env python3
"""多 Agent 预算守卫 — 为 LangGraph 流水线中的各节点做成本熔断。

在流水线里，collector / analyzer / organizer 都会调用 LLM。本模块用
:class:`CostGuard` 汇总每个节点的 token 用量与人民币成本，并提供三重保护：

1. 正常（``ok``）：成本低于预警线。
2. 预警（``warning``）：成本达到 ``alert_threshold * budget_yuan``。
3. 熔断（``exceeded``）：成本超过 ``budget_yuan``，``check`` 抛出
   :class:`BudgetExceededError`，由调用方中断流水线。

价格单位统一为“元 / 百万 token”。可直接运行本文件自测：

    python tests/cost_guard.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from workflows.model_client import Usage  # noqa: E402

PRICE_PER_MILLION = 1_000_000

DEFAULT_BUDGET_YUAN = 1.0
DEFAULT_ALERT_THRESHOLD = 0.8
DEFAULT_INPUT_PRICE_PER_MILLION = 1.0
DEFAULT_OUTPUT_PRICE_PER_MILLION = 2.0


@dataclass(frozen=True)
class CostRecord:
    """单次 LLM 调用的成本快照。

    Attributes:
        timestamp: 记录时间，ISO 8601（UTC）。
        node_name: 触发调用的流水线节点，如 ``analyzer``。
        prompt_tokens: 输入 token 数。
        completion_tokens: 输出 token 数。
        total_tokens: 总 token 数。
        model: 模型标识，可为空。
        cost_yuan: 本次调用估算成本（元）。
    """

    timestamp: str
    node_name: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    cost_yuan: float


class BudgetExceededError(Exception):
    """成本超过预算时抛出。

    Attributes:
        report: 触发熔断时的预算状态字典，``report["status"] == "exceeded"``。
    """

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__(
            f"预算超限：已用 {report['used_yuan']:.6f} 元 > "
            f"预算 {report['budget_yuan']:.6f} 元"
        )


class CostGuard:
    """跨节点预算守卫，聚合 LLM token 用量并按预算熔断。"""

    def __init__(
        self,
        budget_yuan: float = DEFAULT_BUDGET_YUAN,
        alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
        input_price_per_million: float = DEFAULT_INPUT_PRICE_PER_MILLION,
        output_price_per_million: float = DEFAULT_OUTPUT_PRICE_PER_MILLION,
    ) -> None:
        """初始化守卫。

        Args:
            budget_yuan: 总预算，单位元，必须为正数。
            alert_threshold: 预警比例，``0 < threshold <= 1``。
            input_price_per_million: 输入 token 单价，元 / 百万 token。
            output_price_per_million: 输出 token 单价，元 / 百万 token。

        Raises:
            ValueError: 预算非正数或预警比例越界。
        """
        if budget_yuan <= 0:
            raise ValueError(f"budget_yuan must be positive, got {budget_yuan}")
        if not 0 < alert_threshold <= 1:
            raise ValueError(
                f"alert_threshold must be in (0, 1], got {alert_threshold}"
            )
        self.budget_yuan = float(budget_yuan)
        self.alert_threshold = float(alert_threshold)
        self.input_price_per_million = float(input_price_per_million)
        self.output_price_per_million = float(output_price_per_million)
        self._records: list[CostRecord] = []

    @property
    def records(self) -> list[CostRecord]:
        """返回所有调用记录（副本）。"""
        return list(self._records)

    @property
    def total_prompt_tokens(self) -> int:
        """累计输入 token 数。"""
        return sum(record.prompt_tokens for record in self._records)

    @property
    def total_completion_tokens(self) -> int:
        """累计输出 token 数。"""
        return sum(record.completion_tokens for record in self._records)

    @property
    def total_tokens(self) -> int:
        """累计总 token 数。"""
        return sum(record.total_tokens for record in self._records)

    @property
    def total_cost_yuan(self) -> float:
        """累计成本（元）。"""
        return sum(record.cost_yuan for record in self._records)

    def record(
        self,
        node_name: str,
        usage: Usage | dict[str, Any],
        model: str = "",
    ) -> CostRecord:
        """记录一次 LLM 调用的 token 用量与成本。

        Args:
            node_name: 发起调用的节点名。
            usage: :class:`~workflows.model_client.Usage` 或等价的
                ``{"prompt_tokens": ..., "completion_tokens": ...}`` 字典。
            model: 模型标识，可选。

        Returns:
            本次调用生成的 :class:`CostRecord`。
        """
        if isinstance(usage, dict):
            usage = Usage.from_api(usage)
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        total = int(getattr(usage, "total_tokens", 0) or 0)
        if total <= 0:
            total = prompt + completion
        cost = (
            prompt * self.input_price_per_million
            + completion * self.output_price_per_million
        ) / PRICE_PER_MILLION
        record = CostRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            node_name=node_name,
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            model=model,
            cost_yuan=cost,
        )
        self._records.append(record)
        return record

    def _budget_status(self) -> dict[str, Any]:
        """汇总当前预算状态，不抛异常。"""
        used = self.total_cost_yuan
        ratio = used / self.budget_yuan
        if used > self.budget_yuan:
            status = "exceeded"
        elif ratio >= self.alert_threshold:
            status = "warning"
        else:
            status = "ok"
        return {
            "status": status,
            "budget_yuan": self.budget_yuan,
            "used_yuan": used,
            "remaining_yuan": self.budget_yuan - used,
            "ratio": ratio,
            "alert_threshold": self.alert_threshold,
            "calls": len(self._records),
            "total_tokens": self.total_tokens,
        }

    def check(self) -> dict[str, Any]:
        """检查预算状态。

        Returns:
            ``{"status": "ok"|"warning", ...}``。

        Raises:
            BudgetExceededError: 当累计成本超过 ``budget_yuan`` 时。
        """
        status = self._budget_status()
        if status["status"] == "exceeded":
            raise BudgetExceededError(status)
        return status

    def get_report(self) -> dict[str, Any]:
        """生成按节点分组的成本报告。"""
        by_node: dict[str, dict[str, Any]] = {}
        for record in self._records:
            bucket = by_node.setdefault(
                record.node_name,
                {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_yuan": 0.0,
                },
            )
            bucket["calls"] += 1
            bucket["prompt_tokens"] += record.prompt_tokens
            bucket["completion_tokens"] += record.completion_tokens
            bucket["total_tokens"] += record.total_tokens
            bucket["cost_yuan"] += record.cost_yuan

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "budget_yuan": self.budget_yuan,
            "alert_threshold": self.alert_threshold,
            "input_price_per_million": self.input_price_per_million,
            "output_price_per_million": self.output_price_per_million,
            "totals": {
                "calls": len(self._records),
                "prompt_tokens": self.total_prompt_tokens,
                "completion_tokens": self.total_completion_tokens,
                "total_tokens": self.total_tokens,
                "cost_yuan": self.total_cost_yuan,
            },
            "by_node": by_node,
            "records": [asdict(record) for record in self._records],
        }

    def save_report(self, path: str | Path | None = None) -> Path:
        """把成本报告写入 JSON 文件（UTF-8，不转义中文）。

        Args:
            path: 目标路径；默认写到仓库 ``data/cost_report.json``。

        Returns:
            实际写入的文件路径。
        """
        target = (
            Path(path)
            if path is not None
            else _REPO_ROOT / "data" / "cost_report.json"
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.get_report(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return target


def test_cost_tracking() -> None:
    """record 后累计 token 与成本应正确。"""
    guard = CostGuard(
        budget_yuan=1.0,
        alert_threshold=0.8,
        input_price_per_million=1.0,
        output_price_per_million=2.0,
    )
    guard.record(
        "analyzer",
        Usage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500),
        model="deepseek-chat",
    )
    guard.record(
        "organizer",
        {"prompt_tokens": 200, "completion_tokens": 100},
        model="deepseek-chat",
    )

    assert guard.total_prompt_tokens == 1200, guard.total_prompt_tokens
    assert guard.total_completion_tokens == 600, guard.total_completion_tokens
    assert guard.total_tokens == 1800, guard.total_tokens

    expected = (1200 * 1.0 + 600 * 2.0) / PRICE_PER_MILLION
    assert abs(guard.total_cost_yuan - expected) < 1e-12, guard.total_cost_yuan
    assert len(guard.records) == 2

    report = guard.get_report()
    assert report["by_node"]["analyzer"]["cost_yuan"] > 0
    assert report["by_node"]["organizer"]["prompt_tokens"] == 200
    assert report["totals"]["cost_yuan"] == guard.total_cost_yuan


def test_warning_threshold() -> None:
    """接近预算（>= alert_threshold）时 check 返回 warning。"""
    guard = CostGuard(budget_yuan=1.0, alert_threshold=0.8)
    guard.record("analyzer", Usage(prompt_tokens=850_000, completion_tokens=0))

    status = guard.check()
    assert status["status"] == "warning", status
    assert status["ratio"] >= 0.8


def test_budget_exceeded() -> None:
    """超出预算时 check 抛出 BudgetExceededError。"""
    guard = CostGuard(budget_yuan=0.001, alert_threshold=0.8)
    guard.record("analyzer", Usage(prompt_tokens=2_000, completion_tokens=0))

    try:
        guard.check()
    except BudgetExceededError as exc:
        assert exc.report["status"] == "exceeded"
    else:
        raise AssertionError("check() 未在超预算时抛出 BudgetExceededError")


def test_save_report() -> None:
    """save_report 应写出可解析的 JSON。"""
    guard = CostGuard(budget_yuan=5.0)
    guard.record("collector", Usage(prompt_tokens=100, completion_tokens=50))
    with tempfile.TemporaryDirectory() as tmp:
        path = guard.save_report(Path(tmp) / "report.json")
        data = json.loads(path.read_text(encoding="utf-8"))
    assert data["totals"]["calls"] == 1
    assert "collector" in data["by_node"]


def _main() -> None:
    tests = [
        test_cost_tracking,
        test_warning_threshold,
        test_budget_exceeded,
        test_save_report,
    ]
    for test in tests:
        test()
        print(f"[PASS] {test.__name__}")
    print(f"全部通过：{len(tests)}/{len(tests)}")


if __name__ == "__main__":
    _main()
