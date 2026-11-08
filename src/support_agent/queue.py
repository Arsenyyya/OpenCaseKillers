import random
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, or_, and_
from sqlalchemy.dialects.postgresql import insert
from support_agent.db.models import Job, ProductLease, ProviderSlot, Revision, Ticket, Audit
from support_agent.errors import Stale, Capacity, Permanent


@dataclass(frozen=True)
class Claim:
    product_id: str
    id: str
    token: str
    revision_id: str | None
    kind: str


def enqueue(s, product, kind, key, now, revision_id=None, priority=10):
    s.execute(
        insert(Job)
        .values(
            product_id=product,
            id=str(uuid4()),
            kind=kind,
            dedupe_key=key,
            revision_id=revision_id,
            priority=priority,
            status="queued",
            available_at=now,
            attempts=0,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=["product_id", "dedupe_key"])
    )


class Queue:
    def __init__(self, db, settings, clock):
        self.db, self.settings, self.clock = db, settings, clock
        self.turn = 0

    def claim(self, kind):
        products = self.settings.service_products
        # One claim at a time, round robin across products; fresh work precedes backfill.
        for offset in range(len(products)):
            idx = (self.turn + offset) % len(products)
            p = products[idx]
            now = self.clock.now()
            with self.db.tx([p]) as s:
                j = s.scalar(
                    select(Job)
                    .where(
                        Job.kind.in_(["media", "media_text"]) if kind == "media" else Job.kind == kind,
                        or_(
                            and_(Job.status.in_(["queued", "retry_wait"]), Job.available_at <= now),
                            and_(Job.status == "running", Job.lease_until <= now),
                        ),
                    )
                    .order_by(Job.priority.desc(), Job.available_at, Job.id)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if j is None:
                    continue
                if j.attempts >= self.settings.max_attempts:
                    j.status, j.error_code = "dead", "attempts_exhausted"
                    if j.revision_id:
                        r = s.get(Revision, (p, j.revision_id))
                        if r and r.status != "deleted":
                            r.status = "dead"
                    continue
                j.status, j.claim_token = "running", str(uuid4())
                j.attempts += 1
                j.heartbeat, j.lease_until = now, now + timedelta(seconds=self.settings.lease_seconds)
                self.turn = (idx + 1) % len(products)
                return Claim(p, j.id, j.claim_token, j.revision_id, kind)
        return None

    def guard(self, s, c):
        snapshot = s.get(Job, (c.product_id, c.id))
        if not snapshot:
            raise Stale()
        # Ticket before job matches ingest cancellation lock order.
        if snapshot.revision_id:
            r = s.get(Revision, (c.product_id, snapshot.revision_id))
            t = s.scalar(
                select(Ticket)
                .where(Ticket.product_id == c.product_id, Ticket.id == r.ticket_id)
                .with_for_update()
            )
            if t.deleted or t.current_revision != r.number or r.status == "deleted":
                raise Stale()
        j = s.scalar(
            select(Job)
            .where(Job.product_id == c.product_id, Job.id == c.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not j or j.status != "running" or j.claim_token != c.token or j.lease_until <= self.clock.now():
            raise Stale()
        return j

    def heartbeat(self, c):
        with self.db.tx([c.product_id]) as s:
            j = self.guard(s, c)
            j.heartbeat = self.clock.now()
            j.lease_until = j.heartbeat + timedelta(seconds=self.settings.lease_seconds)

    def fail(self, c, exc):
        with self.db.tx([c.product_id]) as s:
            try:
                j = self.guard(s, c)
            except Stale:
                return
            j.error_code = getattr(exc, "code", "processing_failed")
            j.claim_token = None
            j.lease_until = None
            if isinstance(exc, Capacity):
                j.attempts -= 1
                j.status = "retry_wait"
                j.available_at = self.clock.now() + timedelta(seconds=2)
                return
            terminal = isinstance(exc, Permanent) or j.attempts >= self.settings.max_attempts
            j.status = "dead" if terminal else "retry_wait"
            wait = max(2**j.attempts + random.random(), getattr(exc, "retry_after", 0))
            j.available_at = self.clock.now() + timedelta(seconds=wait)
            if terminal and j.revision_id:
                r = s.get(Revision, (c.product_id, j.revision_id))
                r.status = "dead"
            s.add(
                Audit(
                    product_id=c.product_id,
                    event=j.status,
                    actor="worker",
                    ref=j.id,
                    created_at=self.clock.now(),
                )
            )

    def complete(self, s, c, next_kind=None):
        j = self.guard(s, c)
        j.status, j.claim_token, j.lease_until = "succeeded", None, None
        if next_kind:
            enqueue(
                s,
                c.product_id,
                next_kind,
                f"{next_kind}:{j.revision_id}",
                self.clock.now(),
                j.revision_id,
                j.priority,
            )

    def acquire_product(self, product):
        now = self.clock.now()
        with self.db.tx([product]) as s:
            lease = s.scalar(
                select(ProductLease).where(ProductLease.id == "assignment").with_for_update(skip_locked=True)
            )
            if lease is None or (lease.until and lease.until > now):
                raise Capacity()
            lease.token = str(uuid4())
            lease.until = now + timedelta(seconds=self.settings.lease_seconds)
            return lease.token

    def product_guard(self, s, product, token):
        lease = s.scalar(
            select(ProductLease)
            .where(ProductLease.product_id == product, ProductLease.id == "assignment")
            .with_for_update()
        )
        if not lease or lease.token != token or lease.until <= self.clock.now():
            raise Stale()
        return lease

    def renew_product(self, product, token):
        with self.db.tx([product]) as s:
            self.product_guard(s, product, token).until = self.clock.now() + timedelta(
                seconds=self.settings.lease_seconds
            )

    def release_product(self, product, token):
        with self.db.tx([product]) as s:
            lease = s.get(ProductLease, (product, "assignment"))
            if lease and lease.token == token:
                lease.token, lease.until = None, None

    def acquire_slot(self, provider):
        now = self.clock.now()
        with self.db.tx() as s:
            slot = s.scalar(
                select(ProviderSlot)
                .where(
                    ProviderSlot.provider == provider,
                    or_(ProviderSlot.until.is_(None), ProviderSlot.until <= now),
                )
                .order_by(ProviderSlot.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not slot:
                raise Capacity()
            slot.token = str(uuid4())
            slot.until = now + timedelta(seconds=self.settings.lease_seconds)
            return slot.id, slot.token

    def slot_action(self, slot_id, token, release=False):
        with self.db.tx() as s:
            slot = s.scalar(select(ProviderSlot).where(ProviderSlot.id == slot_id).with_for_update())
            if slot.token != token or slot.until <= self.clock.now():
                raise Stale()
            if release:
                slot.token, slot.until = None, None
            else:
                slot.until = self.clock.now() + timedelta(seconds=self.settings.lease_seconds)
