"""Sanitized operational checks. No credentials, token values, prompts or provider payloads."""

from sqlalchemy import select
from support_agent.db.models import Product, Cursor, ProductLease, ProviderSlot
from support_agent.db.session import Database


def doctor(settings):
    source = "demo" if settings.mode == "demo" else "gateway"
    checks = []
    db = Database(settings.database_url)
    try:
        try:
            db.runtime_ready()
            checks.append({"component": "database_role_and_migrations", "ok": True})
        except Exception:
            checks.append(
                {
                    "component": "database_role_and_migrations",
                    "ok": False,
                    "hint": "Start PostgreSQL, run migrate with administrative credentials, then use a runtime role.",
                }
            )
            return {"ready": False, "mode": settings.mode, "checks": checks}
        with db.tx(settings.service_products) as s:
            for product in settings.service_products:
                ready = bool(
                    s.get(Product, product)
                    and s.get(Cursor, (product, source))
                    and s.get(ProductLease, (product, "assignment"))
                )
                checks.append(
                    {
                        "component": "product_registry",
                        "product_id": product,
                        "ok": ready,
                        "hint": "seed-demo for local demo; provision --config for an internal registry."
                        if not ready
                        else None,
                    }
                )
            slots = list(s.scalars(select(ProviderSlot)))
            for provider in ("llm", "asr", "vl"):
                count = sum(slot.provider == provider for slot in slots)
                checks.append(
                    {"component": "provider_capacity", "provider": provider, "slots": count, "ok": count > 0}
                )
        if settings.mode == "demo":
            ready = all(
                (settings.fixtures_dir / name).is_file()
                for name in ("manifest.json", "model-responses.json", "media-responses.json")
            )
            checks.append({"component": "demo_fixtures", "ok": ready})
        else:
            checks.append(
                {
                    "component": "internal_configuration",
                    "ok": True,
                    "connectivity": "not_probed",
                    "note": "Settings validated HTTPS/allowlist/JWT key; real endpoints and model quality remain unverified.",
                }
            )
        return {
            "ready": all(check["ok"] for check in checks),
            "mode": settings.mode,
            "delivery_mode": settings.delivery_mode,
            "checks": checks,
        }
    finally:
        db.close()
