"""Generate the checked-in contracts/OpenAPI, or fail CI when exports drift."""

import argparse
import asyncio
import json
from pathlib import Path

from support_agent.application import Application
from support_agent.api import create_app
from support_agent.settings import Settings
from support_agent.provisioning import ProvisionSpec
from support_agent.contracts import (
    ChangeBatch,
    Extraction,
    Match,
    TicketContent,
    Segment,
    DecisionRequest,
    DeliveryResolutionRequest,
)

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    app = Application(Settings(_env_file=None))
    try:
        schemas = {
            cls.__name__ + ".json": cls.model_json_schema()
            for cls in (
                ChangeBatch,
                Extraction,
                Match,
                TicketContent,
                Segment,
                DecisionRequest,
                DeliveryResolutionRequest,
                ProvisionSpec,
            )
        }
        schemas["openapi.json"] = create_app(app).openapi()
    finally:
        asyncio.run(app.close())
    directory = ROOT / "docs/schemas"
    directory.mkdir(exist_ok=True)
    drift = []
    for filename, value in schemas.items():
        path = directory / filename
        content = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != content:
                drift.append(filename)
        else:
            path.write_text(content)
    if drift:
        raise SystemExit("Schema exports drift: " + ", ".join(drift) + "; run scripts/export_schemas.py")
    print("Schema exports match contracts" if args.check else f"Exported {len(schemas)} schemas")


if __name__ == "__main__":
    main()
