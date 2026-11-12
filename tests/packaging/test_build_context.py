import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "prepare_build_context", ROOT / "scripts/prepare_build_context.py"
)
context = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(context)


def write(root, relative, content="synthetic"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def test_build_context_keeps_source_and_templates_without_copying_local_credentials(tmp_path):
    source, target = tmp_path / "source", tmp_path / "context"
    expected = {
        "Dockerfile",
        ".env.example",
        "pyproject.toml",
        "README.md",
        "src/support_agent/app.py",
        "prompts/extraction-v1.txt",
        "config/terms.json",
        "config/internal.example.json",
        "fixtures/demo/manifest.json",
        "migrations/versions/schema.sql",
    }
    excluded = {
        ".env",
        ".env.internal",
        ".local/tokens.json",
        "config/issuer.key",
        "config/issuer.pem",
        "config/internal.json",
        "config/private.local.json",
        "docs/.env.backup",
        "docs/source/case.pdf",
        "src/support_agent/__pycache__/app.pyc",
        "private-notes.txt",
    }
    for name in expected | excluded:
        write(source, name)
    copied = set(context.prepare_context(source, target))
    assert copied == expected
    assert not any((target / name).exists() for name in excluded)


def test_build_context_rejects_symlink_and_nonempty_destination(tmp_path):
    source, target = tmp_path / "source", tmp_path / "context"
    write(source, "Dockerfile")
    outside = write(tmp_path, "outside-secret")
    (source / "src").mkdir()
    (source / "src/leak.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        context.prepare_context(source, target)
    (source / "src/leak.py").unlink()
    write(target, "do-not-overwrite")
    with pytest.raises(ValueError, match="empty"):
        context.prepare_context(source, target)
    with pytest.raises(ValueError, match="outside"):
        context.prepare_context(source, source / "nested")
