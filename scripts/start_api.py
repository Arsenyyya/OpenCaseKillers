"""Use the restricted API role for a local host run; never print credentials."""

import os
import sys
from pathlib import Path
from dotenv import dotenv_values
from support_agent.cli import main

if __name__ == "__main__":
    values = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    if not values.get("API_DATABASE_URL"):
        raise SystemExit("Run python scripts/bootstrap.py first")
    os.environ["DATABASE_URL"] = values["API_DATABASE_URL"]
    sys.argv = ["support-agent", "api", *sys.argv[1:]]
    main()
