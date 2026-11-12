"""Administrative registry provisioning; no model calls, demo tokens or external backlog writes."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from support_agent.clock import Clock
from support_agent.db.models import Product, Principal, Grant, Cursor, ProductLease, ProviderSlot
from support_agent.db.session import Database
from support_agent.errors import ConfigurationError
from support_agent.policy import digest
from support_agent.services.state import audit, invalidate
from support_agent.settings import AdminSettings


class ProvisionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class BindingSpec(ProvisionModel):
    product_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    roles: list[Literal["viewer", "product_reviewer", "operator"]] = Field(min_length=1, max_length=3)


class PrincipalSpec(ProvisionModel):
    id: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    bindings: list[BindingSpec] = Field(default_factory=list, max_length=100)


class KnownTask(ProvisionModel):
    id: str = Field(min_length=1, max_length=100)
    kind: Literal["feature_request", "defect"]
    need: str = Field(min_length=3, max_length=500)
    status: str = Field(min_length=1, max_length=60)
    reference: str = Field(min_length=1, max_length=300)


class ProductSpec(ProvisionModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,60}$")
    owner: str = Field(min_length=1, max_length=100)
    terms: dict[str, str] = Field(default_factory=dict, max_length=100)
    known_tasks: list[KnownTask] = Field(default_factory=list, max_length=100)
    sources: list[Literal["gateway", "demo"]] = Field(
        default_factory=lambda: ["gateway"], min_length=1, max_length=2
    )

    @model_validator(mode="after")
    def bounded_catalog(self):
        if any(
            not key or len(key) > 100 or len(value) > 500 or key.startswith("_")
            for key, value in self.terms.items()
        ):
            raise ValueError("term catalog exceeds limits or uses reserved keys")
        if len(set(self.sources)) != len(self.sources):
            raise ValueError("duplicate source IDs")
        identities = [(task.kind, task.need.strip().casefold()) for task in self.known_tasks]
        if len(set(identities)) != len(identities):
            raise ValueError("duplicate canonical task mapping")
        return self


class ProvisionSpec(ProvisionModel):
    schema_version: Literal[1] = 1
    products: list[ProductSpec] = Field(min_length=1, max_length=100)
    principals: list[PrincipalSpec] = Field(min_length=1, max_length=5000)
    provider_slots: dict[Literal["llm", "asr", "vl"], int] = Field(
        default_factory=lambda: {"llm": 2, "asr": 2, "vl": 2}
    )

    @model_validator(mode="after")
    def consistent_registry(self):
        products = {p.id for p in self.products}
        principals = {p.id for p in self.principals}
        if len(products) != len(self.products) or len(principals) != len(self.principals):
            raise ValueError("duplicate product/principal IDs")
        if set(self.provider_slots) != {"llm", "asr", "vl"} or any(
            not 1 <= value <= 16 for value in self.provider_slots.values()
        ):
            raise ValueError("provider slots must specify llm/asr/vl with 1..16 slots each")
        for actor in self.principals:
            seen = set()
            for binding in actor.bindings:
                if (
                    binding.product_id not in products
                    or binding.product_id in seen
                    or len(set(binding.roles)) != len(binding.roles)
                ):
                    raise ValueError("invalid or repeated binding")
                seen.add(binding.product_id)
        by_actor = {actor.id: actor for actor in self.principals}
        for product in self.products:
            owner = by_actor.get(product.owner)
            if (
                not owner
                or not owner.enabled
                or not any(
                    b.product_id == product.id and "product_reviewer" in b.roles for b in owner.bindings
                )
            ):
                raise ValueError("each product owner must be an enabled reviewer in the declared registry")
        return self


def load_spec(path):
    path = Path(path)
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError()
        return ProvisionSpec.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ConfigurationError("invalid_registry_file") from None


def provision_registry(spec, admin_url, *, clock=None, dry_run=False):
    """Replace bindings for declared actors within declared products; preserve cursors and other scope."""
    products = [product.id for product in spec.products]
    summary = {
        "products": products,
        "principals": len(spec.principals),
        "bindings": sum(len(binding.roles) for actor in spec.principals for binding in actor.bindings),
        "provider_slots": spec.provider_slots,
        "dry_run": dry_run,
    }
    if dry_run:
        return summary
    if not admin_url:
        raise ConfigurationError("administrative_database_required")
    db, now = Database(admin_url), (clock or Clock()).now()
    try:
        with db.tx(products, actor="provisioning") as s:
            leases = {}
            for product_id in sorted(products):
                leases[product_id] = s.scalar(
                    select(ProductLease)
                    .where(ProductLease.product_id == product_id, ProductLease.id == "assignment")
                    .with_for_update()
                )
            slots = list(s.scalars(select(ProviderSlot).with_for_update()))
            expected = {
                f"{provider}:{i}" for provider, count in spec.provider_slots.items() for i in range(count)
            }
            if any(slot.id not in expected and slot.until and slot.until > now for slot in slots):
                raise ConfigurationError("capacity_has_active_leases")
            for actor in spec.principals:
                s.execute(
                    insert(Principal)
                    .values(id=actor.id, enabled=actor.enabled, token_hash=None)
                    .on_conflict_do_update(index_elements=["id"], set_={"enabled": actor.enabled})
                )
            for product in spec.products:
                row = s.get(Product, product.id)
                terms = dict(product.terms) | {
                    "_known_tasks": [task.model_dump() for task in product.known_tasks]
                }
                changed = row is not None and (row.owner != product.owner or row.terms != terms)
                if row:
                    row.owner, row.terms = product.owner, terms
                else:
                    s.add(Product(id=product.id, owner=product.owner, terms=terms))
                s.flush()
                for source in product.sources:
                    s.execute(
                        insert(Cursor)
                        .values(product_id=product.id, id=source, position="0", initialized_at=now)
                        .on_conflict_do_nothing()
                    )
                s.execute(
                    insert(ProductLease)
                    .values(product_id=product.id, id="assignment")
                    .on_conflict_do_nothing()
                )
                if changed:
                    lease = leases.get(product.id)
                    if lease:
                        lease.token, lease.until = None, None
                    invalidate(s, product.id, now)
                audit(s, product.id, "registry_provisioned", product.id, now, actor="provisioning")
            ids = [actor.id for actor in spec.principals]
            s.execute(delete(Grant).where(Grant.product_id.in_(products), Grant.actor.in_(ids)))
            for actor in spec.principals:
                for binding in actor.bindings:
                    for role in binding.roles:
                        s.add(
                            Grant(
                                product_id=binding.product_id,
                                id=digest([actor.id, role]),
                                actor=actor.id,
                                role=role,
                            )
                        )
            for slot in slots:
                if slot.id not in expected:
                    s.delete(slot)
            for provider, count in spec.provider_slots.items():
                for i in range(count):
                    s.execute(
                        insert(ProviderSlot)
                        .values(id=f"{provider}:{i}", provider=provider)
                        .on_conflict_do_nothing()
                    )
    finally:
        db.close()
    return summary


def provision_file(path, *, dry_run=False):
    spec = load_spec(path)
    if dry_run:
        return provision_registry(spec, None, dry_run=True)
    return provision_registry(spec, AdminSettings().database_admin_url)
