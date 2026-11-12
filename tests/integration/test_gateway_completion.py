import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from pydantic import ValidationError

from support_agent.adapters.providers import Gateway, DemoModels
from support_agent.contracts import TicketContent, Change, Segment
from support_agent.errors import DeliveryUnknown, Transient, MissingFixture, InvalidGatewayResponse, Budget
from support_agent.settings import Settings


@contextmanager
def responses(body, *, lookup=False):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            value = body
            data = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    cfg = Settings(_env_file=None).model_copy(
        update={
            "gateway_url": f"http://127.0.0.1:{server.server_port}",
            "gateway_token": "synthetic-only",
            "backlog_lookup": lookup,
        }
    )
    try:
        yield Gateway(cfg, allow_loopback_for_test=True), requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("body", [b"not-json", {"external_id": 12}, {"external_id": ""}])
async def test_uncertain_success_response_is_not_permanent_rejection(body):
    with responses(body) as (gateway, requests):
        try:
            with pytest.raises(DeliveryUnknown):
                await gateway.deliver("atlas", "key", {"need": "synthetic"})
            assert len(requests) == 1
        finally:
            await gateway.close()


async def test_malformed_lookup_never_creates_card():
    with responses({"unexpected": "field"}, lookup=True) as (gateway, requests):
        try:
            with pytest.raises(InvalidGatewayResponse):
                await gateway.deliver("atlas", "key", {})
            assert [path for path, _ in requests] == ["/v1/backlog/lookup"]
        finally:
            await gateway.close()


async def test_input_budget_is_enforced_before_http_and_demo_is_finite():
    with responses({"output_json": '{"items":[]}'}) as (gateway, requests):
        try:
            gateway.settings = gateway.settings.model_copy(update={"model_input_chars": 1000})
            with pytest.raises(Budget):
                await gateway.extract([{"segment_id": "s", "text": "x" * 2000}])
            assert not requests
        finally:
            await gateway.close()
    model = DemoModels(Settings(_env_file=None).fixtures_dir)
    with pytest.raises(MissingFixture):
        await model.extract([{"text": "unknown fixture text"}])


def test_contract_bounds_and_finite_retry_delays():
    ticket = {
        "external_id": "t",
        "version": "v",
        "text": "",
        "channel": "text",
        "occurred_at": "2026-11-06T12:00:00Z",
        "attachments": [
            {"id": "same", "content_ref": "x.png", "declared_type": "image"},
            {"id": "same", "content_ref": "y.png", "declared_type": "image"},
        ],
    }
    with pytest.raises(ValidationError):
        TicketContent.model_validate_json(json.dumps(ticket))
    with pytest.raises(ValidationError):
        Change.model_validate_json(
            json.dumps(
                {
                    "sequence": 1,
                    "external_id": "t",
                    "version": "v",
                    "operation": "upsert",
                    "changed_at": "2026-11-06T12:00:00",
                }
            )
        )
    for value in [float("nan"), float("inf"), -5, "invalid"]:
        assert Transient(value).retry_after == 0
    assert Transient(99999).retry_after == 3600
    for value in [float("nan"), float("inf"), 2, -1]:
        with pytest.raises(ValidationError):
            Segment(
                segment_id="s",
                source_ref="r",
                media_type="image",
                text="x",
                confidence=value,
                processor_version="v",
            )
