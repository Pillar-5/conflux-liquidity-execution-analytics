"""Reporting: datasets, reproducible reports and benchmarks.

``python -m conflux_analytics.reporting`` is a documented entry point that
writes CSV, JSON and Markdown reports from the SQLite database.
"""

from __future__ import annotations

import sys

from .benchmark import BenchmarkResult, run_benchmark
from .dataset import AnalyticsDataset, load_dataset, quality_summary
from .report import render_markdown, write_report

__all__ = [
    "AnalyticsDataset",
    "BenchmarkResult",
    "load_dataset",
    "quality_summary",
    "render_markdown",
    "run_benchmark",
    "write_report",
]


def main(argv: list[str] | None = None) -> int:
    """Module entry point: print the reproduction paths of a fresh report."""
    from ..cli import main as cli_main

    return cli_main(["report", *(argv or [])])


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI tests
    sys.exit(main(sys.argv[1:]))
