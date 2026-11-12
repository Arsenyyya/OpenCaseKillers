"""Synthetic internal gateway contract, exercising HTTPS/JWT/DB workflow rather than Qwen quality."""

import asyncio
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
import socket
import ssl
import threading
import time
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from dotenv import dotenv_values
import httpx
import jwt
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from support_agent.adapters.providers import DemoModels, Gateway
from support_agent.api import create_app
from support_agent.application import Application
from support_agent.clock import FrozenClock
from support_agent.contracts import Item
from support_agent.db.models import Card, Cursor, Group, Job, Outbox, Principal, Receipt, Revision, Ticket
from support_agent.db.session import Database
from support_agent.db.testing import isolated_database
from support_agent.errors import Forbidden, Transient
from support_agent.provisioning import ProvisionSpec, provision_registry
from support_agent.services.delivery.service import build_digest
from support_agent.services.ingest.service import ingest_once
from support_agent.settings import ROOT, Settings
from support_agent.worker import drain, run_one

PRODUCT = "contract-product"
GATEWAY_CREDENTIAL = "synthetic-gateway-contract-credential"
ISSUER = "https://synthetic-issuer.invalid"
AUDIENCE = "support-agent-contract"
NOW = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def write_tls_material(directory):
    """Ephemeral test-only CA and localhost leaf; private key never enters project fixtures."""
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic contract test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")]))
        .issuer_name(ca.subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = directory / "ca.pem", directory / "server.pem", directory / "server.key"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_path, 0o600)
    return ca_path, cert_path, key_path


@pytest.fixture
def internal_contract(database_settings, tmp_path):
    with isolated_database(database_settings) as database:
        spec = ProvisionSpec.model_validate(
            {
                "schema_version": 1,
                "products": [
                    {
                        "id": PRODUCT,
                        "owner": "contract-reviewer",
                        "sources": ["gateway"],
                        "terms": {"CSV": "Выгрузка заказов"},
                    }
                ],
                "principals": [
                    {
                        "id": "contract-reviewer",
                        "bindings": [{"product_id": PRODUCT, "roles": ["product_reviewer"]}],
                    },
                    {"id": "contract-operator", "bindings": [{"product_id": PRODUCT, "roles": ["operator"]}]},
                ],
                "provider_slots": {"llm": 2, "asr": 1, "vl": 1},
            }
        )
        provision_registry(spec, database.database_admin_url, clock=FrozenClock(NOW))
        receiver = Database(database.database_url)
        models = DemoModels(database.fixtures_dir)
        source = json.loads((database.fixtures_dir / "atlas/tickets.json").read_text())
        tickets = {key: source[key] for key in ("t01:v1", "t02:v1")}
        ca, certificate, private_key = write_tls_material(tmp_path)
        jwt_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwt_public = tmp_path / "issuer.pem"
        jwt_public.write_bytes(
            jwt_key.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        state = {"requests": [], "created_receipts": 0, "drop_after_first_create": True}

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def respond(self, status, body):
                encoded = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_POST(self):
                if self.headers.get("Authorization") != f"Bearer {GATEWAY_CREDENTIAL}":
                    self.respond(403, {"error": "synthetic_service_identity_required"})
                    return
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                state["requests"].append((self.path, payload, self.headers.get("Idempotency-Key")))
                if self.path == f"/v1/products/{PRODUCT}/changes":
                    cursor = int(payload["cursor"] or "0")
                    remaining = [
                        {
                            "sequence": i,
                            "external_id": f"t{i:02}",
                            "version": "v1",
                            "operation": "upsert",
                            "changed_at": "2026-11-06T12:00:00Z",
                            "backfill": False,
                        }
                        for i in (1, 2)
                        if i > cursor
                    ]
                    selected = remaining[: payload["limit"]]
                    self.respond(
                        200,
                        {
                            "changes": selected,
                            "next_cursor": str(selected[-1]["sequence"]) if selected else str(cursor),
                            "has_more": len(remaining) > len(selected),
                        },
                    )
                elif self.path == f"/v1/products/{PRODUCT}/tickets/read":
                    ticket = tickets.get(f"{payload['ticket_id']}:{payload['version']}")
                    self.respond(200 if ticket else 404, ticket or {"error": "unknown_synthetic_ticket"})
                elif self.path == "/v1/models/extract":
                    output, usage = asyncio.run(models.extract(payload["segments"], payload["repair"]))
                    self.respond(200, {"output_json": output, "usage": usage})
                elif self.path == "/v1/models/match":
                    output, usage = asyncio.run(
                        models.match(
                            Item.model_validate(payload["item"]),
                            payload["candidates"],
                            payload["segments"],
                        )
                    )
                    self.respond(200, {"output_json": output, "usage": usage})
                elif self.path == "/v1/backlog/lookup":
                    if payload["product_id"] != PRODUCT:
                        self.respond(403, {"error": "wrong_scope"})
                        return
                    with receiver.tx([PRODUCT]) as session:
                        receipt = session.scalar(
                            select(Receipt).where(Receipt.correlation_key == payload["key"])
                        )
                        self.respond(200, {"external_id": receipt.id if receipt else None})
                elif self.path == "/v1/backlog/cards":
                    if (
                        payload["product_id"] != PRODUCT
                        or self.headers.get("Idempotency-Key") != payload["correlation_key"]
                    ):
                        self.respond(403, {"error": "scope_or_idempotency_contract"})
                        return
                    with receiver.tx([PRODUCT]) as session:
                        inserted = session.scalar(
                            insert(Receipt)
                            .values(
                                product_id=PRODUCT,
                                id=str(uuid4()),
                                correlation_key=payload["correlation_key"],
                                payload=payload["card"],
                                created_at=NOW,
                            )
                            .on_conflict_do_nothing(index_elements=["correlation_key"])
                            .returning(Receipt.id)
                        )
                        receipt = session.scalar(
                            select(Receipt).where(
                                Receipt.correlation_key == payload["correlation_key"],
                            )
                        )
                        external_id = receipt.id
                    state["created_receipts"] += int(inserted is not None)
                    if state["drop_after_first_create"]:
                        state["drop_after_first_create"] = False
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    self.respond(200, {"external_id": external_id})
                else:
                    self.respond(404, {"error": "outside_synthetic_contract"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate, private_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        settings = Settings(
            _env_file=None,
            app_env="internal",
            mode="internal",
            delivery_mode="reviewed",
            database_url=database.database_url,
            database_admin_url=None,
            service_products=[PRODUCT],
            gateway_url=f"https://127.0.0.1:{server.server_port}",
            gateway_token=GATEWAY_CREDENTIAL,
            allowed_hosts=["127.0.0.1"],
            internal_ca=str(ca),
            jwt_issuer=ISSUER,
            jwt_audience=AUDIENCE,
            jwt_public_key=str(jwt_public),
            extraction_model="synthetic-contract-fixture",
            backlog_idempotent=True,
            backlog_lookup=True,
            batch_size=1,
            token_file=tmp_path / "never-created-demo-tokens.json",
        )
        try:
            yield settings, state, jwt_key
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            receiver.close()


def signed_token(key, actor="contract-reviewer", **changes):
    claims = {
        "sub": actor,
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + 300,
        "role": "untrusted-claim",
        "products": ["foreign-product"],
    }
    return jwt.encode(claims | changes, key, algorithm="RS256")


async def test_internal_https_jwt_provisioned_pipeline_and_durable_delivery_replay(internal_contract, caplog):
    settings, state, jwt_key = internal_contract
    clock = FrozenClock(NOW)
    app = Application(settings, clock=clock)
    api = None
    try:
        app.db.runtime_ready()
        assert isinstance(app.models, Gateway) and app.models is app.gateway
        with app.db.tx([PRODUCT]) as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 0
            assert session.get(Cursor, (PRODUCT, "gateway")).position == "0"
        with app.db.tx() as session:
            principals = list(session.scalars(select(Principal)))
            assert len(principals) == 2 and all(p.token_hash is None for p in principals)
        result = await ingest_once(app, PRODUCT)
        assert result == {"accepted": 2, "pages": 2}
        await drain(app)
        digest = build_digest(app, PRODUCT)
        assert len(digest["cards"]) == 1
        with app.db.tx([PRODUCT]) as session:
            assert session.scalar(select(func.count()).select_from(Group)) == 1
            card = session.scalar(select(Card))
            assert card.snapshot["independent_origins"] == 2 and len(card.snapshot["sources"]) == 2
            assert all(
                r.status == "processed" and r.model_id == "synthetic-contract-fixture"
                for r in session.scalars(select(Revision))
            )
        api_base = dotenv_values(ROOT / ".env")["API_DATABASE_URL"]
        api_url = make_url(api_base).set(database=make_url(settings.database_url).database)
        api = Application(settings.model_copy(update={"database_url": api_url}), clock=clock)
        api.db.runtime_ready()
        routes = create_app(api)
        decision_path = f"/products/{PRODUCT}/cards/{card.id}/decision"
        decision = {
            "revision": card.revision,
            "content_hash": card.content_hash,
            "decision": "accepted",
            "reason": "Synthetic transport contract approval",
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=routes), base_url="http://api.test"
        ) as client:
            denied = await client.post(
                decision_path,
                json=decision,
                headers={
                    "Authorization": "Bearer "
                    + signed_token(jwt_key, "contract-operator", role="product_reviewer"),
                },
            )
            assert denied.status_code == 403
            wrong_audience = await client.post(
                decision_path,
                json=decision,
                headers={
                    "Authorization": "Bearer " + signed_token(jwt_key, aud="foreign-audience"),
                },
            )
            assert wrong_audience.status_code == 403
            approved = await client.post(
                decision_path, json=decision, headers={"Authorization": "Bearer " + signed_token(jwt_key)}
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["status"] == "accepted"
        assert await run_one(app, "delivery")
        with app.db.tx([PRODUCT]) as session:
            assert session.scalar(select(Outbox.status)) == "sending"
            assert session.scalar(select(func.count()).select_from(Receipt)) == 1
        clock.value += timedelta(seconds=10)
        assert await run_one(app, "delivery")
        with app.db.tx([PRODUCT]) as session:
            assert session.scalar(select(Outbox.status)) == "delivered"
            assert session.scalar(select(Outbox.external_id)) == session.scalar(select(Receipt.id))
            assert session.scalar(select(Card.status)) == "delivered"
        await app.close()
        app = Application(settings, clock=clock)
        assert (await ingest_once(app, PRODUCT))["accepted"] == 0
        assert await drain(app) == 0
        replay = build_digest(app, PRODUCT)
        assert replay["cards"] == digest["cards"]
        with app.db.tx([PRODUCT]) as session:
            assert session.scalar(select(func.count()).select_from(Ticket)) == 2
            assert session.scalar(select(func.count()).select_from(Card)) == 1
            assert session.scalar(select(func.count()).select_from(Receipt)) == 1
            assert session.scalar(select(func.count()).select_from(Outbox)) == 1
            assert all(j.status == "succeeded" for j in session.scalars(select(Job)))
        assert state["created_receipts"] == 1
        creates = [r for r in state["requests"] if r[0] == "/v1/backlog/cards"]
        assert len(creates) == 1 and creates[0][2]
        assert len([r for r in state["requests"] if r[0] == "/v1/models/extract"]) == 2
        assert len([r for r in state["requests"] if r[0] == "/v1/models/match"]) == 2
        windows = {r[1]["initial_since"] for r in state["requests"] if r[0].endswith("/changes")}
        assert windows == {(NOW - timedelta(days=30)).isoformat()}
        before = len(state["requests"])
        with pytest.raises(Forbidden):
            await ingest_once(app, "atlas")
        assert len(state["requests"]) == before
        assert not settings.token_file.exists()
        assert GATEWAY_CREDENTIAL not in caplog.text
    finally:
        if api:
            await api.close()
        await app.close()


async def test_internal_https_rejects_untrusted_ca_and_never_falls_back(internal_contract):
    settings, state, _jwt_key = internal_contract
    with pytest.raises(ValidationError):
        Settings.model_validate(settings.model_dump() | {"allowed_hosts": ["other.invalid"]})
    with pytest.raises(ValidationError):
        Settings.model_validate(
            settings.model_dump() | {"gateway_url": settings.gateway_url.replace("https:", "http:")}
        )
    untrusted = Settings.model_validate(settings.model_dump() | {"internal_ca": None})
    app = Application(untrusted, clock=FrozenClock(NOW))
    try:
        assert isinstance(app.models, Gateway)
        with pytest.raises(Transient):
            await app.gateway.list_changes(PRODUCT, "0", 1)
        assert state["requests"] == []
        assert isinstance(app.models, Gateway)
    finally:
        await app.close()
