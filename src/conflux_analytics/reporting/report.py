"""Reproducible report generation (CSV, JSON and Markdown).

The report is written into ``settings.report_path`` with a run-specific prefix so
that two runs never overwrite each other, and every number in it comes from the
database via :mod:`conflux_analytics.reporting.dataset`.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from ..logging_setup import get_logger
from .dataset import AnalyticsDataset, load_dataset, quality_summary

logger = get_logger(__name__)

#: Columns written for each dataset, in report order. Only columns that exist in
#: the schema are listed; unknown columns are skipped rather than invented.
POOL_STATE_COLUMNS = (
    "pool_address",
    "dex_id",
    "pool_type",
    "block_number",
    "observed_at",
    "reserve0_raw",
    "reserve1_raw",
    "reserve0_dec",
    "reserve1_dec",
    "sqrt_price_x96",
    "tick",
    "liquidity",
    "fee_bps",
    "data_status",
    "source",
)

EXECUTION_COLUMNS = (
    "pool_address",
    "dex_id",
    "trade_size",
    "input_token",
    "output_token",
    "input_amount_raw",
    "input_amount_dec",
    "output_amount_raw",
    "output_amount_dec",
    "reference_price",
    "reference_price_method",
    "effective_price",
    "price_impact_bps",
    "total_deviation_bps",
    "fee_bps",
    "fee_amount_raw",
    "gas_units",
    "gas_price_wei",
    "gas_cost_native_wei",
    "gas_method",
    "gas_status",
    "quote_method",
    "block_number",
    "observed_at",
    "success",
    "failure_reason",
)

SWAP_COLUMNS = (
    "pool_address",
    "dex_id",
    "block_number",
    "block_timestamp",
    "transaction_hash",
    "log_index",
    "sender",
    "recipient",
    "amount0_raw",
    "amount1_raw",
    "sqrt_price_x96",
    "liquidity",
    "tick",
    "observed_at",
)

LIQUIDITY_EVENT_COLUMNS = (
    "pool_address",
    "dex_id",
    "event_type",
    "block_number",
    "transaction_hash",
    "log_index",
    "owner",
    "amount0_raw",
    "amount1_raw",
    "amount_raw",
    "liquidity",
    "tick_lower",
    "tick_upper",
    "observed_at",
)

RUN_COLUMNS = (
    "run_id",
    "status",
    "trigger",
    "started_at",
    "completed_at",
    "start_block",
    "end_block",
    "blocks_processed",
    "markets_processed",
    "observations_created",
    "errors",
    "error_summary",
)


def _write_csv(path: Path, rows: list[dict[str, Any]], columns: tuple[str, ...]) -> int:
    """Write ``rows`` to ``path``; returns the number of data rows written."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in columns})
    return len(rows)


def write_report(settings: Settings, *, limit: int = 20_000) -> dict[str, Any]:
    """Generate CSV, JSON and Markdown reports from stored observations."""
    dataset = load_dataset(settings, limit=limit)
    report_dir = settings.report_path
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")

    written: dict[str, str] = {}
    counts: dict[str, int] = {}
    files = (
        ("markets.csv", dataset.pool_states, POOL_STATE_COLUMNS, "pool_states"),
        ("execution.csv", dataset.execution, EXECUTION_COLUMNS, "execution"),
        ("swaps.csv", dataset.swaps, SWAP_COLUMNS, "swaps"),
        ("liquidity_events.csv", dataset.liquidity_events, LIQUIDITY_EVENT_COLUMNS, "liquidity"),
        ("runs.csv", dataset.runs, RUN_COLUMNS, "runs"),
    )
    for filename, rows, columns, key in files:
        path = report_dir / f"{stamp}-{filename}"
        counts[key] = _write_csv(path, rows, columns)
        written[key] = str(path)

    summary = _summary(dataset)
    json_path = report_dir / f"{stamp}-summary.json"
    json_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    written["summary_json"] = str(json_path)

    markdown_path = report_dir / f"{stamp}-report.md"
    markdown_path.write_text(render_markdown(summary, dataset), encoding="utf-8")
    written["markdown"] = str(markdown_path)

    logger.info(
        "report written",
        extra={"event": "report.written", "files": len(written), "report_dir": str(report_dir)},
    )
    return {"written": written, "counts": counts, "summary": summary}


def _summary(dataset: AnalyticsDataset) -> dict[str, Any]:
    """Aggregate statistics; every figure is a direct count over stored rows."""
    quality = quality_summary(dataset)
    first_seen, last_seen = dataset.observed_at_range
    first_block, last_block = dataset.block_range
    latest_run = dataset.runs[0] if dataset.runs else None
    impacts = sorted(
        float(row["price_impact_bps"])
        for row in dataset.execution
        if row.get("price_impact_bps") is not None
    )
    gas_costs = sorted(
        int(row["gas_cost_native_wei"])
        for row in dataset.execution
        if row.get("gas_cost_native_wei") is not None
    )
    return {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": "Conflux Liquidity & Execution Analytics",
        "chain_id": dataset.chain_id,
        "network_name": dataset.settings.network.name,
        "rpc_url": dataset.settings.network.rpc_url,
        "explorer_url": dataset.settings.network.explorer_url,
        "database_path": str(dataset.settings.db_path),
        "collection_period": {"first_observed_at": first_seen, "last_observed_at": last_seen},
        "block_range": {"first_block": first_block, "last_block": last_block},
        "latest_run": latest_run,
        "dexes": [
            {
                "dex_id": row.get("dex_id"),
                "name": row.get("name"),
                "pool_type": row.get("pool_type"),
                "enabled": row.get("enabled"),
                "documentation": row.get("documentation"),
            }
            for row in dataset.dexes
        ],
        "market_count": len(dataset.pools),
        "markets": [
            {
                "pool_address": row.get("pool_address"),
                "dex_id": row.get("dex_id"),
                "pool_type": row.get("pool_type"),
                "token0_symbol": row.get("token0_symbol"),
                "token1_symbol": row.get("token1_symbol"),
                "fee_bps": row.get("fee_bps"),
                "discovery_source": row.get("discovery_source"),
                "verified": row.get("verified"),
                "active": row.get("active"),
            }
            for row in dataset.pools
        ],
        "data_quality": quality,
        "execution": {
            "price_impact_bps_min": impacts[0] if impacts else None,
            "price_impact_bps_median": impacts[len(impacts) // 2] if impacts else None,
            "price_impact_bps_max": impacts[-1] if impacts else None,
        },
        "gas": {
            "observations_with_gas_cost": len(gas_costs),
            "gas_cost_native_wei_min": gas_costs[0] if gas_costs else None,
            "gas_cost_native_wei_max": gas_costs[-1] if gas_costs else None,
            "note": "Gas values are eth_estimateGas estimates, not actual receipts.",
        },
        "limitations": [
            "USD liquidity is not reported unless an explicit price source is configured.",
            "Price impact is reported only where a reference price was computable at the same block.",
            "Gas figures are estimates; actual gas is stored separately from receipts.",
            "Historical coverage is limited to the collected block range and the RPC log limits.",
        ],
    }



def render_markdown(summary: dict[str, Any], dataset: AnalyticsDataset) -> str:
    """Render the summary as a Markdown report."""
    period = summary["collection_period"]
    blocks = summary["block_range"]
    quality = summary["data_quality"]
    lines: list[str] = [
        "# Conflux Liquidity & Execution Analytics - analytical report",
        "",
        f"Generated (UTC): `{summary['generated_at']}`",
        "",
        "## Environment",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Network | {summary['network_name']} |",
        f"| Chain ID | {summary['chain_id']} |",
        f"| RPC URL | `{summary['rpc_url']}` |",
        f"| Explorer | {summary['explorer_url']} |",
        f"| Database | `{summary['database_path']}` |",
        "",
        "## Collection period",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| First observation (UTC) | {period['first_observed_at']} |",
        f"| Last observation (UTC) | {period['last_observed_at']} |",
        f"| First block | {blocks['first_block']} |",
        f"| Last block | {blocks['last_block']} |",
        f"| Runs recorded | {len(dataset.runs)} |",
        "",
        "## DEXs covered",
        "",
        "| dex_id | name | pool type | enabled |",
        "| --- | --- | --- | --- |",
    ]
    for dex in summary["dexes"]:
        lines.append(
            f"| {dex['dex_id']} | {dex['name']} | {dex['pool_type']} | {dex['enabled']} |"
        )
    if not summary["dexes"]:
        lines.append("| (none) | | | |")

    lines += [
        "",
        "## Markets covered",
        "",
        "| pool | dex | pool type | pair | fee bps | discovery source | verified |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for market in summary["markets"]:
        pair = f"{market['token0_symbol'] or '?'}/{market['token1_symbol'] or '?'}"
        lines.append(
            f"| `{market['pool_address']}` | {market['dex_id']} | {market['pool_type']} | "
            f"{pair} | {market['fee_bps']} | {market['discovery_source']} | "
            f"{market['verified']} |"
        )
    if not summary["markets"]:
        lines.append("| (none) | | | | | | |")

    lines += [
        "",
        "## Observations collected",
        "",
        "| Dataset | Rows |",
        "| --- | --- |",
        f"| Pool states | {quality['pool_state_observations']} |",
        f"| Execution observations | {quality['execution_observations']} |",
        f"| Swap events | {quality['swap_events']} |",
        f"| Liquidity events | {quality['liquidity_events']} |",
        "",
        "## Data quality",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Successful executions | {quality['execution_successes']} |",
        f"| Execution success rate | {quality['execution_success_rate']} |",
        f"| Observations with price impact | {quality['price_impact_available']} |",
        f"| Observations with gas cost | {quality['gas_cost_available']} |",
        f"| Observations with fee information | {quality['fee_available']} |",
        f"| Pool-state data status counts | {quality['pool_state_data_status']} |",
        f"| Quote methods used | {quality['quote_methods']} |",
        "",
        "## Execution results",
        "",
        "| Metric | bps |",
        "| --- | --- |",
        f"| Minimum price impact | {summary['execution']['price_impact_bps_min']} |",
        f"| Median price impact | {summary['execution']['price_impact_bps_median']} |",
        f"| Maximum price impact | {summary['execution']['price_impact_bps_max']} |",
        "",
        "Price impact excludes protocol fees; fees are reported per observation.",
        "",
        "## Gas estimates",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| Observations with gas cost | {summary['gas']['observations_with_gas_cost']} |",
        f"| Minimum estimated cost (wei) | {summary['gas']['gas_cost_native_wei_min']} |",
        f"| Maximum estimated cost (wei) | {summary['gas']['gas_cost_native_wei_max']} |",
        "",
        f"{summary['gas']['note']}",
        "",
        "## Known limitations",
        "",
    ]
    lines += [f"- {item}" for item in summary["limitations"]]
    lines += [
        "",
        "## Reproduction",
        "",
        "```bash",
        "conflux-analytics collect --start-block <first_block> --end-block <last_block>",
        "conflux-analytics report",
        "```",
        "",
        "All values above were read from the SQLite database written by the collector; "
        "none were entered manually.",
        "",
    ]
    return "\n".join(lines)


__all__ = ["write_report"]
