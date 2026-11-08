"""Create local-only secrets. Never contacts production or replaces existing credentials."""

import json
import os
from pathlib import Path
from secrets import token_urlsafe

root = Path(__file__).resolve().parents[1]
env = root / ".env"
if not env.exists():
    owner, worker, api = [token_urlsafe(24) for _ in range(3)]
    env.write_text(f"""APP_ENV=local
MODE=demo
DELIVERY_MODE=shadow
POSTGRES_PASSWORD={owner}
WORKER_PASSWORD={worker}
API_PASSWORD={api}
DATABASE_ADMIN_URL=postgresql+psycopg://agent_owner:{owner}@127.0.0.1:55432/agent
DATABASE_URL=postgresql+psycopg://agent_worker:{worker}@127.0.0.1:55432/agent
API_DATABASE_URL=postgresql+psycopg://agent_api:{api}@127.0.0.1:55432/agent
""")
    os.chmod(env, 0o600)
local = root / ".local"
local.mkdir(exist_ok=True)
tokens = local / "tokens.json"
if not tokens.exists():
    tokens.write_text(
        json.dumps({n: token_urlsafe(32) for n in ["viewer", "reviewer", "operator", "boreal"]}, indent=2)
    )
    os.chmod(tokens, 0o600)
print("Local credentials ready: .env and .local/tokens.json (values hidden)")
