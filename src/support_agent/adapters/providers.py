import base64
import json
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse, quote

import httpx
from support_agent.contracts import ChangeBatch, TicketContent, Extraction, Match
from support_agent.errors import Permanent, Transient, DeliveryUnknown
from support_agent.policy import digest
from support_agent.settings import ROOT


class FixtureSource:
    source_id = "demo"

    def __init__(self, root, product):
        self.root, self.product = Path(root), product

    async def list_changes(self, cursor, limit):
        path = self.root / self.product / "changes.jsonl"
        rows = [json.loads(x) for x in path.read_text().splitlines() if x]
        rows = [r for r in rows if r["sequence"] > int(cursor or "0")]
        batch = rows[:limit]
        return ChangeBatch.model_validate_json(
            json.dumps(
                {
                    "changes": batch,
                    "next_cursor": str(batch[-1]["sequence"]) if batch else (cursor or "0"),
                    "has_more": len(rows) > limit,
                }
            )
        )

    async def read_ticket(self, ticket_id, version):
        rows = json.loads((self.root / self.product / "tickets.json").read_text())
        try:
            return TicketContent.model_validate_json(json.dumps(rows[f"{ticket_id}:{version}"]))
        except KeyError:
            raise Permanent() from None


class DemoModels:
    model_id = "fixture-provider (synthetic; no Qwen inference)"

    def __init__(self, root):
        self.answers = json.loads((Path(root) / "model-responses.json").read_text())
        self.media = json.loads((Path(root) / "media-responses.json").read_text())

    async def extract(self, segments, repair=False):
        key = digest([s["text"] for s in segments])
        if key not in self.answers:
            raise Permanent()
        return json.dumps(self.answers[key], ensure_ascii=False), {}

    async def match(self, item, candidates, segments):
        # Finite fixture vocabulary only: this is never a production classifier.
        known = {i["need"] for v in self.answers.values() for i in v["items"]}
        if item.need not in known:
            raise Permanent()
        found = next((c for c in candidates if c["need"] == item.need), None)
        return json.dumps(
            {
                "action": "match" if found else ("uncertain" if item.needs_review else "new"),
                "candidate_id": found["id"] if found else None,
                "reason": "Controlled synthetic fixture",
                "evidence": [e.model_dump() for e in item.evidence],
            }
        ), {}

    async def recognize(self, data, media_type, page=None):
        key = digest([base64.b64encode(data).decode(), page])
        if key not in self.media:
            raise Permanent()
        return self.media[key]


class Gateway:
    """Documented integration gateway, not a claimed corporate API. No implicit retries."""

    source_id = "gateway"
    model_id = "Qwen 3.8"

    def __init__(self, settings, *, allow_loopback_for_test=False):
        self.settings = settings
        self.base = (settings.gateway_url or "").rstrip("/")
        u = urlparse(self.base)
        test = (
            allow_loopback_for_test
            and settings.app_env == "local"
            and u.hostname in ("127.0.0.1", "localhost")
        )
        if not test and (u.scheme != "https" or u.hostname not in settings.allowed_hosts):
            raise Permanent()
        verify = ssl.create_default_context(cafile=settings.internal_ca) if settings.internal_ca else True
        self.client = httpx.AsyncClient(
            base_url=self.base,
            verify=verify,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
            headers={"Authorization": f"Bearer {settings.gateway_token}"},
            trust_env=False,
        )

    async def request(self, method, path, *, payload=None, raw=False, headers=None):
        if not path.startswith("/") or path.startswith("//") or "://" in path:
            raise Permanent()
        try:
            async with self.client.stream(method, path, json=payload, headers=headers) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    retry = response.headers.get("Retry-After", "0")
                    try:
                        retry = float(retry)
                    except ValueError:
                        try:
                            retry = max(
                                0, (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                            )
                        except (ValueError, TypeError):
                            retry = 0
                    raise Transient(retry)
                if response.status_code >= 300:
                    raise Permanent()
                data = bytearray()
                async for part in response.aiter_bytes():
                    data.extend(part)
                    if len(data) > self.settings.max_file_bytes:
                        raise Permanent()
                if raw:
                    return bytes(data)
                try:
                    return json.loads(data)
                except (ValueError, UnicodeError):
                    raise Permanent() from None
        except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError):
            raise Transient() from None

    async def list_changes(self, product, cursor, limit):
        r = await self.request(
            "POST",
            f"/v1/products/{quote(product, safe='')}/changes",
            payload={"cursor": cursor, "limit": limit},
        )
        try:
            return ChangeBatch.model_validate_json(json.dumps(r))
        except ValueError:
            raise Permanent() from None

    async def read_ticket(self, product, ticket, version):
        r = await self.request(
            "POST",
            f"/v1/products/{quote(product, safe='')}/tickets/read",
            payload={"ticket_id": ticket, "version": version},
        )
        try:
            result = TicketContent.model_validate_json(json.dumps(r))
        except ValueError:
            raise Permanent() from None
        if result.external_id != ticket or result.version != version:
            raise Permanent()
        return result

    async def attachment(self, product, ref):
        return await self.request(
            "POST",
            f"/v1/products/{quote(product, safe='')}/attachments/read",
            payload={"content_ref": ref},
            raw=True,
        )

    async def extract(self, segments, repair=False):
        r = await self.request(
            "POST",
            "/v1/models/extract",
            payload={
                "model": self.model_id,
                "prompt_version": "extraction-v1",
                "system": (ROOT / "prompts/extraction-v1.txt").read_text(),
                "segments": segments,
                "schema": Extraction.model_json_schema(),
                "repair": repair,
                "max_output_tokens": 3000,
            },
        )
        return self.model_result(r)

    async def match(self, item, candidates, segments):
        r = await self.request(
            "POST",
            "/v1/models/match",
            payload={
                "model": self.model_id,
                "prompt_version": "matching-v1",
                "system": (ROOT / "prompts/matching-v1.txt").read_text(),
                "item": item.model_dump(),
                "candidates": candidates,
                "segments": segments,
                "schema": Match.model_json_schema(),
                "max_output_tokens": 800,
            },
        )
        return self.model_result(r)

    @staticmethod
    def model_result(r):
        if not isinstance(r, dict) or not isinstance(r.get("output_json"), str):
            raise Permanent()
        usage = r.get("usage", {})
        if not isinstance(usage, dict) or any(
            k not in ("input_tokens", "output_tokens") or type(v) is not int or v < 0
            for k, v in usage.items()
        ):
            raise Permanent()
        return r["output_json"], usage

    async def recognize(self, data, media_type, page=None):
        r = await self.request(
            "POST",
            "/v1/models/media",
            payload={
                "model": "Qwen-ASR" if media_type == "audio" else "Qwen-VL",
                "data_base64": base64.b64encode(data).decode(),
                "media_type": media_type,
                "page": page,
            },
        )
        if not isinstance(r, list):
            raise Permanent()
        return r

    async def deliver(self, product, key, payload):
        if self.settings.backlog_lookup:
            r = await self.request("POST", "/v1/backlog/lookup", payload={"product_id": product, "key": key})
            if r.get("external_id"):
                return str(r["external_id"])
        try:
            r = await self.request(
                "POST",
                "/v1/backlog/cards",
                payload={"product_id": product, "correlation_key": key, "card": payload},
                headers={"Idempotency-Key": key} if self.settings.backlog_idempotent else {},
            )
        except Transient:
            if not self.settings.backlog_idempotent and not self.settings.backlog_lookup:
                raise DeliveryUnknown() from None
            raise
        if not isinstance(r, dict) or not isinstance(r.get("external_id"), str):
            raise DeliveryUnknown()
        return r["external_id"][:100]

    async def close(self):
        await self.client.aclose()


class BoundSource:
    def __init__(self, gateway, product):
        self.gateway, self.product, self.source_id = gateway, product, gateway.source_id

    async def list_changes(self, cursor, limit):
        return await self.gateway.list_changes(self.product, cursor, limit)

    async def read_ticket(self, ticket_id, version):
        return await self.gateway.read_ticket(self.product, ticket_id, version)
