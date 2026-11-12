"""Validated extraction and product-scoped assignment, with durable retry boundaries."""

import asyncio
import json
from contextlib import suppress
from datetime import datetime

from sqlalchemy import delete, select, text

from support_agent.contracts import Item
from support_agent.db.models import Group, GroupObservation, Metric, Need, Observation, Revision, Ticket
from support_agent.errors import Budget, InvalidOutput, Stale
from support_agent.policy import digest, mask, parse_extraction, parse_match
from support_agent.services.state import audit, invalidate, rebuild_memberships
from support_agent.settings import ROOT

PROMPT_VERSION = "extraction-v1+matching-v1"


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
    words = [item.need, item.context]
    for term, synonyms in json.loads((ROOT / "config/terms.json").read_text()).items():
        if any(x.lower() in item.need.lower() for x in synonyms):
            words.extend([term] + synonyms)
    with app.db.tx([p]) as s:
        from support_agent.catalog import product_terms

        for term, description in list(product_terms(s, p).items())[:30]:
            if term.casefold() in (item.need + " " + item.context).casefold():
                words.extend([term, description])
        query = " ".join(words)[:2000]
        rows = (
            s.execute(
                text("""
                SELECT id, need, context,
                  ts_rank(to_tsvector('russian', need || ' ' || context), plainto_tsquery('russian', :q))
                    + similarity(need, :need) AS score
                FROM groups WHERE product_id=:p AND active AND NOT provisional AND kind=:kind
                  AND (to_tsvector('russian', need || ' ' || context) @@ plainto_tsquery('russian', :q)
                       OR need % :need)
                ORDER BY score DESC, id LIMIT :k
            """),
                {"q": query, "need": item.need, "p": p, "kind": item.kind, "k": app.settings.candidate_limit},
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows[: app.settings.matching_limit]]


def combined_item(items, product):
    """One contribution per ticket/group; preserve conservative review flags and evidence."""
    first = items[0]
    evidence, seen = [], set()
    for item in items:
        for quote in item.evidence:
            key = quote.segment_id, quote.quote
            if key not in seen:
                evidence.append(quote.model_dump())
                seen.add(key)
    urgency_rank = {"critical": 0, "high": 1, "normal": 2, "unknown": 3}
    reasons = list(dict.fromkeys(item.review_reason for item in items if item.review_reason))
    return {
        "kind": first.kind,
        "need": mask(first.need, product),
        "context": mask(first.context, product),
        "urgency": min((item.urgency for item in items), key=urgency_rank.__getitem__),
        "evidence": evidence[:3],
        "needs_review": any(item.needs_review for item in items),
        "review_reason": mask("; ".join(reasons), product)[:300] or None,
    }


async def _validated_extraction(app, c, segments):
    """Save a successful extraction before contending for the assignment lease."""
    with app.db.tx([c.product_id]) as s:
        app.queue.guard(s, c)
        revision = s.get(Revision, (c.product_id, c.revision_id))
        compatible = revision.model_id == app.models.model_id and revision.prompt_version == PROMPT_VERSION
        if compatible and revision.extracted_items is not None:
            return [Item.model_validate_json(json.dumps(item)) for item in revision.extracted_items]
    items = []
    for part in chunks(segments, app.settings.context_chars):
        raw = await app.model_call(c, "llm", lambda: app.models.extract(part))
        try:
            parsed = parse_extraction(raw, part, app.settings.max_items)
        except InvalidOutput:
            with app.db.tx([c.product_id]) as s:
                app.queue.guard(s, c)
                s.add(
                    Metric(product_id=c.product_id, name="invalid_json", value=1, created_at=app.clock.now())
                )
            raw = await app.model_call(c, "llm", lambda: app.models.extract(part, repair=True))
            parsed = parse_extraction(raw, part, app.settings.max_items)
        items.extend(parsed.items)
        if len(items) > app.settings.max_items:
            raise Budget()
    with app.db.tx([c.product_id]) as s:
        app.queue.guard(s, c)
        revision = s.get(Revision, (c.product_id, c.revision_id))
        revision.extracted_items = [item.model_dump() for item in items]
        revision.model_id, revision.prompt_version = app.models.model_id, PROMPT_VERSION
    return items


async def run_analysis(app, c):
    p = c.product_id
    with app.db.tx([p]) as s:
        app.queue.guard(s, c)
        r = s.get(Revision, (p, c.revision_id))
        # A committed analysis is the durable idempotency boundary, including empty/provisional output.
        if (
            r.processed_at is not None
            and r.model_id == app.models.model_id
            and r.prompt_version == PROMPT_VERSION
            and r.extracted_items is not None
        ):
            app.queue.complete(s, c)
            return
        segments, complete = r.segments or [], r.coverage.get("complete", False)
        ticket_id = r.ticket_id
    items = await _validated_extraction(app, c, segments)
    token = app.queue.acquire_product(p)
    assignments, local, identical = [], [], {}

    async def renew_assignment():
        while True:
            await asyncio.sleep(app.settings.lease_seconds / 3)
            app.queue.renew_product(p, token)

    renewal = asyncio.create_task(renew_assignment())
    try:
        for item in items:
            if renewal.done():
                renewal.result()
            # Identical needs in separate chunks still represent one contribution and one match call.
            signature = (item.kind, item.need, item.context)
            if signature in identical:
                gid, provisional, match_reason = identical[signature]
                assignments.append((item, gid, provisional, match_reason))
                continue
            candidates = find_similar_groups(app, p, item)
            seen = {candidate["id"] for candidate in candidates}
            candidates = (
                candidates
                + [
                    candidate
                    for candidate in local
                    if candidate["id"] not in seen and candidate["kind"] == item.kind
                ]
            )[: app.settings.matching_limit]
            lookup = {row["segment_id"]: row for row in segments}
            match_segments = [dict(lookup[quote.segment_id], text=quote.quote) for quote in item.evidence]
            call = asyncio.create_task(
                app.model_call(c, "llm", lambda: app.models.match(item, candidates, match_segments))
            )
            try:
                done, _ = await asyncio.wait([call, renewal], return_when=asyncio.FIRST_COMPLETED)
                if call not in done:
                    renewal.result()
                    raise Stale()
                raw = call.result()
            finally:
                if not call.done():
                    call.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await call
            match = parse_match(raw, candidates, match_segments)
            if match.action == "match":
                gid = match.candidate_id
            else:
                from uuid import uuid4

                gid = str(uuid4())
                local.append({"id": gid, "need": item.need, "context": item.context, "kind": item.kind})
            provisional = match.action == "uncertain"
            identical[signature] = (gid, provisional, match.reason if provisional else None)
            assignments.append((item, gid, provisional, match.reason if provisional else None))
        with app.db.tx([p]) as s:
            app.queue.product_guard(s, p, token)
            app.queue.guard(s, c)
            r = s.get(Revision, (p, c.revision_id))
            t = s.get(Ticket, (p, ticket_id))
            s.execute(delete(Need).where(Need.product_id == p, Need.revision_id == r.id))
            s.flush()
            grouped = {}
            for item, gid, provisional, match_reason in assignments:
                entry = grouped.setdefault(gid, {"items": [], "provisional": False, "reasons": []})
                entry["items"].append(item)
                entry["provisional"] |= provisional
                if match_reason:
                    entry["reasons"].append(match_reason)
            for gid, entry in grouped.items():
                value = combined_item(entry["items"], p)
                provisional = entry["provisional"]
                if provisional:
                    value["needs_review"] = True
                    value["review_reason"] = (
                        mask("; ".join(filter(None, [value["review_reason"], *entry["reasons"]])), p)[:300]
                        or "uncertain_group_match"
                    )
                g = s.get(Group, (p, gid))
                if g and g.kind != value["kind"]:
                    raise InvalidOutput()
                observation_hash = digest([value["kind"], entry["items"][0].need])
                observed = s.scalar(
                    select(Observation).where(
                        Observation.product_id == p,
                        Observation.ticket_id == t.id,
                        Observation.need_hash == observation_hash,
                    )
                )
                source_time = datetime.fromisoformat(r.raw["occurred_at"]) if r.raw else t.occurred_at
                first = observed.first_seen if observed else min(source_time, r.created_at)
                if not g:
                    g = Group(
                        product_id=p,
                        id=gid,
                        kind=value["kind"],
                        need=value["need"],
                        context=value["context"],
                        first_seen=first,
                        provisional=provisional or value["needs_review"],
                        active=True,
                    )
                    s.add(g)
                    s.flush()
                group_observed = s.scalar(
                    select(GroupObservation).where(
                        GroupObservation.product_id == p,
                        GroupObservation.ticket_id == t.id,
                        GroupObservation.group_id == gid,
                    )
                )
                first = min(first, group_observed.first_seen) if group_observed else first
                if not observed:
                    s.add(
                        Observation(
                            product_id=p, ticket_id=t.id, need_hash=observation_hash, first_seen=first
                        )
                    )
                if not group_observed:
                    s.add(GroupObservation(product_id=p, ticket_id=t.id, group_id=gid, first_seen=first))
                s.add(
                    Need(
                        product_id=p,
                        id=f"{r.id}:{digest([gid])[:20]}",
                        revision_id=r.id,
                        group_id=gid,
                        ticket_id=t.id,
                        origin_id=t.origin_id,
                        first_seen=first,
                        eligible=complete and not value["needs_review"] and not provisional,
                        **value,
                    )
                )
            r.status = "processed" if complete else "partial"
            if any(
                entry["provisional"] or any(item.needs_review for item in entry["items"])
                for entry in grouped.values()
            ):
                r.status = "needs_review"
            r.model_id, r.prompt_version, r.processed_at = (
                app.models.model_id,
                PROMPT_VERSION,
                app.clock.now(),
            )
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
        with suppress(asyncio.CancelledError, Exception):
            await renewal
        app.queue.release_product(p, token)
