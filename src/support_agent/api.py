import logging
import re
from contextlib import asynccontextmanager
from uuid import uuid4
from typing import Annotated

from fastapi import FastAPI, Request, Depends, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select

from support_agent.application import Application
from support_agent.settings import get_settings
from support_agent.auth import authenticate
from support_agent.contracts import DecisionRequest
from support_agent.db.models import Group, Card, Revision, Job, Digest, Ticket
from support_agent.errors import AgentError, NotFound, Conflict
from support_agent.services.delivery.service import record_product_decision
from support_agent.services.state import retention_preview, audit
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
        size, parts = 0, []
        async for chunk in request.stream():
            size += len(chunk)
            if size > 65536:
                return JSONResponse({"error": "request_too_large", "request_id": rid}, status_code=413)
            parts.append(chunk)
        request._body = b"".join(parts)
        try:
            response = await call_next(request)
        except Exception:
            logging.getLogger("support_agent").error("request_failed request_id=%s", rid)
            response = JSONResponse({"error": "internal_error", "request_id": rid}, status_code=500)
        response.headers["X-Request-ID"] = rid
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @api.exception_handler(AgentError)
    async def domain_error(request, exc):
        return JSONResponse(
            {"error": exc.code, "request_id": request.state.request_id}, status_code=exc.status
        )

    @api.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            {"error": "invalid_request", "request_id": request.state.request_id}, status_code=422
        )

    def actor(authorization: Annotated[str | None, Header()] = None):
        return authenticate(appstate, authorization)

    User = Annotated[object, Depends(actor)]
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
    def groups(product: str, user: User, limit: Limit = 25, offset: Offset = 0):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            return [
                {
                    "id": g.id,
                    "kind": g.kind,
                    "need": g.need,
                    "context": g.context,
                    "revision": g.revision,
                    "active": g.active,
                    "provisional": g.provisional,
                }
                for g in s.scalars(select(Group).order_by(Group.id).limit(limit).offset(offset))
            ]

    @api.get("/products/{product}/cards")
    def cards(product: str, user: User, limit: Limit = 25, offset: Offset = 0):
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
    def card(product: str, card_id: str, user: User):
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
    def evidence(product: str, revision_id: str, user: User):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            r = s.get(Revision, (product, revision_id))
            if not r or r.status == "deleted" or not r.segments:
                raise NotFound()
            return {
                "revision_id": r.id,
                "segments": r.segments,
                "coverage": r.coverage,
                "masking_version": r.masking_version,
            }

    @api.get("/products/{product}/digest")
    def digest(product: str, user: User, period: Annotated[str, Query(pattern=r"^\d{4}-\d{2}-\d{2}$")]):
        user.require(product)
        with appstate.db.tx([product], user.id) as s:
            d = s.scalar(
                select(Digest).where(Digest.period == period).order_by(Digest.created_at.desc()).limit(1)
            )
            return {"period": period, "cards": d.snapshot if d else []}

    @api.post("/products/{product}/cards/{card_id}/decision")
    def decision(product: str, card_id: str, body: DecisionRequest, user: User):
        return record_product_decision(appstate, user, product, card_id, body)

    @api.get("/products/{product}/pipeline")
    def pipeline(product: str, user: User, limit: Limit = 25, offset: Offset = 0):
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
    def retry(product: str, job_id: str, user: User):
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
    def retention(product: str, user: User, dry_run: bool = True):
        user.require(product, "operator")
        with appstate.db.tx([product], user.id) as s:
            ids = retention_preview(s, product, appstate.clock.now(), appstate.settings.retention_days)
            if not dry_run:
                key = f"retention:{appstate.clock.now().isoformat()}"
                enqueue(s, product, "retention", key, appstate.clock.now())
                audit(s, product, "retention_requested", key, appstate.clock.now(), user.id)
            return {"dry_run": dry_run, "ticket_ids": ids, "queued": not dry_run}

    @api.get("/metrics")
    def metrics(user: User):
        permitted = [p for p, roles in user.permissions.items() if "operator" in roles]
        for p in user.products:
            user.require(p, "operator")
        if not permitted:
            from support_agent.errors import Forbidden

            raise Forbidden()
        return report(appstate, permitted)

    return api
