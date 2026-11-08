import hashlib
from dataclasses import dataclass
from pathlib import Path
import jwt
from sqlalchemy import select
from support_agent.db.models import Principal, Grant
from support_agent.errors import Forbidden


@dataclass(frozen=True)
class Actor:
    id: str
    permissions: dict[str, frozenset[str]]

    @property
    def products(self):
        return list(self.permissions)

    def require(self, product, role=None):
        roles = self.permissions.get(product, frozenset())
        if not roles or (role and role not in roles):
            raise Forbidden()


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(app, bearer):
    if not bearer or len(bearer) > 8192 or not bearer.startswith("Bearer "):
        raise Forbidden()
    token = bearer[7:]
    with app.db.tx() as s:
        if app.settings.app_env == "local" and app.settings.mode == "demo":
            principal = s.scalar(
                select(Principal).where(
                    Principal.token_hash == token_hash(token), Principal.enabled.is_(True)
                )
            )
        else:
            try:
                claims = jwt.decode(
                    token,
                    Path(app.settings.jwt_public_key).read_text(),
                    algorithms=["RS256"],
                    issuer=app.settings.jwt_issuer,
                    audience=app.settings.jwt_audience,
                    options={"require": ["sub", "exp", "iat", "iss", "aud"]},
                )
            except (jwt.PyJWTError, OSError, ValueError):
                raise Forbidden() from None
            principal = s.get(Principal, claims["sub"])
            if principal and not principal.enabled:
                principal = None
        if not principal:
            raise Forbidden()
        actor_id = principal.id
    # Only the verified principal determines server-side bindings; no role/product headers.
    with app.db.tx(actor=actor_id) as s:
        grants = s.scalars(select(Grant).where(Grant.actor == actor_id)).all()
        permissions = {}
        for g in grants:
            permissions.setdefault(g.product_id, set()).add(g.role)
        return Actor(actor_id, {p: frozenset(r) for p, r in permissions.items()})
