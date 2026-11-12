"""Stage the project's build inputs without copying local secrets or following symlinks."""

import argparse
import shutil
from pathlib import Path

ROOT_FILES = {
    "Dockerfile",
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "alembic.ini",
    "compose.yaml",
    "Makefile",
}
ROOT_DIRECTORIES = {
    "src",
    "prompts",
    "config",
    "fixtures",
    "migrations",
    "deploy",
    "scripts",
    "docs",
    "tests",
}
PRIVATE_NAMES = {
    ".git",
    ".venv",
    ".local",
    ".cache",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".DS_Store",
    "tokens.json",
    "credentials.json",
}
PRIVATE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".pyc", ".log"}


def is_private(relative: Path) -> bool:
    for part in relative.parts:
        if part in PRIVATE_NAMES or (part.startswith(".env") and part != ".env.example"):
            return True
        if part.endswith(".egg-info"):
            return True
    return (
        relative.parts[:2] == ("docs", "source")
        or relative.suffix.lower() in PRIVATE_SUFFIXES
        or relative.name.endswith(".local.json")
        or (
            relative.name.startswith("internal")
            and relative.suffix == ".json"
            and not relative.name.endswith(".example.json")
        )
    )


def prepare_context(source: Path, destination: Path) -> list[str]:
    source, destination = source.resolve(), destination.resolve()
    if not (source / "Dockerfile").is_file():
        raise ValueError("Build source must be the project directory containing Dockerfile")
    if destination == source or destination.is_relative_to(source):
        raise ValueError("Build destination must be outside the source tree")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Build destination must be empty")
    destination.mkdir(parents=True, exist_ok=True)
    copied = []

    def copy_entry(path: Path):
        relative = path.relative_to(source)
        if is_private(relative):
            return
        if path.is_symlink():
            raise ValueError(f"Build input must not be a symlink: {relative}")
        if path.is_dir():
            for child in sorted(path.iterdir()):
                copy_entry(child)
        elif path.is_file():
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(relative.as_posix())

    for path in sorted(source.iterdir()):
        if path.name in ROOT_FILES or (path.name in ROOT_DIRECTORIES and path.is_dir()):
            copy_entry(path)
    return copied


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    paths = prepare_context(args.source, args.destination)
    print(f"Prepared {len(paths)} build inputs; local credentials and caches excluded")


if __name__ == "__main__":
    main()
