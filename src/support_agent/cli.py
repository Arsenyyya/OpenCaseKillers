import argparse
import asyncio
import json
import logging
import signal
from datetime import datetime

from support_agent.application import Application
from support_agent.settings import get_settings, ROOT
from support_agent.clock import FrozenClock
from support_agent.demo import seed_demo, run_demo
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest
from support_agent.services.state import retention_preview, retention_run
from support_agent.worker import worker_loop, drain
from support_agent.evaluate import evaluate
from support_agent.observability.metrics import report


def output(result):
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


async def dispatch(args):
    settings = get_settings()
    clock = None
    if args.command in ("run-demo", "seed-demo"):
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
                output({"processed": await drain(app)})
            else:
                await worker_loop(app, args.kind)
        elif args.command == "build-digest":
            output(build_digest(app, args.product, args.period))
        elif args.command == "metrics":
            output(report(app, settings.service_products))
        elif args.command == "retention":
            app.service_scope(args.product)
            from sqlalchemy import select
            from support_agent.db.models import ProductLease

            with app.db.tx([args.product]) as s:
                if args.execute:
                    lease = s.scalar(
                        select(ProductLease)
                        .where(ProductLease.product_id == args.product, ProductLease.id == "assignment")
                        .with_for_update()
                    )
                    lease.token, lease.until = None, None
                    ids = retention_run(s, args.product, app.clock.now(), settings.retention_days)
                else:
                    ids = retention_preview(s, args.product, app.clock.now(), settings.retention_days)
                output({"dry_run": not args.execute, "ticket_ids": ids})
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
                    except Exception:
                        logging.getLogger("support_agent").warning("poll_failed product=%s", p)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.poll_seconds)
                except TimeoutError:
                    pass
        elif args.command == "measure-endpoints":
            if not app.gateway:
                raise RuntimeError("measure-endpoints requires configured internal mode")
            import time

            durations = []
            for _ in range(args.calls):
                t = time.perf_counter()
                await app.gateway.extract(
                    [{"segment_id": "probe", "text": "Synthetic probe: please add CSV export."}]
                )
                durations.append(time.perf_counter() - t)
            output({"synthetic_probe": True, "calls": len(durations), "seconds": durations})
    finally:
        await app.close()


def main():
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    parser = argparse.ArgumentParser(description="Bounded support analytics pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["migrate", "seed-demo", "run-demo", "ingest-once", "ingest", "scheduler", "metrics"]:
        sub.add_parser(name)
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
    ev = sub.add_parser("evaluate")
    ev.add_argument("--gold")
    ev.add_argument("--results")
    load = sub.add_parser("load-test")
    load.add_argument("--tickets", type=int, default=300)
    load.add_argument("--workers", type=int, default=4)
    measure = sub.add_parser("measure-endpoints")
    measure.add_argument("--calls", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    if args.command == "migrate":
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "migrations"))
        command.upgrade(cfg, "head")
    elif args.command == "api":
        import uvicorn

        uvicorn.run(
            "support_agent.api:create_app", factory=True, host=args.host, port=args.port, log_level="warning"
        )
    elif args.command == "evaluate":
        output(evaluate(args.gold, args.results))
    elif args.command == "load-test":
        from support_agent.loadtest import load_test

        output(load_test(args.tickets, args.workers))
    else:
        try:
            asyncio.run(dispatch(args))
        except Exception as exc:
            output({"error": getattr(exc, "code", "command_failed"), "type": type(exc).__name__})
            raise SystemExit(1) from None
