"""One CLI for administrative setup, workers and the same application rules used by HTTP."""

import argparse
import asyncio
import json
import logging
import signal
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from support_agent.application import Application
from support_agent.settings import get_settings, ROOT
from support_agent.clock import FrozenClock
from support_agent.demo import seed_demo, run_demo
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest
from support_agent.services.state import retention_plan, retention_run
from support_agent.worker import worker_loop, drain
from support_agent.evaluate import evaluate
from support_agent.observability.metrics import report
from support_agent.errors import AgentError, ConfigurationError


def output(result):
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def command_actor(app, args):
    from support_agent.auth import authenticate

    try:
        if args.token_file:
            token = args.token_file.read_text().strip()
        elif app.settings.app_env == "local" and app.settings.mode == "demo":
            token = json.loads(app.settings.token_file.read_text())[args.local_actor]
        else:
            raise ConfigurationError("verified_token_file_required")
    except (OSError, KeyError, ValueError):
        raise ConfigurationError("token_file_unavailable") from None
    return authenticate(app, "Bearer " + token)


async def dispatch(args):
    settings = get_settings()
    clock = None
    if args.command in ("run-demo", "seed-demo"):
        if settings.app_env != "local" or settings.mode != "demo":
            raise ConfigurationError("demo_commands_are_local_only")
        if not settings.database_admin_url:
            raise ConfigurationError("administrative_database_required_for_demo_seed")
        manifest = json.loads((settings.fixtures_dir / "manifest.json").read_text())
        clock = FrozenClock(datetime.fromisoformat(manifest["clock"]))
    app = Application(settings, clock=clock)
    try:
        app.db.runtime_ready()
        if args.command == "seed-demo":
            seed_demo(app)
            output({"status": "seeded", "synthetic": True, "tokens": str(settings.token_file)})
        elif args.command == "run-demo":
            output(await run_demo(app))
        elif args.command == "ingest-once":
            output({p: await ingest_once(app, p) for p in settings.service_products})
        elif args.command == "worker":
            if args.once:
                output({"stage": args.kind, "processed": await drain(app, kind=args.kind)})
            else:
                await worker_loop(app, args.kind)
        elif args.command == "build-digest":
            output(build_digest(app, args.product, args.period))
        elif args.command == "metrics":
            output(report(app, settings.service_products))
        elif args.command == "resolve-delivery":
            from support_agent.services.delivery.service import reconcile_delivery

            actor = command_actor(app, args)
            output(
                reconcile_delivery(
                    app, actor, args.product, args.outbox_id, args.resolution, args.reason, args.external_id
                )
            )
        elif args.command == "retention":
            app.service_scope(args.product)
            if args.execute and args.offset:
                raise ConfigurationError("retention_execute_requires_zero_offset")
            from sqlalchemy import select
            from support_agent.db.models import ProductLease

            with app.db.tx([args.product]) as s:
                plan = retention_plan(
                    s,
                    args.product,
                    app.clock.now(),
                    settings.retention_days,
                    limit=args.limit,
                    offset=args.offset,
                )
                if args.execute:
                    lease = s.scalar(
                        select(ProductLease)
                        .where(ProductLease.product_id == args.product, ProductLease.id == "assignment")
                        .with_for_update()
                    )
                    lease.token, lease.until = None, None
                    ids = retention_run(
                        s, args.product, app.clock.now(), settings.retention_days, limit=args.limit
                    )
                    output(
                        {
                            "dry_run": False,
                            "ticket_ids": ids,
                            "planned_revisions": plan["revision_ids"],
                            "has_more": plan["has_more"],
                        }
                    )
                else:
                    output({"dry_run": True, **plan})
        elif args.command in ("scheduler", "ingest"):
            stop = asyncio.Event()
            for sig in (signal.SIGTERM, signal.SIGINT):
                asyncio.get_running_loop().add_signal_handler(sig, stop.set)
            while not stop.is_set():
                for p in settings.service_products:
                    try:
                        if args.command == "ingest":
                            await ingest_once(app, p)
                        else:
                            build_digest(app, p)
                    except Exception as exc:
                        logging.getLogger("support_agent").warning(
                            "poll_failed product=%s code=%s", p, getattr(exc, "code", "processing_failed")
                        )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.poll_seconds)
                except TimeoutError:
                    pass
        elif args.command == "measure-endpoints":
            if not app.gateway:
                raise ConfigurationError("configured_internal_mode_required")
            import time

            durations = []
            for _ in range(args.calls):
                started = time.perf_counter()
                await app.gateway.extract(
                    [{"segment_id": "probe", "text": "Synthetic probe: please add CSV export."}]
                )
                durations.append(time.perf_counter() - started)
            output({"synthetic_probe": True, "calls": len(durations), "seconds": durations})
    finally:
        await app.close()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="OpenCaseKillers: bounded, auditable support analytics")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in (
        "migrate",
        "seed-demo",
        "run-demo",
        "ingest-once",
        "ingest",
        "scheduler",
        "metrics",
        "doctor",
    ):
        sub.add_parser(name)
    provision = sub.add_parser("provision", help="Administrative product/issuer-subject registry setup")
    provision.add_argument("--config", type=Path, required=True)
    provision.add_argument("--dry-run", action="store_true")
    api = sub.add_parser("api")
    api.add_argument("--host", default="127.0.0.1")
    api.add_argument("--port", type=int, default=8000)
    worker = sub.add_parser("worker")
    worker.add_argument("kind", choices=["media", "media_text", "analysis", "delivery", "retention"])
    worker.add_argument("--once", action="store_true")
    build = sub.add_parser("build-digest")
    build.add_argument("product")
    build.add_argument("--period")
    retention = sub.add_parser("retention")
    retention.add_argument("product")
    retention.add_argument("--execute", action="store_true")
    retention.add_argument("--limit", type=int, choices=range(1, 501), default=100, metavar="1..500")
    retention.add_argument("--offset", type=int, default=0)
    resolve = sub.add_parser("resolve-delivery")
    resolve.add_argument("product")
    resolve.add_argument("outbox_id")
    resolve.add_argument("--resolution", required=True, choices=["delivered", "not_delivered", "corrected"])
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--external-id")
    resolve.add_argument("--token-file", type=Path, help="Verified bearer/JWT file; never a token in argv")
    resolve.add_argument(
        "--local-actor", default="operator", choices=["viewer", "operator", "reviewer", "boreal"]
    )
    ev = sub.add_parser("evaluate")
    ev.add_argument("--gold")
    ev.add_argument("--results")
    load = sub.add_parser("load-test")
    load.add_argument("--tickets", type=int, default=300)
    load.add_argument("--workers", type=int, default=4)
    measure = sub.add_parser("measure-endpoints")
    measure.add_argument("--calls", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    try:
        if args.command == "migrate":
            from alembic import command
            from alembic.config import Config

            cfg = Config(str(ROOT / "alembic.ini"))
            cfg.set_main_option("script_location", str(ROOT / "migrations"))
            command.upgrade(cfg, "head")
            output({"status": "migrated"})
        elif args.command == "provision":
            from support_agent.provisioning import provision_file

            output(provision_file(args.config, dry_run=args.dry_run))
        elif args.command == "doctor":
            from support_agent.diagnostics import doctor

            result = doctor(get_settings())
            output(result)
            if not result["ready"]:
                raise SystemExit(1)
        elif args.command == "api":
            import uvicorn

            uvicorn.run(
                "support_agent.api:create_app",
                factory=True,
                host=args.host,
                port=args.port,
                log_level="warning",
            )
        elif args.command == "evaluate":
            output(evaluate(args.gold, args.results))
        elif args.command == "load-test":
            from support_agent.loadtest import load_test

            output(load_test(args.tickets, args.workers))
        else:
            asyncio.run(dispatch(args))
    except ValidationError:
        output(
            {
                "error": "configuration_error",
                "hint": "Check explicit settings and the example configuration; values are not echoed.",
            }
        )
        raise SystemExit(1) from None
    except Exception as exc:
        output(
            {
                "error": exc.code if isinstance(exc, AgentError) else "command_failed",
                "type": type(exc).__name__,
            }
        )
        raise SystemExit(1) from None
