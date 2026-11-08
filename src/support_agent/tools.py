"""Typed, product-bound application tools. The LLM receives no write-tool registry."""

from dataclasses import dataclass
from typing import Literal
from support_agent.application import Application
from support_agent.auth import Actor
from support_agent.contracts import ChangeBatch, TicketContent, Item, DecisionRequest
from support_agent.db.models import Product
from support_agent.errors import Forbidden
from support_agent.services.analysis.worker import find_similar_groups
from support_agent.services.delivery.service import build_digest, record_product_decision


@dataclass(frozen=True)
class ExecutionContext:
    product_id: str
    actor: Actor | None = None


class ApplicationTools:
    """Scope comes from an authenticated request or configured service, never source text."""

    def __init__(self, app: Application, context: ExecutionContext):
        self.app, self.context = app, context
        if context.actor:
            context.actor.require(context.product_id)
        else:
            app.service_scope(context.product_id)

    async def list_ticket_changes(self, cursor: str, limit: int) -> ChangeBatch:
        if not 1 <= limit <= self.app.settings.batch_size:
            raise Forbidden()
        return await self.app.source(self.context.product_id).list_changes(cursor, limit)

    async def read_ticket_content(self, ticket_id: str, version: str) -> TicketContent:
        # Original content is a workflow operation, never available to unprivileged API viewers.
        if self.context.actor:
            raise Forbidden()
        return await self.app.source(self.context.product_id).read_ticket(ticket_id, version)

    def find_similar_groups(self, item: Item) -> list[dict]:
        return find_similar_groups(self.app, self.context.product_id, item)

    def read_product_context(self, keys: list[Literal["owner", "terms"]]) -> dict:
        if len(keys) > 2 or set(keys) - {"owner", "terms"}:
            raise Forbidden()
        with self.app.db.tx([self.context.product_id]) as s:
            product = s.get(Product, self.context.product_id)
            return {key: getattr(product, key) for key in keys}

    def build_digest(self, period: str | None = None) -> dict:
        if self.context.actor:
            self.context.actor.require(self.context.product_id, "operator")
        return build_digest(self.app, self.context.product_id, period)

    def record_product_decision(self, card_id: str, request: DecisionRequest) -> dict:
        if not self.context.actor:
            raise Forbidden()
        return record_product_decision(
            self.app, self.context.actor, self.context.product_id, card_id, request
        )
