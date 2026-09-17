"""Reproducible benchmark over real Conflux eSpace data.

The benchmark performs an actual collection run against the configured RPC and
reports measured numbers only. Nothing here is derived from stored history: a
benchmark that cannot reach the network reports the failure instead of a number.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings
from ..logging_setup import get_logger
from ..rpc.provider import RpcProvider
from .dataset import load_dataset

logger = get_logger(__name__)


@dataclass
class BenchmarkResult:
    """Measured outcome of one benchmark execution."""

    chain_id: int
    rpc_url: str
    latest_block: int | None = None
    dexes_tested: list[str] = field(default_factory=list)
    markets_discovered: int = 0
    markets_collected: int = 0
    markets_analysed: int = 0
    pool_state_reads: int = 0
    quotes_attempted: int = 0
    quotes_succeeded: int = 0
    quotes_failed: int = 0
    rpc_errors: int = 0
    collection_seconds: float | None = None
    average_rpc_latency_ms: float | None = None
    data_age_seconds: float | None = None
    execution_analysis_success_rate: float | None = None
    run_status: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def quote_success_rate(self) -> float | None:
        if not self.quotes_attempted:
            return None
        return self.quotes_succeeded / self.quotes_attempted

    def render(self) -> str:
        """The benchmark block that is also written to ``reports/``."""

        def show(value: Any) -> str:
            return "unavailable" if value is None else str(value)

        lines = [
            "## Conflux eSpace Analytics Benchmark",
            "",
            f"Chain ID: {self.chain_id}",
            f"RPC URL: {self.rpc_url}",
            f"Latest block: {show(self.latest_block)}",
            f"DEXs tested: {', '.join(self.dexes_tested) if self.dexes_tested else 'none'}",
            f"Markets discovered: {self.markets_discovered}",
            f"Markets successfully collected: {self.markets_collected}",
            f"Markets analysed: {self.markets_analysed}",
            f"Pool-state reads: {self.pool_state_reads}",
            f"Quotes attempted: {self.quotes_attempted}",
            f"Successful observations: {self.quotes_succeeded}",
            f"Failed observations: {self.quotes_failed}",
            f"Quote success rate: {show(self.quote_success_rate)}",
            f"RPC errors: {self.rpc_errors}",
            f"Average RPC latency: {show(self.average_rpc_latency_ms)} ms",
            f"Collection duration: {show(self.collection_seconds)} s",
            f"Data age: {show(self.data_age_seconds)} s",
            f"Execution-analysis success rate: {show(self.execution_analysis_success_rate)}",
            f"Run status: {show(self.run_status)}",
        ]
        if self.notes:
            lines.append("")
            lines.append("Notes:")
            lines.extend(f"- {note}" for note in self.notes)
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "rpc_url": self.rpc_url,
            "latest_block": self.latest_block,
            "dexes_tested": self.dexes_tested,
            "markets_discovered": self.markets_discovered,
            "markets_collected": self.markets_collected,
            "markets_analysed": self.markets_analysed,
            "pool_state_reads": self.pool_state_reads,
            "quotes_attempted": self.quotes_attempted,
            "quotes_succeeded": self.quotes_succeeded,
            "quotes_failed": self.quotes_failed,
            "quote_success_rate": self.quote_success_rate,
            "rpc_errors": self.rpc_errors,
            "average_rpc_latency_ms": self.average_rpc_latency_ms,
            "collection_seconds": self.collection_seconds,
            "data_age_seconds": self.data_age_seconds,
            "execution_analysis_success_rate": self.execution_analysis_success_rate,
            "run_status": self.run_status,
            "notes": self.notes,
        }


def run_benchmark(
    settings: Settings,
    provider: RpcProvider,
    *,
    dex_ids: list[str] | None = None,
    start_block: int | None = None,
    end_block: int | None = None,
) -> BenchmarkResult:
    """Run a real collection and measure it.

    ``provider`` is passed in so the caller owns the connection; the benchmark
    only measures. A run that cannot reach the network is reported as a failure
    rather than replaced with a stored or invented number.
    """
    from ..models.common import seconds_since
    from ..pipeline import AnalyticsPipeline

    result = BenchmarkResult(chain_id=settings.network.chain_id, rpc_url=provider.rpc_url)
    pipeline = AnalyticsPipeline(settings, provider)
    try:
        started = time.perf_counter()
        try:
            outcome = pipeline.run_collection(
                dex_ids=dex_ids,
                start_block=start_block,
                end_block=end_block,
                trigger="benchmark",
            )
        except Exception as exc:  # noqa: BLE001 - the benchmark reports its own failure
            result.notes.append(f"collection run failed: {exc}")
            logger.error("benchmark run failed", extra={"event": "benchmark.failed"})
            return result
        result.collection_seconds = round(time.perf_counter() - started, 3)
        result.run_status = outcome.status
        result.latest_block = outcome.end_block
        result.markets_discovered = sum(
            int(summary.get("pools_loaded", 0)) for summary in outcome.dexes.values()
        )
        result.markets_collected = outcome.markets_processed
        result.pool_state_reads = outcome.states_collected
        # Quote counts are read back from the database so the benchmark measures
        # stored observations rather than in-memory counters.
        rows = pipeline.observations.execution_observations(
            run_id=outcome.run_id, limit=100_000
        )
        result.quotes_attempted = len(rows)
        result.quotes_succeeded = sum(1 for row in rows if row.get("success"))
        result.quotes_failed = result.quotes_attempted - result.quotes_succeeded
        result.markets_analysed = len({row["pool_address"] for row in rows})
        result.dexes_tested = sorted(outcome.dexes)
        if result.quotes_attempted:
            result.execution_analysis_success_rate = round(
                result.quotes_succeeded / result.quotes_attempted, 6
            )
        if outcome.errors:
            result.notes.append(f"{len(outcome.errors)} error(s) recorded during the run")
        if result.quotes_attempted and not result.quotes_succeeded:
            result.notes.append(
                "no quote succeeded: see the per-observation failure_reason for this run"
            )

        health = pipeline.check_network()
        result.average_rpc_latency_ms = health.get("latency_ms")
        if health.get("latest_block") is not None:
            result.latest_block = health["latest_block"]
    finally:
        pipeline.close()
    result.rpc_errors = provider.rpc_error_count

    dataset = load_dataset(settings, limit=1)
    _, last = dataset.observed_at_range
    result.data_age_seconds = seconds_since(last)
    return result


__all__ = ["BenchmarkResult", "run_benchmark"]