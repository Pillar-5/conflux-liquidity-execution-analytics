"""Server-rendered analytics dashboard.

The dashboard is deliberately dependency-free: plain HTML built from the same
repositories the REST API uses. It never calls the chain directly, so what it
displays is exactly what was collected and stored - including data status and
freshness for every value shown.
"""

from __future__ import annotations

import html
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..config import Settings
from .app import ApiContext, freshness

_BASE_STYLE = """\
<style>
 body { font-family: "Segoe UI", Arial, sans-serif; margin: 0; background: #f5f7fa; color: #1c2733; }
 header { background: #14212e; color: #fff; padding: 14px 24px; }
 header h1 { margin: 0; font-size: 18px; font-weight: 600; }
 header .sub { color: #9fb3c8; font-size: 12px; }
 main { padding: 20px 24px; max-width: 1200px; margin: 0 auto; }
 .cards { display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 18px; }
 .card { background: #fff; border: 1px solid #dbe2ea; border-radius: 6px; padding: 12px 16px; min-width: 150px; }
 .card .label { color: #64748b; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
 .card .value { font-size: 16px; font-weight: 600; margin-top: 4px; }
 table { border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #dbe2ea; }
 th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef2f6; font-size: 13px; }
 th { background: #f0f4f8; color: #475569; font-size: 11px; text-transform: uppercase; }
 code, .mono { font-family: Consolas, monospace; font-size: 12px; }
 .badge { padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
 .fresh { background: #dcfce7; color: #166534; }
 .stale { background: #fef3c7; color: #92400e; }
 .unavailable { background: #e2e8f0; color: #475569; }
 .verified { background: #dbeafe; color: #1e40af; }
 .error { background: #fee2e2; color: #991b1b; }
 a { color: #2563eb; text-decoration: none; }
 h2 { font-size: 15px; margin: 22px 0 8px; }
 .muted { color: #64748b; font-size: 12px; }
</style>
"""


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else "—"


def _page(title: str, body: str) -> str:
    return (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        f"<title>{_esc(title)}</title>{_BASE_STYLE}</head><body>"
        "<header><h1>Conflux Liquidity &amp; Execution Analytics</h1>"
        "<div class='sub'>Read-only analytics over collected Conflux eSpace market data</div></header>"
        f"<main>{body}</main></body></html>"
    )


def _badge(status: str | None) -> str:
    css = (status or "unavailable").replace(" ", "")
    return f"<span class='badge {css}'>{_esc(status or 'unavailable')}</span>"


def _short(address: str | None) -> str:
    if not address:
        return "—"
    return f"{address[:10]}…{address[-6:]}"


def _cards(items: list[tuple[str, Any, bool]]) -> str:
    """Render summary cards. ``items`` are ``(label, value, value_is_status)``."""
    out = ["<div class='cards'>"]
    for label, value, is_status in items:
        rendered = _badge(value) if is_status else _esc(value)
        out.append(
            f"<div class='card'><div class='label'>{_esc(label)}</div>"
            f"<div class='value'>{rendered}</div></div>"
        )
    out.append("</div>")
    return "".join(out)


def _market_link(pool: dict[str, Any]) -> str:
    return f"market/{pool['pool_address']}"


def _liquidity_summary(state: dict[str, Any] | None, pool: dict[str, Any]) -> str:
    """Token-denominated liquidity, which needs no external price source."""
    if not state:
        return "unavailable"
    parts: list[str] = []
    if state.get("reserve0_dec") is not None:
        parts.append(f"{state['reserve0_dec']} {pool.get('token0_symbol') or 'token0'}")
    if state.get("reserve1_dec") is not None:
        parts.append(f"{state['reserve1_dec']} {pool.get('token1_symbol') or 'token1'}")
    if state.get("sqrt_price_x96") is not None:
        parts.append(f"sqrtPriceX96={state['sqrt_price_x96']}")
    if state.get("liquidity") is not None:
        parts.append(f"L={state['liquidity']}")
    return " · ".join(parts) if parts else "unavailable"


def _market_rows(
    ctx: ApiContext, chain_id: int, explorer: str
) -> tuple[list[str], int, int]:
    """Market table rows plus ``(with_state, with_execution)`` counters."""
    latest = ctx.runs.latest_run()
    run_id = latest.run_id if latest else None
    pools = ctx.catalog.list_pools(chain_id=chain_id, active_only=True)
    rows: list[str] = []
    with_state = 0
    with_execution = 0
    for pool in pools:
        address = pool["pool_address"]
        state = ctx.observations.latest_pool_state(address, run_id=run_id)
        if state:
            with_state += 1
        executions = ctx.observations.execution_observations(
            pool_address=address, run_id=run_id, limit=50
        )
        if any(row.get("success") for row in executions):
            with_execution += 1
        fresh = freshness(state.get("observed_at")) if state else freshness(None)
        status = (state or {}).get("data_status") or "unavailable"
        if state and fresh["status"] == "stale":
            status = "stale"
        symbol0 = pool.get("token0_symbol") or _short(pool.get("token0_address"))
        symbol1 = pool.get("token1_symbol") or _short(pool.get("token1_address"))
        rows.append(
            "<tr>"
            f"<td>{_esc(pool.get('dex_id'))}</td>"
            f"<td><a class='mono' href='{explorer}/address/{address}' target='_blank' "
            f"rel='noopener'>{_short(address)}</a></td>"
            f"<td>{_esc(symbol0)} / {_esc(symbol1)}</td>"
            f"<td>{_esc(pool.get('pool_type'))}</td>"
            f"<td class='mono'>{_esc(_liquidity_summary(state, pool))}</td>"
            f"<td>{_esc(pool.get('fee_bps'))}</td>"
            f"<td>{_esc((state or {}).get('block_number'))}</td>"
            f"<td>{_esc((state or {}).get('observed_at'))}</td>"
            f"<td>{_badge(status)}</td>"
            f"<td><a href='{_market_link(pool)}'>open</a></td>"
            "</tr>"
        )
    return rows, with_state, with_execution



def mount_dashboard(app: FastAPI, settings: Settings, ctx: ApiContext) -> None:
    """Attach the dashboard routes to ``app``."""
    explorer = settings.network.explorer_url.rstrip("/")
    chain_id = settings.network.chain_id
    base = (settings.dashboard.mount_path or "/dashboard").rstrip("/")

    def _run_section() -> str:
        latest = ctx.runs.latest_run()
        if not latest:
            return (
                "<h2>Latest collection run</h2><p class='muted'>No collection run has been "
                "recorded yet. Run <code>conflux-analytics collect</code> to populate the "
                "database.</p>"
            )
        record = latest.as_dict()
        rows = [
            ("run_id", record.get("run_id")),
            ("status", record.get("status")),
            ("started_at (UTC)", record.get("started_at")),
            ("completed_at (UTC)", record.get("completed_at")),
            ("blocks_processed", record.get("blocks_processed")),
            ("markets_processed", record.get("markets_processed")),
            ("observations_created", record.get("observations_created")),
            ("error_summary", record.get("error_summary") or "none"),
        ]
        body = "".join(
            f"<tr><th>{_esc(k)}</th><td class='mono'>{_esc(v)}</td></tr>" for k, v in rows
        )
        return f"<h2>Latest collection run</h2><table>{body}</table>"

    def _dex_section() -> str:
        dexes = ctx.catalog.list_dexes(chain_id=chain_id)
        if not dexes:
            return (
                "<h2>DEXs</h2><p class='muted'>No DEX has been registered yet. Run "
                "<code>conflux-analytics discover</code>.</p>"
            )
        head = (
            "<tr><th>dex_id</th><th>name</th><th>pool type</th><th>pools</th>"
            "<th>enabled</th><th>capabilities</th><th>documentation</th></tr>"
        )
        body = []
        for dex in dexes:
            pools = ctx.catalog.list_pools(chain_id=chain_id, dex_id=dex["dex_id"])
            caps = dex.get("capabilities_json")
            body.append(
                "<tr>"
                f"<td>{_esc(dex.get('dex_id'))}</td>"
                f"<td>{_esc(dex.get('name'))}</td>"
                f"<td>{_esc(dex.get('pool_type'))}</td>"
                f"<td>{_esc(len(pools))}</td>"
                f"<td>{_badge('ok' if dex.get('enabled') else 'disabled')}</td>"
                f"<td class='mono muted'>{_esc(caps)}</td>"
                f"<td>{_esc(dex.get('documentation') or '—')}</td>"
                "</tr>"
            )
        return f"<h2>DEXs</h2><table>{head}{''.join(body)}</table>"

    @app.get(base, response_class=HTMLResponse, include_in_schema=False)
    def dashboard_index() -> HTMLResponse:
        try:
            health = ctx.provider.health().as_dict()
        except Exception as exc:  # noqa: BLE001 - a dashboard must render even when RPC is down
            health = {
                "status": "unreachable",
                "reachable": False,
                "detail": str(exc),
                "latest_block": None,
                "latency_ms": None,
            }
        rows, with_state, with_execution = _market_rows(ctx, chain_id, explorer)
        latest = ctx.runs.latest_run()
        tokens = ctx.catalog.list_tokens(chain_id=chain_id)
        dexes = ctx.catalog.list_dexes(chain_id=chain_id)
        cards = _cards(
            [
                ("Network", f"{settings.network.name} (chain {chain_id})", False),
                ("RPC status", health.get("status"), True),
                ("Latest block", health.get("latest_block"), False),
                ("RPC latency (ms)", health.get("latency_ms"), False),
                ("DEX adapters registered", len(dexes), False),
                ("Discovered markets", len(rows), False),
                ("Markets with state", with_state, False),
                ("Markets with quotes", with_execution, False),
                ("Known tokens", len(tokens), False),
                ("Latest run status", (latest.status if latest else "none"), False),
            ]
        )
        market_head = (
            "<tr><th>DEX</th><th>Pool</th><th>Pair</th><th>Pool type</th>"
            "<th>Liquidity (token-denominated)</th><th>Fee bps</th><th>Block</th>"
            "<th>Observed at (UTC)</th><th>Data status</th><th></th></tr>"
        )
        market_table = (
            f"<table>{market_head}{''.join(rows)}</table>"
            if rows
            else "<p class='muted'>No markets discovered yet.</p>"
        )
        body = (
            f"{cards}"
            f"<p class='muted'>RPC <code>{_esc(settings.network.rpc_url)}</code> · "
            f"current block <span class='mono'>{_esc(health.get('latest_block'))}</span> · "
            f"<a href='{base}/execution'>execution &amp; history</a> · "
            f"<a href='/docs'>OpenAPI</a> · "
            f"<a href='/api/markets'>raw market JSON</a></p>"
            f"{_run_section()}"
            f"<h2>Markets</h2>{market_table}"
            f"{_dex_section()}"
            "<h2>How to read this page</h2>"
            "<p class='muted'>Every number originates from a stored observation. "
            "<span class='badge verified'>verified</span> was read from the chain, "
            "<span class='badge fresh'>fresh</span> is a verified value inside the "
            "freshness window, <span class='badge stale'>stale</span> is a real value "
            "older than the freshness window, and "
            "<span class='badge unavailable'>unavailable</span> means the source data "
            "does not exist - nothing is estimated in its place. Liquidity is shown in "
            "token units because no price source is configured by default; USD values "
            "are only derived when an explicit price source is configured.</p>"
        )
        return HTMLResponse(_page("Dashboard", body))
    @app.get(
        base + "/market/{pool_address}", response_class=HTMLResponse, include_in_schema=False
    )
    def dashboard_market(pool_address: str) -> HTMLResponse:
        pool = ctx.catalog.get_pool(pool_address)
        if not pool:
            raise HTTPException(status_code=404, detail="pool not found in catalog")
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        state = ctx.observations.latest_pool_state(pool_address, run_id=run_id)
        executions = ctx.observations.execution_observations(
            pool_address=pool_address, run_id=run_id, limit=500
        )
        history = ctx.observations.execution_observations(pool_address=pool_address, limit=2000)
        swaps = ctx.observations.swap_events(pool_address=pool_address, limit=50)
        fresh = freshness(state.get("observed_at")) if state else freshness(None)
        ok_count = sum(1 for row in executions if row.get("success"))

        token_links = []
        for key in ("token0_address", "token1_address"):
            address = pool.get(key)
            if address:
                token_links.append(
                    f"<a class='mono' href='{explorer}/token/{address}' target='_blank' "
                    f"rel='noopener'>{_short(address)}</a>"
                )
        swap_head = (
            "<tr><th>Block</th><th>Tx</th><th>amount0 raw</th><th>amount1 raw</th>"
            "<th>Log index</th><th>Observed at (UTC)</th></tr>"
        )
        swap_body = "".join(
            "<tr>"
            f"<td>{_esc(row.get('block_number'))}</td>"
            f"<td><a class='mono' href='{explorer}/tx/{row.get('transaction_hash')}' "
            f"target='_blank' rel='noopener'>{_short(row.get('transaction_hash'))}</a></td>"
            f"<td class='mono'>{_esc(row.get('amount0_raw'))}</td>"
            f"<td class='mono'>{_esc(row.get('amount1_raw'))}</td>"
            f"<td>{_esc(row.get('log_index'))}</td>"
            f"<td>{_esc(row.get('observed_at'))}</td>"
            "</tr>"
            for row in swaps
        )
        swap_table = (
            "<table>" + swap_head + swap_body + "</table>"
            if swaps
            else "<p class='muted'>No swap events collected for this pool yet.</p>"
        )
        body = "".join([
            _cards(
                [
                    ("Pair", f"{pool.get('token0_symbol') or '?'} / "
                             f"{pool.get('token1_symbol') or '?'}", False),
                    ("Pool type", pool.get("pool_type"), False),
                    ("Fee bps", pool.get("fee_bps"), False),
                    ("Block", (state or {}).get("block_number"), False),
                    ("Data status", (state or {}).get("data_status") or "unavailable", True),
                    ("Freshness", fresh["status"], True),
                    ("Data age (s)", fresh["data_age_seconds"], False),
                    ("Successful quotes", f"{ok_count} / {len(executions)}", False),
                ]
            ),
            f"<p><a href='{base}'>&larr; all markets</a> &middot; pool "
            f"<a class='mono' href='{explorer}/address/{pool_address}' target='_blank' "
            f"rel='noopener'>{pool_address}</a></p>",
            "<h2>Tokens</h2>",
            f"<p>{' &middot; '.join(token_links) if token_links else 'unavailable'}</p>",
            "<h2>Liquidity</h2>",
            f"<p class='mono'>{_esc(_liquidity_summary(state, pool))}</p>",
            "<h2>Execution observations (latest run)</h2>",
            f"{_execution_table(executions)}",
            "<h2>Historical execution observations (plotted)</h2>",
            f"{_sparkline(history)}",
            f"{_execution_table(history[:200])}",
            "<h2>Recent swap events</h2>",
            f"{swap_table}",
            "<h2>Provenance</h2>",
            f"{_provenance_table(state, pool)}",
        ])
        return HTMLResponse(_page(f"Market {_short(pool_address)}", body))

    @app.get(base + "/execution", response_class=HTMLResponse, include_in_schema=False)
    def dashboard_execution() -> HTMLResponse:
        latest = ctx.runs.latest_run()
        run_id = latest.run_id if latest else None
        latest_rows = (
            ctx.observations.execution_observations(run_id=run_id, limit=5000) if run_id else []
        )
        history_rows = ctx.observations.execution_observations(limit=5000)
        successes = [row for row in latest_rows if row.get("success")]

        # Trade-size comparison: the best (lowest) price impact observed per size.
        comparison: dict[str, list[dict[str, Any]]] = {}
        for row in successes:
            comparison.setdefault(str(row.get("trade_size")), []).append(row)
        comparison_head = (
            "<tr><th>Trade size</th><th>Markets compared</th><th>Best market</th>"
            "<th>Best impact (bps)</th><th>Median impact (bps)</th></tr>"
        )
        comparison_body = []
        for size in sorted(comparison):
            candidates = comparison[size]
            scored = [
                row
                for row in candidates
                if row.get("price_impact_bps") is not None
            ]
            if not scored:
                comparison_body.append(
                    f"<tr><td>{_esc(size)}</td><td>{len(candidates)}</td>"
                    "<td colspan='3' class='muted'>no comparable impact (reference price "
                    "unavailable)</td></tr>"
                )
                continue
            ranked = sorted(scored, key=lambda row: float(row["price_impact_bps"]))
            impacts = sorted(float(row["price_impact_bps"]) for row in scored)
            median = impacts[len(impacts) // 2]
            best = ranked[0]
            comparison_body.append(
                "<tr>"
                f"<td>{_esc(size)}</td>"
                f"<td>{len(candidates)}</td>"
                f"<td class='mono'>{_esc(best.get('dex_id'))} "
                f"{_short(best.get('pool_address'))}</td>"
                f"<td>{_esc(best.get('price_impact_bps'))}</td>"
                f"<td>{_esc(round(median, 6))}</td>"
                "</tr>"
            )
        comparison_table = (
            f"<table>{comparison_head}{''.join(comparison_body)}</table>"
            if comparison_body
            else "<p class='muted'>No successful execution observations yet.</p>"
        )

        body = "".join([
            _cards(
                [
                    ("Latest run", run_id or "none", False),
                    ("Observations in latest run", len(latest_rows), False),
                    (
                        "Success rate (latest run)",
                        f"{(len(successes) / len(latest_rows)):.2%}" if latest_rows else "n/a",
                        False,
                    ),
                    ("Historical observations", len(history_rows), False),
                    (
                        "Markets compared",
                        len({row.get("pool_address") for row in successes}),
                        False,
                    ),
                ]
            ),
            f"<p><a href='{base}'>&larr; dashboard</a></p>",
            "<h2>Trade-size comparison across markets</h2>",
            "<p class='muted'>Price impact excludes protocol fees by methodology; fees are "
            "reported separately per observation. Only observations whose source data "
            "exists are included - no result is estimated or extrapolated.</p>",
            f"{comparison_table}",
            "<h2>Price impact history</h2>",
            f"{_sparkline(history_rows)}",
            "<h2>Latest run observations</h2>",
            f"{_execution_table(latest_rows)}",
        ])
        return HTMLResponse(_page("Execution analytics", body))




def _execution_table(rows: list[dict[str, Any]]) -> str:
    """Execution observations for one market, with method and status visible."""
    if not rows:
        return "<p class='muted'>No execution observations recorded for this selection.</p>"
    head = (
        "<tr><th>Trade size</th><th>Direction</th><th>Input</th><th>Output</th>"
        "<th>Reference</th><th>Effective</th><th>Impact (bps)</th><th>Fee bps</th>"
        "<th>Gas units</th><th>Gas cost (wei)</th><th>Quote method</th><th>Status</th></tr>"
    )
    body = []
    for row in rows:
        direction = f"{_short(row.get('input_token'))} → {_short(row.get('output_token'))}"
        status = "ok" if row.get("success") else (row.get("failure_reason") or "failed")
        body.append(
            "<tr>"
            f"<td>{_esc(row.get('trade_size'))}</td>"
            f"<td class='mono'>{direction}</td>"
            f"<td>{_esc(row.get('input_amount_dec'))}</td>"
            f"<td>{_esc(row.get('output_amount_dec'))}</td>"
            f"<td>{_esc(row.get('reference_price'))}</td>"
            f"<td>{_esc(row.get('effective_price'))}</td>"
            f"<td>{_esc(row.get('price_impact_bps'))}</td>"
            f"<td>{_esc(row.get('fee_bps'))}</td>"
            f"<td>{_esc(row.get('gas_units'))}</td>"
            f"<td class='mono'>{_esc(row.get('gas_cost_native_wei'))}</td>"
            f"<td>{_esc(row.get('quote_method'))}</td>"
            f"<td>{_badge(status)}</td>"
            "</tr>"
        )
    return "<table>" + head + "".join(body) + "</table>"


def _sparkline(rows: list[dict[str, Any]], width: int = 640, height: int = 130) -> str:
    """Trade-size comparison chart: price impact against block number.

    Only observations that carry a numeric impact are plotted. Nothing is
    interpolated and no series is invented for a missing trade size.
    """
    series: dict[str, list[tuple[int, float]]] = {}
    for row in rows:
        impact = row.get("price_impact_bps")
        block = row.get("block_number")
        if impact is None or block is None:
            continue
        try:
            series.setdefault(str(row.get("trade_size")), []).append((int(block), float(impact)))
        except (TypeError, ValueError):
            continue
    usable = {label: sorted(points) for label, points in series.items() if points}
    if not usable:
        return (
            "<p class='muted'>No plotted execution history yet: impacts require a "
            "reference price at the same block.</p>"
        )

    blocks = [b for points in usable.values() for b, _ in points]
    impacts = [i for points in usable.values() for _, i in points]
    min_b, max_b = min(blocks), max(blocks)
    min_i, max_i = min(impacts), max(impacts)
    span_b = max(max_b - min_b, 1)
    span_i = max(max_i - min_i, 1e-12)
    palette = ["#2563eb", "#0891b2", "#ea580c", "#7c3aed", "#16a34a"]

    parts = [
        f"<svg width='{width}' height='{height}' role='img' "
        "aria-label='price impact by block'>",
        f"<rect x='0' y='0' width='{width}' height='{height}' fill='#fff' stroke='#dbe2ea'/>",
    ]
    legend: list[str] = []
    for index, (label, points) in enumerate(sorted(usable.items())):
        colour = palette[index % len(palette)]
        coords = " ".join(
            f"{44 + (b - min_b) / span_b * (width - 64):.1f},"
            f"{height - 22 - (i - min_i) / span_i * (height - 44):.1f}"
            for b, i in points
        )
        parts.append(
            f"<polyline fill='none' stroke='{colour}' stroke-width='2' points='{coords}'/>"
        )
        legend.append(f"<span style='color:{colour}'>&#9632; {_esc(label)}</span>")
    parts.append(
        f"<text x='8' y='16' font-size='10' fill='#64748b'>price impact bps</text>"
        f"<text x='8' y='{height - 8}' font-size='10' fill='#64748b'>"
        f"block {_esc(min_b)}–{_esc(max_b)}</text>"
    )
    parts.append("</svg>")
    return (
        "".join(parts)
        + "<div class='muted' style='margin-top:6px'>"
        + " &nbsp; ".join(legend)
        + "</div>"
    )


def _provenance_table(state: dict[str, Any] | None, pool: dict[str, Any]) -> str:
    """Explicit provenance block so a reviewer can retrace every collection."""
    rows = [
        ("pool_address", _esc(pool.get("pool_address"))),
        ("dex_id", _esc(pool.get("dex_id"))),
        ("pool_type", _esc(pool.get("pool_type"))),
        ("discovery_source", _esc(pool.get("discovery_source"))),
        ("verified", _esc(pool.get("verified"))),
        ("state_source", _esc((state or {}).get("source") or "unavailable")),
        ("block_number", _esc((state or {}).get("block_number"))),
        ("block_hash", _esc((state or {}).get("block_hash") or "unavailable")),
        ("observed_at (UTC)", _esc((state or {}).get("observed_at"))),
        ("data_status", _badge((state or {}).get("data_status") or "unavailable")),
        ("detail", _esc((state or {}).get("detail") or "—")),
    ]
    body = "".join(
        f"<tr><th>{label}</th><td class='mono'>{value}</td></tr>" for label, value in rows
    )
    return f"<table>{body}</table>"


