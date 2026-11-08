from sqlalchemy import select, text, delete
from support_agent.db.models import Revision, Ticket, Group, Need, Metric, Observation
from datetime import datetime
from support_agent.policy import parse_extraction, parse_match
from support_agent.errors import InvalidOutput, Budget, Stale
from support_agent.services.state import rebuild_memberships, invalidate, audit
from support_agent.settings import ROOT
import json
import asyncio
from contextlib import suppress
from support_agent.policy import digest


def chunks(segments, max_chars):
    group, size = [], 0
    for seg in segments:
        if group and size + len(seg["text"]) > max_chars:
            yield group
            group, size = [], 0
        group.append(seg)
        size += len(seg["text"])
    if group:
        yield group


def find_similar_groups(app, p, item):
    words = [item.need]
    for term, synonyms in json.loads((ROOT / "config/terms.json").read_text()).items():
        if any(x.lower() in item.need.lower() for x in synonyms):
            words.extend([term] + synonyms)
    query = " ".join(words)[:2000]
    # Indexed FTS/trigram top-k; only compatible active groups in the trusted product scope.
    with app.db.tx([p]) as s:
        rows = (
            s.execute(
                text("""
            SELECT id, need, context,
              ts_rank(to_tsvector('russian', need || ' ' || context), plainto_tsquery('russian', :q))
                + similarity(need, :need) AS score
            FROM groups WHERE product_id=:p AND active AND kind=:kind
              AND (to_tsvector('russian', need || ' ' || context) @@ plainto_tsquery('russian', :q)
                   OR need % :need)
            ORDER BY score DESC, id LIMIT :k
        """),
                {"q": query, "need": item.need, "p": p, "kind": item.kind, "k": app.settings.candidate_limit},
            )
            .mappings()
            .all()
        )
        return [dict(r) for r in rows[: app.settings.matching_limit]]


async def run_analysis(app, c):
    p = c.product_id
    with app.db.tx([p]) as s:
        app.queue.guard(s, c)
        r = s.get(Revision, (p, c.revision_id))
        segments, complete = r.segments or [], r.coverage.get("complete", False)
        ticket_id = r.ticket_id
    items = []
    for part in chunks(segments, app.settings.context_chars):
        raw = await app.model_call(c, "llm", lambda: app.models.extract(part))
        try:
            parsed = parse_extraction(raw, part, app.settings.max_items)
        except InvalidOutput:
            with app.db.tx([p]) as s:
                s.add(Metric(product_id=p, name="invalid_json", value=1, created_at=app.clock.now()))
            raw = await app.model_call(c, "llm", lambda: app.models.extract(part, repair=True))
            parsed = parse_extraction(raw, part, app.settings.max_items)
        items.extend(parsed.items)
        if len(items) > app.settings.max_items:
            raise Budget()
    token = app.queue.acquire_product(p)
    assignments, local = [], []

    async def renew_assignment():
        while True:
            await asyncio.sleep(app.settings.lease_seconds / 3)
            app.queue.renew_product(p, token)

    renewal = asyncio.create_task(renew_assignment())
    try:
        for item in items:
            await asyncio.sleep(0)
            candidates = find_similar_groups(app, p, item)
            # Same-ticket new groups are also bounded, trusted candidates.
            seen = {x["id"] for x in candidates}
            candidates = (candidates + [x for x in local if x["id"] not in seen and x["kind"] == item.kind])[
                : app.settings.matching_limit
            ]
            # Matching needs only already validated evidence, never the whole long ticket.
            lookup = {row["segment_id"]: row for row in segments}
            match_segments = [dict(lookup[e.segment_id], text=e.quote) for e in item.evidence]
            raw = await app.model_call(c, "llm", lambda: app.models.match(item, candidates, match_segments))
            match = parse_match(raw, candidates, match_segments)
            if match.action == "match":
                gid = match.candidate_id
            else:
                from uuid import uuid4

                gid = str(uuid4())
                local.append({"id": gid, "need": item.need, "context": item.context, "kind": item.kind})
            assignments.append((item, gid, match.action == "uncertain"))
        with app.db.tx([p]) as s:
            app.queue.product_guard(s, p, token)
            app.queue.guard(s, c)
            r = s.get(Revision, (p, c.revision_id))
            t = s.get(Ticket, (p, ticket_id))
            s.execute(delete(Need).where(Need.product_id == p, Need.revision_id == r.id))
            s.flush()
            affected = set()
            for item, gid, provisional in assignments:
                if gid in affected:
                    continue
                affected.add(gid)
                g = s.get(Group, (p, gid))
                observation_hash = digest([item.kind, item.need])
                observed = s.scalar(
                    select(Observation).where(
                        Observation.product_id == p,
                        Observation.ticket_id == t.id,
                        Observation.need_hash == observation_hash,
                    )
                )
                source_time = datetime.fromisoformat(r.raw["occurred_at"]) if r.raw else t.occurred_at
                first = observed.first_seen if observed else min(source_time, r.created_at)
                if not observed:
                    s.add(
                        Observation(
                            product_id=p, ticket_id=t.id, need_hash=observation_hash, first_seen=first
                        )
                    )
                if not g:
                    g = Group(
                        product_id=p,
                        id=gid,
                        kind=item.kind,
                        need=item.need,
                        context=item.context,
                        first_seen=first,
                        provisional=provisional or item.needs_review,
                        active=True,
                    )
                    s.add(g)
                    s.flush()
                s.add(
                    Need(
                        product_id=p,
                        id=f"{r.id}:{digest([item.kind, item.need, gid])[:20]}",
                        revision_id=r.id,
                        group_id=gid,
                        ticket_id=t.id,
                        origin_id=t.origin_id,
                        kind=item.kind,
                        need=item.need,
                        context=item.context,
                        urgency=item.urgency,
                        evidence=[e.model_dump() for e in item.evidence],
                        eligible=complete and not item.needs_review and not provisional,
                        first_seen=first,
                    )
                )
            r.status = "processed" if complete else "partial"
            if any(item.needs_review or provisional for item, _, provisional in assignments):
                r.status = "needs_review"
            r.model_id, r.prompt_version = app.models.model_id, "extraction-v1+matching-v1"
            r.processed_at = app.clock.now()
            old_fingerprints = {
                g.id: g.fingerprint for g in s.scalars(select(Group).where(Group.product_id == p))
            }
            rebuild_memberships(s, p)
            changed = {
                g.id
                for g in s.scalars(select(Group).where(Group.product_id == p))
                if old_fingerprints.get(g.id) != g.fingerprint
            }
            invalidate(s, p, app.clock.now(), changed)
            audit(s, p, "analysis_validated", r.id, app.clock.now())
            app.queue.complete(s, c)
    finally:
        renewal.cancel()
        with suppress(asyncio.CancelledError, Stale):
            await renewal
        app.queue.release_product(p, token)
