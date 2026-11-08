import asyncio
import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, DBAPIError
from fastapi.testclient import TestClient
from dotenv import dotenv_values

from support_agent.api import create_app
from support_agent.application import Application
from support_agent.db.session import Database
from support_agent.db.models import Group, Ticket, Need
from support_agent.services.ingest.service import ingest_once
from support_agent.services.delivery.service import build_digest
from support_agent.worker import drain


async def prepare(app):
    for p in ["atlas", "boreal"]:
        await ingest_once(app, p)
    await drain(app)
    build_digest(app, "atlas")
    build_digest(app, "boreal")


def test_api_authorization_direct_ids_spoofing_masking_limits(app):
    asyncio.run(prepare(app))
    # Exercise the actual non-owner API database role, including transaction-local scope.
    api_base = dotenv_values(".env")["API_DATABASE_URL"]
    api_url = make_url(api_base).set(database=make_url(app.settings.database_url).database)
    api_app = Application(app.settings, db=Database(api_url), clock=app.clock)
    try:
        with TestClient(create_app(api_app)) as client:
            viewer = {"Authorization": "Bearer " + app.tokens["viewer"]}
            operator = {"Authorization": "Bearer " + app.tokens["operator"]}
            reviewer = {"Authorization": "Bearer " + app.tokens["reviewer"]}
            boreal = {"Authorization": "Bearer " + app.tokens["boreal"]}
            assert client.get("/health/ready").status_code == 200
            assert client.get("/metrics").status_code == 403
            assert client.get("/products/atlas/cards").status_code == 403
            assert client.get("/products/boreal/cards", headers=viewer).status_code == 403
            foreign = client.get("/products/boreal/cards", headers=boreal).json()[0]
            assert client.get("/products/atlas/cards/" + foreign["id"], headers=viewer).status_code == 404
            assert client.get("/products/boreal/evidence/demo:t01:1", headers=viewer).status_code == 403
            data = client.get("/products/atlas/evidence/demo:t17:1", headers=viewer)
            assert data.status_code == 200
            for secret in ["demo.person@example.test", "DEMO_SECRET_123456", "+7 (999) 123-45-67"]:
                assert secret not in data.text
            own = client.get("/products/atlas/cards", headers=viewer).json()[0]
            payload = {
                "revision": own["revision"],
                "content_hash": own["content_hash"],
                "decision": "accepted",
                "reason": "review",
            }
            path = "/products/atlas/cards/" + own["id"] + "/decision"
            assert client.post(path, json=payload, headers=operator).status_code == 403
            assert (
                client.post(path, json=payload, headers=viewer | {"X-Role": "product_reviewer"}).status_code
                == 403
            )
            assert (
                client.post(path, json=payload | {"role": "product_reviewer"}, headers=reviewer).status_code
                == 422
            )
            assert client.post(path, json=payload | {"revision": 999}, headers=reviewer).status_code == 409
            assert client.post(path, json=payload, headers=reviewer).status_code == 200
            assert client.get("/products/atlas/cards?limit=1000", headers=viewer).status_code == 422
            assert client.post(path, content=b"x" * 70000, headers=reviewer).status_code == 413
            assert client.get("/metrics", headers=operator).status_code == 200
            assert client.get("/metrics", headers=viewer).status_code == 403
            # Viewer response never contains raw source fields or credentials.
            assert "DEMO_SECRET_123456" not in client.get("/products/atlas/cards", headers=viewer).text
            assert "evil.invalid" not in client.get("/products/atlas/cards", headers=viewer).text
    finally:
        api_app.db.close()


async def test_forced_rls_runtime_role_empty_scope_and_pool_reset(app):
    await ingest_once(app, "atlas")
    await ingest_once(app, "boreal")
    app.db.runtime_ready()
    with app.db.tx(["atlas"]) as s:
        assert set(s.scalars(select(Ticket.product_id))) == {"atlas"}
        role = s.execute(text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user")).one()
        assert role == (False, False)
        assert s.scalar(text("SELECT tableowner<>current_user FROM pg_tables WHERE tablename='tickets'"))
        with pytest.raises(DBAPIError):
            s.execute(
                text(
                    "INSERT INTO audit(product_id,id,event,actor,ref,created_at) VALUES ('boreal','forged','test','test','test',now())"
                )
            )
    # Failed transaction is rolled back by context, then reborrowed with no ambient scope.
    with app.db.tx() as s:
        assert s.scalars(select(Ticket)).all() == []
    with app.db.tx(["boreal"]) as s:
        assert set(s.scalars(select(Ticket.product_id))) == {"boreal"}
    with app.db.tx() as s:
        assert s.scalar(text("SELECT coalesce(current_setting('app.products',true),'')")) == ""


async def test_composite_foreign_keys_prevent_cross_product_links(app):
    await prepare(app)
    with app.db.tx(["atlas", "boreal"]) as s:
        foreign = s.scalar(select(Group).where(Group.product_id == "boreal"))
    with pytest.raises(IntegrityError):
        with app.db.tx(["atlas", "boreal"]) as s:
            s.add(
                Need(
                    product_id="atlas",
                    id="forged",
                    revision_id="demo:t01:1",
                    group_id=foreign.id,
                    ticket_id="demo:t01",
                    origin_id="demo:t01",
                    kind="feature_request",
                    need="forged",
                    context="",
                    urgency="unknown",
                    evidence=[],
                    eligible=True,
                    first_seen=app.clock.now(),
                )
            )
            s.flush()


def test_corporate_jwt_signature_issuer_audience_algorithm_and_server_roles(app, tmp_path):
    import jwt
    import time
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    from support_agent.auth import authenticate
    from support_agent.errors import Forbidden

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = tmp_path / "issuer.pem"
    public.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    )
    app.settings = app.settings.model_copy(
        update={
            "mode": "internal",
            "app_env": "internal",
            "jwt_public_key": str(public),
            "jwt_issuer": "https://issuer.test",
            "jwt_audience": "agent",
        }
    )
    claims = {
        "sub": "reviewer",
        "iss": "https://issuer.test",
        "aud": "agent",
        "iat": int(time.time()),
        "exp": int(time.time()) + 120,
        "role": "operator",
        "products": ["boreal"],
    }
    good = jwt.encode(claims, key, algorithm="RS256")
    actor = authenticate(app, "Bearer " + good)
    assert actor.products == ["atlas"] and actor.permissions["atlas"] == frozenset(["product_reviewer"])
    for token in [
        jwt.encode(claims | {"iss": "https://evil.test"}, key, algorithm="RS256"),
        jwt.encode(claims | {"aud": "other"}, key, algorithm="RS256"),
        jwt.encode(claims | {"exp": 1}, key, algorithm="RS256"),
        jwt.encode(claims, "not-a-real-key-but-longer-than-thirty-two-bytes", algorithm="HS256"),
        good[:-8] + "tampered",
    ]:
        with pytest.raises(Forbidden):
            authenticate(app, "Bearer " + token)
