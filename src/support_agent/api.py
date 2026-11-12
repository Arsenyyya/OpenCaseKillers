import logging
import re
from contextlib import asynccontextmanager
from datetime import date
from uuid import uuid4
from typing import Annotated

from fastapi import FastAPI, Request, Depends, Header, Query, Path
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select, func, or_

from support_agent.application import Application
from support_agent.settings import get_settings
from support_agent.auth import authenticate
from support_agent.contracts import DecisionRequest, DeliveryResolutionRequest
from support_agent.db.models import (
    Group,
    Card,
    Revision,
    Job,
    Digest,
    Ticket,
    Need,
    Membership,
    Outbox,
    Metric,
)
from support_agent.errors import AgentError, NotFound, Conflict, Forbidden
from support_agent.services.delivery.service import record_product_decision, period_window
from support_agent.services.state import retention_plan, audit
from support_agent.queue import enqueue
from support_agent.observability.metrics import report


def create_app(application=None):
    owned = application is None
    appstate = application or Application(get_settings())

    @asynccontextmanager
    async def lifespan(api):
        appstate.db.runtime_ready()
        yield
        if owned:
            await appstate.close()

    api = FastAPI(title="Support Insight Agent", version="0.1.0", lifespan=lifespan)
    api.state.application = appstate

    @api.middleware("http")
    async def boundary(request: Request, call_next):
        rid = request.headers.get("X-Request-ID", "")
        if not re.fullmatch(r"[A-Za-z0-9-]{1,64}", rid):
            rid = str(uuid4())
        request.state.request_id = rid

        def limited_error(code, status):
            return JSONResponse(
                {"error": code, "request_id": rid},
                status_code=status,
                headers={
                    "X-Request-ID": rid,
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

        size, parts = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return limited_error("request_too_large", 413)
            parts.append(chunk)
        request._body = b"".join(parts)
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger("support_agent").error("request_failed request_id=%s", rid)
            response = JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)
        body = bytearray()
        async for part in response.body_iterator:
            body.extend(part)
            if len(body) > 2 * 1024 * 1024:
                await response.body_iterator.aclose()
                return limited_error("response_too_large", 413)
        response = Response(
            bytes(body),
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
            background=response.background,
        )
        response.headers["X-Request-ID"] = rid
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.exception_handler(AgentError)
    async def domain_error(request, exc):
        if isinstance(exc, Forbidden):
            logging.getLogger("support_agent").warning(
                "operation_denied request_id=%s", request.state.request_id
            )
            verified = getattr(request.state, "actor", None)
            if verified and verified.products:
                try:
                    with appstate.db.tx([verified.products[0]], verified.id) as s:
                        s.add(
                            Metric(
                                product_id=verified.products[0],
                                name="forbidden_operations",
                                value=1,
                                created_at=appstate.clock.now(),
                            )
                        )
                except Exception:
                    logging.getLogger("support_agent").error(
                        "denial_metric_failed request_id=%s", request.state.request_id
                    )
        return JSONResponse(
            {"error": exc.code, "request_id": request.state.request_id}, status_code=exc.status
        )

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {"error": "invalid_request", "request_id": request.state.request_id}, status_code=422
        )

    def actor(request: Request, authorization: Annotated[str | None, Header()] = None):
        verified = authenticate(appstate, authorization)
        request.state.actor = verified
        return verified

    User = Annotated[object, Depends(actor)]
    ProductPath = Annotated[str, Path(pattern=r"^[A-Za-z0-9_-]{1,60}$")]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Offset = Annotated[int, Query(ge=0, le=100000)]

    @api.get("/health/live")
    def live():
        return {"status": "alive"}

    @api.get("/health/ready")
    def ready():
        try:
            appstate.db.runtime_ready()
            if appstate.settings.mode == "demo" and not appstate.settings.fixtures_dir.is_dir():
                raise RuntimeError()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"error": "not_ready"}, status_code=503)

    @api.get("/products/{product}/groups")
    def groups(
        product: ProductPath,
        user: User,
        limit: Limit = 25,
        offset: Offset = 0,
        needs_review: bool | None = None,
    ):
        user.require(product)
        _, start, end = period_window(appstate)
        with appstate.db.tx([product], user.id) as s:
            review_exists = (
                select(Need.id)
                .join(Revision, (Revision.product_id == Need.product_id) & (Revision.id == Need.revision_id))
                .join(Ticket, (Ticket.product_id == Need.product_id) & (Ticket.id == Need.ticket_id))
                .where(
                    Need.product_id == product,
                    Need.group_id == Group.id,
                    Ticket.deleted.is_(False),
                    Revision.number == Ticket.current_revision,
                    Revision.status != "deleted",
                    or_(Need.needs_review.is_(True), Need.eligible.is_(False)),
                )
                .exists()
            )
            requires_review = or_(Group.provisional.is_(True), review_exists)
            query = select(Group, requires_review.label("requires_review")).where(Group.product_id == product)
            if needs_review is not None:
                query = query.where(requires_review if needs_review else ~requires_review)
            rows = list(s.execute(query.order_by(Group.id).limit(limit).offset(offset)))
            group_ids = [g.id for g, _ in rows]
            counts = dict(
                s.execute(
                    select(Membership.group_id, func.count())
                    .where(
                        Membership.product_id == product,
                        Membership.group_id.in_(group_ids),
                        Membership.first_seen >= start,
                        Membership.first_seen < end,
                    )
                    .group_by(Membership.group_id)
                ).all()
            )
            return [
                {
                    "id": g.id,
                    "kind": g.kind,
                    "need": g.need,
                    "context": g.context,
                    "revision": g.revision,
                    "active": g.active,
                    "provisional": g.provisional,
                    "requires_review": bool(review),
                    "independent_origins": counts.get(g.id, 0),
                }
                for g, review in rows
            ]

    @api.get("/products/{product}/groups/{group_id}")
    def group_detail(product: ProductPath, group_id: str, user: User, limit: Limit = 25, offset: Offset = 0):
        user.require(product)
        _, start, end = period_window(appstate)
        with appstate.db.tx([product], user.id) as s:
            group = s.get(Group, (product, group_id))
            if not group:
                raise NotFound()
            needs = list(
                s.scalars(
                    select(Need)
                    .join(
                        Revision, (Revision.product_id == Need.product_id) & (Revision.id == Need.revision_id)
                    )
                    .join(Ticket, (Ticket.product_id == Need.product_id) & (Ticket.id == Need.ticket_id))
                    .where(
                        Need.product_id == product,
                        Need.group_id == group.id,
                        Ticket.deleted.is_(False),
                        Revision.number == Ticket.current_revision,
                        Revision.status != "deleted",
                    )
                    .order_by(Need.first_seen, Need.id)
                    .limit(limit)
                    .offset(offset)
                )
            )
            count = s.scalar(
                select(func.count())
                .select_from(Membership)
                .where(
                    Membership.product_id == product,
                    Membership.group_id == group.id,
                    Membership.first_seen >= start,
                    Membership.first_seen < end,
                )
            )
            return {
                "id": group.id,
                "kind": group.kind,
                "need": group.need,
                "context": group.context,
                "revision": group.revision,
                "active": group.active,
                "provisional": group.provisional,
                "independent_origins": count,
                "limit": limit,
                "offset": offset,
                "needs": [
                    {
                        "ticket_id": n.ticket_id,
                        "revision_id": n.revision_id,
                        "eligible": n.eligible,
                        "needs_review": n.needs_review,
                        "review_reason": n.review_reason,
                        "evidence": n.evidence,
                    }
                    for n in needs
                ],
            }

    @api.get("/products/{product}/cards")
    def cards(product: ProductPath, user: User, limit: Limit = 25, offset: Offset = 0):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            return [
                {
                    "id": c.id,
                    "revision": c.revision,
                    "content_hash": c.content_hash,
                    "status": c.status,
                    "snapshot": c.snapshot,
                }
                for c in s.scalars(
                    select(Card).order_by(Card.created_at, Card.id).limit(limit).offset(offset)
                )
            ]

    @api.get("/products/{product}/cards/{card_id}")
    def card(product: ProductPath, card_id: str, user: User):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            c = s.get(Card, (product, card_id))
            if not c:
                raise NotFound()
            c.viewed_at = appstate.clock.now()
            return {
                "id": c.id,
                "revision": c.revision,
                "content_hash": c.content_hash,
                "status": c.status,
                "snapshot": c.snapshot,
            }

    @api.get("/products/{product}/evidence/{revision_id}")
    def evidence(
        product: ProductPath,
        revision_id: str,
        user: User,
        limit: Annotated[int, Query(ge=1, le=20)] = 10,
        offset: Offset = 0,
    ):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            r = s.get(Revision, (product, revision_id))
            if not r or r.status == "deleted" or not r.segments:
                raise NotFound()
            ticket = s.get(Ticket, (product, r.ticket_id))
            if not ticket or ticket.deleted or ticket.current_revision != r.number:
                raise NotFound()
            return {
                "revision_id": r.id,
                "segments": r.segments[offset : offset + limit],
                "total_segments": len(r.segments),
                "limit": limit,
                "offset": offset,
                "next_offset": offset + limit if offset + limit < len(r.segments) else None,
                "coverage": r.coverage,
                "masking_version": r.masking_version,
            }

    @api.get("/products/{product}/digest")
    def digest(product: ProductPath, user: User, period: Annotated[date, Query()]):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            d = s.scalar(
                select(Digest)
                .where(Digest.period == period.isoformat())
                .order_by(Digest.created_at.desc())
                .limit(1)
            )
            return {"period": period.isoformat(), "cards": d.snapshot if d else []}

    @api.post("/products/{product}/cards/{card_id}/decision")
    def decision(product: ProductPath, card_id: str, body: DecisionRequest, user: User):
        return record_product_decision(appstate, user, product, card_id, body)

    @api.get("/products/{product}/outbox")
    def outbox(product: ProductPath, user: User, limit: Limit = 25, offset: Offset = 0):
        user.require(product, "operator")
        with appstate.db.tx([product], user.id) as s:
            return [
                {
                    "id": o.id,
                    "card_id": o.card_id,
                    "revision": o.revision,
                    "status": o.status,
                    "correlation_key": o.correlation_key,
                    "external_id": o.external_id,
                    "sent_at": o.sent_at,
                    "resolved_at": o.resolved_at,
                    "resolved_by": o.resolved_by,
                }
                for o in s.scalars(
                    select(Outbox)
                    .where(Outbox.product_id == product)
                    .order_by(Outbox.id)
                    .limit(limit)
                    .offset(offset)
                )
            ]

    @api.post("/products/{product}/outbox/{outbox_id}/resolve")
    def resolve_outbox(product: ProductPath, outbox_id: str, body: DeliveryResolutionRequest, user: User):
        user.require(product, "operator")
        from support_agent.services.delivery.service import reconcile_delivery

        return reconcile_delivery(
            appstate, user, product, outbox_id, body.resolution, body.reason, body.external_id
        )

    @api.get("/products/{product}/pipeline")
    def pipeline(product: ProductPath, user: User, limit: Limit = 25, offset: Offset = 0):
        user.require(product, "operator")
        with appstate.db.tx([product], user.id) as s:
            return [
                {
                    "id": j.id,
                    "kind": j.kind,
                    "status": j.status,
                    "attempts": j.attempts,
                    "error_code": j.error_code,
                    "revision_id": j.revision_id,
                }
                for j in s.scalars(select(Job).order_by(Job.created_at, Job.id).limit(limit).offset(offset))
            ]

    @api.post("/products/{product}/jobs/{job_id}/retry")
    def retry(product: ProductPath, job_id: str, user: User):
        user.require(product, "operator")
        with appstate.db.tx([product], user.id) as s:
            j = s.scalar(select(Job).where(Job.id == job_id).with_for_update())
            if not j:
                raise NotFound()
            if j.status != "dead":
                raise Conflict()
            if j.revision_id:
                r = s.get(Revision, (product, j.revision_id))
                t = s.get(Ticket, (product, r.ticket_id))
                if (
                    t.deleted
                    or t.current_revision != r.number
                    or r.status == "deleted"
                    or r.model_calls >= appstate.settings.max_model_calls
                ):
                    raise Conflict()
            j.status, j.attempts, j.available_at = "queued", 0, appstate.clock.now()
            audit(s, product, "job_retried", j.id, appstate.clock.now(), user.id)
            return {"status": "queued"}

    @api.post("/products/{product}/retention")
    def retention(
        product: ProductPath,
        user: User,
        dry_run: bool = True,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Offset = 0,
    ):
        user.require(product, "operator")
        with appstate.db.tx([product], user.id) as s:
            plan = retention_plan(
                s, product, appstate.clock.now(), appstate.settings.retention_days, limit, offset
            )
            if not dry_run:
                if offset:
                    raise Conflict()
                key = f"retention:{appstate.clock.now().isoformat()}"
                enqueue(s, product, "retention", key, appstate.clock.now())
                audit(s, product, "retention_requested", key, appstate.clock.now(), user.id)
            return {"dry_run": dry_run, **plan, "queued": not dry_run}

    @api.get("/metrics")
    def metrics(user: User):
        permitted = [p for p, roles in user.permissions.items() if "operator" in roles]
        if not permitted:
            raise Forbidden()
        return report(appstate, permitted)

    return api
