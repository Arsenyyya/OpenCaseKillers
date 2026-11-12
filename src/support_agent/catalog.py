"""Small trusted product catalog. No inferred backlog relations or external URL fetching."""

from support_agent.db.models import Product


def lookup_known_task(session, product_id, kind, canonical_need):
    product = session.get(Product, product_id)
    if not product:
        return None
    canonical = canonical_need.strip().casefold()
    for task in product.terms.get("_known_tasks", []):
        if (
            isinstance(task, dict)
            and task.get("kind") == kind
            and str(task.get("need", "")).strip().casefold() == canonical
        ):
            return {key: task[key] for key in ("id", "status", "reference") if key in task}
    return None


def product_terms(session, product_id):
    product = session.get(Product, product_id)
    return {
        key: value
        for key, value in (product.terms if product else {}).items()
        if not key.startswith("_") and isinstance(value, str)
    }
