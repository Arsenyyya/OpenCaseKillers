"""Read-only local API smoke test. Credentials are loaded in memory and never printed."""

import argparse
import json
from pathlib import Path
import httpx

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--tokens", type=Path, default=Path(".local/tokens.json"))
    args = parser.parse_args()
    tokens = json.loads(args.tokens.read_text())
    with httpx.Client(base_url=args.url, timeout=5, trust_env=False) as client:
        assert client.get("/health/ready").status_code == 200
        headers = {"Authorization": "Bearer " + tokens["viewer"]}
        cards = client.get("/products/atlas/cards", headers=headers)
        assert cards.status_code == 200
        assert client.get("/products/boreal/cards", headers=headers).status_code == 403
        assert client.get("/metrics").status_code == 403
        print(
            json.dumps(
                {
                    "ready": True,
                    "atlas_cards": len(cards.json()),
                    "cross_product_denied": True,
                    "anonymous_metrics_denied": True,
                },
                indent=2,
            )
        )
