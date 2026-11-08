"""Manual clean-db walkthrough, restart, replay, update and deletion; no production calls."""

import asyncio
import json
import shutil
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from sqlalchemy import select, func

from support_agent.application import Application
from support_agent.settings import Settings
from support_agent.clock import FrozenClock
from support_agent.db.testing import isolated_database
from support_agent.db.models import Ticket, Membership, Revision, Group
from support_agent.demo import run_demo
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest
from support_agent.worker import drain


async def main():
    settings = Settings()
    with isolated_database(settings) as cfg, TemporaryDirectory() as directory:
        root = Path(directory)
        cfg = cfg.model_copy(update={"token_file": root / "tokens.json", "delivery_mode": "reviewed"})
        clock = FrozenClock(datetime.fromisoformat("2026-11-06T12:00:00+00:00"))
        app = Application(cfg, clock=clock)
        try:
            first = await run_demo(app)
            assert first["counts"] == {"tickets": 40, "memberships": 37, "cards": 6, "receipts": 2}
            await app.close()
            app = Application(cfg, clock=clock)
            second = await run_demo(app)
            assert second["counts"] == first["counts"]
            fixtures = root / "fixtures"
            shutil.copytree(cfg.fixtures_dir, fixtures)
            path = fixtures / "atlas/changes.jsonl"
            path.write_text(path.read_text() + (fixtures / "atlas/mutations.jsonl").read_text())
            app.settings = cfg.model_copy(update={"fixtures_dir": fixtures})
            await ingest_once(app, "atlas")
            await drain(app)
            build_digest(app, "atlas")
            with app.db.tx(["atlas"]) as s:
                ticket = s.get(Ticket, ("atlas", "demo:t03"))
                revision = s.get(Revision, ("atlas", "demo:t03:1"))
                assert ticket.deleted and revision.raw is None and revision.segments is None
                group = s.scalar(select(Group).where(Group.need == "Добавить выгрузку списка заказов в CSV"))
                origins = s.scalar(
                    select(func.count()).select_from(Membership).where(Membership.group_id == group.id)
                )
                assert origins == 10
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "synthetic": True,
                        "initial": first["counts"],
                        "restart_and_repeat_unchanged": True,
                        "atlas_csv_origins_after_update_delete": origins,
                        "deleted_content_purged": True,
                        "model_quality": "not_run",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        finally:
            await app.close()


if __name__ == "__main__":
    asyncio.run(main())
