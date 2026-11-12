from datetime import timedelta
from sqlalchemy import select
from support_agent.db.models import Cursor, Ticket, Revision, ProductLease, Need, Metric
from support_agent.policy import digest
from support_agent.queue import enqueue
from support_agent.errors import Permanent
from support_agent.services.state import remove_ticket_content, audit, rebuild_memberships, invalidate


async def ingest_once(app, product):
    app.service_scope(product)
    source = app.source(product)
    accepted, pages = 0, 0
    while True:
        with app.db.tx([product]) as s:
            cursor = s.get(Cursor, (product, source.source_id))
            if cursor is None:
                from support_agent.errors import ConfigurationError

                raise ConfigurationError("source_registry_not_provisioned")
            position, initialized = cursor.position, cursor.initialized_at
        batch = await source.list_changes(position, app.settings.batch_size)
        sequences = [c.sequence for c in batch.changes]
        if sequences != sorted(set(sequences)) or (batch.has_more and batch.next_cursor == position):
            raise Permanent()
        contents = {}
        for change in batch.changes:
            if change.operation == "upsert":
                contents[change.sequence] = await source.read_ticket(change.external_id, change.version)
        with app.db.tx([product]) as s:
            cursor = s.scalar(
                select(Cursor)
                .where(Cursor.product_id == product, Cursor.id == source.source_id)
                .with_for_update()
            )
            if cursor.position != position:
                continue  # Another poller advanced the cursor; refetch, no mixed batch commits.
            lease = s.scalar(
                select(ProductLease)
                .where(ProductLease.product_id == product, ProductLease.id == "assignment")
                .with_for_update()
            )
            for change in batch.changes:
                t = s.scalar(
                    select(Ticket)
                    .where(
                        Ticket.product_id == product,
                        Ticket.source_id == source.source_id,
                        Ticket.external_id == change.external_id,
                    )
                    .with_for_update()
                )
                if t and t.source_seq >= change.sequence:
                    s.add(Metric(product_id=product, name="dedupe_hits", value=1, created_at=app.clock.now()))
                    continue
                content = contents.get(change.sequence)
                if (
                    t
                    and change.operation == "upsert"
                    and not t.deleted
                    and t.source_version == change.version
                ):
                    current = s.get(Revision, (product, f"{t.id}:{t.current_revision}"))
                    if current is None or current.content_hash != digest(content.model_dump(mode="json")):
                        raise Permanent("source_version_content_changed")
                    # The source may redeliver the same version under a newer feed sequence.
                    # Advance ordering metadata, never the accepted revision or content lifetime.
                    t.source_seq = change.sequence
                    s.add(Metric(product_id=product, name="dedupe_hits", value=1, created_at=app.clock.now()))
                    continue
                if t and change.operation != "upsert" and t.deleted and t.source_version == change.version:
                    t.source_seq = change.sequence
                    continue
                lease.token, lease.until = None, None
                if (
                    not t
                    and content
                    and content.occurred_at < initialized - timedelta(days=30)
                    and not change.backfill
                ):
                    continue
                now = app.clock.now()
                tid = f"{source.source_id}:{change.external_id}"
                if t is None:
                    t = Ticket(
                        product_id=product,
                        id=tid,
                        source_id=source.source_id,
                        external_id=change.external_id,
                        source_seq=change.sequence,
                        source_version=change.version,
                        current_revision=0,
                        origin_id=tid,
                        deleted=False,
                        occurred_at=content.occurred_at if content else change.changed_at,
                        updated_at=now,
                        channel=content.channel if content else "text",
                    )
                    s.add(t)
                    s.flush()
                else:
                    remove_ticket_content(s, t, now, deleted=change.operation != "upsert")
                t.source_seq, t.source_version = change.sequence, change.version
                t.current_revision += 1
                t.updated_at = now
                if change.operation != "upsert":
                    t.deleted = True
                    audit(s, product, "tombstone", t.id, now)
                    continue
                # A retained tombstone cannot be resurrected by a stale source sequence.
                t.deleted = False
                t.channel = content.channel
                if content.external_id != change.external_id or content.version != change.version:
                    raise Permanent()
                previous_origin = t.origin_id
                if content.duplicate_of:
                    target = s.scalar(
                        select(Ticket).where(
                            Ticket.product_id == product,
                            Ticket.source_id == source.source_id,
                            Ticket.external_id == content.duplicate_of,
                        )
                    )
                    if target is None or target.id == t.id:
                        raise Permanent()  # Gateway must order explicit duplicate roots before aliases.
                    ancestor, visited = target, {t.id}
                    while ancestor:
                        if ancestor.id in visited or len(visited) > 1000:
                            raise Permanent()
                        visited.add(ancestor.id)
                        ancestor = (
                            s.get(Ticket, (product, ancestor.duplicate_of)) if ancestor.duplicate_of else None
                        )
                    t.origin_id, t.duplicate_of = target.origin_id, target.id
                else:
                    t.origin_id, t.duplicate_of = t.id, None
                if t.origin_id != previous_origin:
                    descendants, frontier = {t.id}, [t.id]
                    while frontier:
                        children = list(
                            s.scalars(
                                select(Ticket).where(
                                    Ticket.product_id == product,
                                    Ticket.duplicate_of.in_(frontier),
                                    Ticket.id.not_in(descendants),
                                )
                            )
                        )
                        frontier = [child.id for child in children]
                        descendants.update(frontier)
                        if len(descendants) > 10000:
                            raise Permanent()
                        for child in children:
                            child.origin_id = t.origin_id
                    affected = set()
                    for need in s.scalars(
                        select(Need).where(Need.product_id == product, Need.ticket_id.in_(descendants))
                    ):
                        need.origin_id = t.origin_id
                        affected.add(need.group_id)
                    rebuild_memberships(s, product)
                    invalidate(s, product, now, affected)
                payload = content.model_dump(mode="json")
                rid = f"{t.id}:{t.current_revision}"
                s.add(
                    Revision(
                        product_id=product,
                        id=rid,
                        ticket_id=t.id,
                        number=t.current_revision,
                        source_version=content.version,
                        content_hash=digest(payload),
                        raw=payload,
                        segments=None,
                        status="accepted",
                        coverage={},
                        created_at=now,
                    )
                )
                s.flush()
                enqueue(
                    s,
                    product,
                    "media_text" if not content.attachments else "media",
                    f"media:{rid}",
                    now,
                    rid,
                    priority=1 if change.backfill else (20 if not content.attachments else 10),
                )
                accepted += 1
                audit(s, product, "revision_accepted", rid, now)
            cursor.position = batch.next_cursor
        pages += 1
        if not batch.has_more:
            break
        if pages >= 1000:
            break  # bounded polling round; the persisted cursor resumes next round
    return {"accepted": accepted, "pages": pages}
