"""Command-line interface.

Commands map one-to-one onto the documented workflow:

``health``  verify RPC reachability, chain ID and a basic ``eth_call``
``discover`` find and register pools for the enabled DEX adapters
``collect``  read state, run execution analytics and (optionally) historical events
``analyze``  re-run execution analytics for the stored markets, no event scan
``report``   write CSV/JSON/Markdown reports from the database
``benchmark`` run a real collection and measure it
``serve``    start the read-only FastAPI server and dashboard
``initdb``   create/upgrade the database schema

The CLI never signs, submits or simulates a state-changing transaction: gas is
estimated with ``eth_estimateGas`` only.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC
from typing import Any

from .config import Settings, load_settings
from .errors import ConfluxAnalyticsError
from .logging_setup import configure_logging, get_logger
from .rpc.provider import RpcProvider

logger = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


def _print(payload: Any) -> None:
    """Print a JSON payload so every command output is machine-readable."""
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _build_provider(settings: Settings) -> RpcProvider:
    return RpcProvider(
        rpc_url=settings.network.rpc_url,
        chain_id=settings.network.chain_id,
        timeout_seconds=settings.network.rpc_timeout_seconds,
        max_retries=settings.network.rpc_max_retries,
        retry_backoff_seconds=settings.network.rpc_retry_backoff_seconds,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conflux-analytics",
        description="Liquidity and execution analytics for Conflux eSpace DEX markets",
    )
    parser.add_argument("--env-file", default=None, help="path to a .env file to load")
    parser.add_argument("--config", default=None, help="path to the YAML configuration file")
    parser.add_argument(
        "--log-level", default=None, help="override the configured logging level"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health", help="check RPC reachability, chain ID and eth_call")

    discover = sub.add_parser("discover", help="discover and register pools")
    discover.add_argument("--dex", action="append", dest="dex_ids", help="limit to a dex_id")

    collect = sub.add_parser("collect", help="collect state, executions and events")
    collect.add_argument("--dex", action="append", dest="dex_ids", help="limit to a dex_id")
    collect.add_argument("--start-block", type=int, default=None)
    collect.add_argument("--end-block", type=int, default=None)
    collect.add_argument("--no-events", action="store_true", help="skip historical log scans")
    collect.add_argument("--no-execution", action="store_true", help="skip execution analytics")

    analyze = sub.add_parser("analyze", help="re-run execution analytics over stored markets")
    analyze.add_argument("--dex", action="append", dest="dex_ids", help="limit to a dex_id")

    report = sub.add_parser("report", help="write CSV/JSON/Markdown reports")
    report.add_argument("--limit", type=int, default=20_000, help="max rows per dataset")

    benchmark = sub.add_parser("benchmark", help="run a measured collection benchmark")
    benchmark.add_argument("--dex", action="append", dest="dex_ids", help="limit to a dex_id")
    benchmark.add_argument("--start-block", type=int, default=None)
    benchmark.add_argument("--end-block", type=int, default=None)

    initdb = sub.add_parser("initdb", help="create or upgrade the database schema")
    initdb.add_argument("--quiet", action="store_true")

    serve = sub.add_parser("serve", help="start the API and dashboard")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--reload", action="store_true")
    return parser


def _settings(args: argparse.Namespace) -> Settings:
    """Resolve configuration, honouring the optional ``--env-file``."""
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    settings = load_settings(config_path=args.config)
    if args.log_level:
        settings = settings.model_copy(
            update={"logging": settings.logging.model_copy(update={"level": args.log_level})}
        )
    configure_logging(settings.logging)
    return settings


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _cmd_health(settings: Settings, args: argparse.Namespace) -> int:
    _ = args  # health takes no command-specific options
    provider = _build_provider(settings)
    try:
        health = provider.health()
        _print(
            {
                "network": settings.network.name,
                "chain_id_expected": settings.network.chain_id,
                "provider": health.as_dict(),
                "enabled_dexes": [dex.dex_id for dex in settings.enabled_dexes()],
            }
        )
        return EXIT_OK if health.status == "ok" else EXIT_FAILURE
    finally:
        provider.close()


def _pipeline(settings: Settings):
    from .pipeline import AnalyticsPipeline

    return AnalyticsPipeline(settings, _build_provider(settings))


def _cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    pipeline = _pipeline(settings)
    try:
        _print(pipeline.discover(args.dex_ids))
        return EXIT_OK
    finally:
        pipeline.close()


def _cmd_collect(settings: Settings, args: argparse.Namespace) -> int:
    pipeline = _pipeline(settings)
    try:
        result = pipeline.run_collection(
            dex_ids=args.dex_ids,
            start_block=args.start_block,
            end_block=args.end_block,
            collect_events=not args.no_events,
            with_execution=not args.no_execution,
            trigger="cli:collect",
        )
        _print(result.as_dict())
        return EXIT_OK if result.status != "failed" else EXIT_FAILURE
    finally:
        pipeline.close()


def _cmd_analyze(settings: Settings, args: argparse.Namespace) -> int:
    """Execution analytics over the stored markets, without a historical log scan."""
    pipeline = _pipeline(settings)
    try:
        result = pipeline.run_collection(
            dex_ids=args.dex_ids,
            collect_events=False,
            with_execution=True,
            trigger="cli:analyze",
        )
        _print(result.as_dict())
        return EXIT_OK if result.status != "failed" else EXIT_FAILURE
    finally:
        pipeline.close()


def _cmd_report(settings: Settings, args: argparse.Namespace) -> int:
    from .reporting import write_report

    outcome = write_report(settings, limit=args.limit)
    _print({"counts": outcome["counts"], "written": outcome["written"]})
    return EXIT_OK


def _cmd_benchmark(settings: Settings, args: argparse.Namespace) -> int:
    from .reporting import run_benchmark

    provider = _build_provider(settings)
    try:
        result = run_benchmark(
            settings,
            provider,
            dex_ids=args.dex_ids,
            start_block=args.start_block,
            end_block=args.end_block,
        )
    finally:
        provider.close()
    print(result.render())
    report_dir = settings.report_path
    report_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = report_dir / f"{stamp}-benchmark.json"
    path.write_text(json.dumps(result.as_dict(), indent=2, sort_keys=True), encoding="utf-8")
    print(f"\nBenchmark JSON written to {path}")
    return EXIT_OK if result.run_status else EXIT_FAILURE


def _cmd_initdb(settings: Settings, args: argparse.Namespace) -> int:
    from .storage.database import connect
    from .storage.migrations import schema_version

    conn = connect(settings.db_path)
    try:
        payload = {
            "database_path": str(settings.db_path),
            "schema_version": schema_version(conn),
        }
    finally:
        conn.close()
    if not args.quiet:
        _print(payload)
    return EXIT_OK


def _cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    host = args.host or settings.api.host
    port = args.port or settings.api.port
    _print(
        {
            "host": host,
            "port": port,
            "api_docs": f"http://{host}:{port}/docs",
            "dashboard": f"http://{host}:{port}{settings.dashboard.mount_path}",
            "chain_id": settings.network.chain_id,
        }
    )
    uvicorn.run(
        "conflux_analytics.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level=settings.logging.level.lower(),
    )
    return EXIT_OK


COMMANDS = {
    "health": _cmd_health,
    "discover": _cmd_discover,
    "collect": _cmd_collect,
    "analyze": _cmd_analyze,
    "report": _cmd_report,
    "benchmark": _cmd_benchmark,
    "initdb": _cmd_initdb,
    "serve": _cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``conflux-analytics`` console script."""
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        settings = _settings(args)
    except ConfluxAnalyticsError as exc:
        print(f"configuration error ({exc.category}): {exc.message}", file=sys.stderr)
        return EXIT_USAGE
    handler = COMMANDS[args.command]
    try:
        return handler(settings, args)
    except ConfluxAnalyticsError as exc:
        logger.error(
            "command failed",
            extra={"event": "cli.error", "command": args.command, "category": exc.category},
        )
        print(f"{args.command} failed ({exc.category}): {exc.message}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover - covered through the console script
    sys.exit(main())