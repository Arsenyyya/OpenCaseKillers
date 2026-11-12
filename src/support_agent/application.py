import asyncio
from datetime import timedelta
import logging
from sqlalchemy import select
from support_agent.settings import Settings
from support_agent.clock import Clock
from support_agent.db.session import Database
from support_agent.db.models import Revision, Metric, Receipt, Cursor
from support_agent.queue import Queue
from support_agent.adapters.providers import DemoModels, Gateway, FixtureSource, BoundSource
from support_agent.adapters.storage import LocalStorage
from support_agent.errors import Forbidden, Budget, Stale, Transient, ConfigurationError


class Application:
    def __init__(self, settings: Settings, *, db=None, clock=None, models=None):
        self.settings = settings
        self.db = db or Database(settings.database_url)
        self.clock = clock or Clock()
        self.queue = Queue(self.db, settings, self.clock)
        self.gateway = Gateway(settings) if settings.mode == "internal" else None
        self.models = models if models is not None else (self.gateway or DemoModels(settings.fixtures_dir))
        self.storage = LocalStorage(settings.fixtures_dir / "assets", settings.max_file_bytes)

    def service_scope(self, product):
        if product not in self.settings.service_products:
            raise Forbidden()

    def source(self, product):
        self.service_scope(product)
        if self.gateway:
            with self.db.tx([product]) as session:
                cursor = session.get(Cursor, (product, self.gateway.source_id))
                if not cursor:
                    raise ConfigurationError("source_registry_not_provisioned")
                initial_since = cursor.initialized_at - timedelta(days=30)
            return BoundSource(self.gateway, product, initial_since)
        return FixtureSource(self.settings.fixtures_dir, product)

    async def close(self):
        if self.gateway:
            await self.gateway.close()
        self.db.close()

    async def model_call(self, claim, provider, factory):
        slot_id, token = self.queue.acquire_slot(provider)

        async def renew():
            while True:
                await asyncio.sleep(self.settings.lease_seconds / 3)
                self.queue.slot_action(slot_id, token)

        heartbeat, work = None, None
        try:
            with self.db.tx([claim.product_id]) as s:
                self.queue.guard(s, claim)
                r = s.scalar(
                    select(Revision)
                    .where(Revision.product_id == claim.product_id, Revision.id == claim.revision_id)
                    .with_for_update()
                )
                if r.model_calls >= self.settings.max_model_calls:
                    raise Budget()
                r.model_calls += 1
                s.add(
                    Metric(
                        product_id=claim.product_id,
                        name=f"{provider}_calls",
                        value=1,
                        created_at=self.clock.now(),
                    )
                )
            heartbeat = asyncio.create_task(renew())
            work = asyncio.create_task(factory())
            done, _ = await asyncio.wait(
                {work, heartbeat},
                timeout=self.settings.timeout_seconds + 1,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                # A renewal failure revokes useful work; do not let its exception mask cleanup.
                raise Transient() from None
            if work not in done:
                raise Transient() from None
            result = work.result()
            if isinstance(result, tuple):
                raw, usage = result
                with self.db.tx([claim.product_id]) as s:
                    self.queue.guard(s, claim)
                    r = s.get(Revision, (claim.product_id, claim.revision_id))
                    r.usage = {k: r.usage.get(k, 0) + v for k, v in usage.items()} | {
                        k: v for k, v in r.usage.items() if k not in usage
                    }
                return raw
            return result
        finally:
            pending = [task for task in (work, heartbeat) if task is not None]
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            try:
                self.queue.slot_action(slot_id, token, release=True)
            except Stale:
                pass
            except Exception:
                logging.getLogger("support_agent").warning(
                    "provider_slot_release_failed provider=%s", provider
                )

    async def local_deliver(self, product, key, payload):
        # A separate transaction represents the local receiver's durable idempotency store.
        from sqlalchemy.dialects.postgresql import insert
        from uuid import uuid4

        with self.db.tx([product]) as s:
            s.execute(
                insert(Receipt)
                .values(
                    product_id=product,
                    id=str(uuid4()),
                    correlation_key=key,
                    payload=payload,
                    created_at=self.clock.now(),
                )
                .on_conflict_do_nothing(index_elements=["correlation_key"])
            )
            return s.scalar(
                select(Receipt.id).where(Receipt.product_id == product, Receipt.correlation_key == key)
            )
