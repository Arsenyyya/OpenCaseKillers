"""Install a local wheel in isolation and verify runtime resources outside the checkout."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
from tempfile import TemporaryDirectory
import tomllib
import venv
import zipfile

SMOKE = r"""
import asyncio
import importlib.metadata
import json
import sys
from pathlib import Path

installed = Path(sys.argv[1]).resolve()
import support_agent
assert Path(support_agent.__file__).resolve().is_relative_to(installed), 'Checkout import leaked into wheel check'
from support_agent.settings import ROOT, Settings
from support_agent.adapters.providers import DemoModels, FixtureSource
from support_agent.adapters.storage import LocalStorage
from alembic.config import Config
from alembic.script import ScriptDirectory

settings = Settings(_env_file=None)
assert ROOT.is_relative_to(installed), 'Resources resolved outside the installed wheel'
required = ['prompts/extraction-v1.txt', 'prompts/matching-v1.txt', 'config/terms.json',
            'fixtures/demo/model-responses.json', 'fixtures/demo/media-responses.json',
            'fixtures/demo/assets/mixed.pdf', 'fixtures/demo/assets/request.docx',
            'migrations/env.py', 'alembic.ini']
for relative in required:
    assert (ROOT / relative).is_file(), 'Missing wheel resource: '+relative
assert settings.token_file.resolve().is_relative_to(Path.cwd()), 'Credentials default into site-packages'
assert settings.storage_dir.resolve().is_relative_to(Path.cwd()), 'Runtime storage defaults into site-packages'
models = DemoModels(settings.fixtures_dir)
assert models.answers, 'Demo response corpus missing'
batch = asyncio.run(FixtureSource(settings.fixtures_dir, 'atlas').list_changes('0', 2))
assert len(batch.changes) == 2
storage = LocalStorage(settings.fixtures_dir / 'assets', settings.max_file_bytes)
assert storage.read('call.wav')[:4] == b'RIFF'
config = Config(str(ROOT / 'alembic.ini'))
config.set_main_option('script_location', str(ROOT / 'migrations'))
heads = ScriptDirectory.from_config(config).get_heads()
assert len(heads) == 1, 'Migration history must have one current head'
print(json.dumps({'installed_wheel': 'passed', 'version': importlib.metadata.version('support-insight-agent'),
                  'resource_files_checked': len(required), 'migration_heads': heads}))
"""


def find_uv() -> str:
    local = Path(sys.executable).with_name("uv")
    if local.is_file():
        return str(local)
    available = shutil.which("uv")
    if available:
        return available
    raise RuntimeError("uv is required; follow the project installation instructions")


def check_wheel(wheel: Path) -> str:
    wheel = wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        raise ValueError("Expected a locally built .whl file")
    with zipfile.ZipFile(wheel) as package:
        for name in package.namelist():
            path = Path(name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "__pycache__" in path.parts
                or any(p in {".env", ".local", ".git", "tokens.json", "credentials.json"} for p in path.parts)
                or any(p.startswith(".env.") for p in path.parts)
                or path.suffix.lower() in {".pyc", ".pem", ".key", ".p12", ".pfx"}
            ):
                raise ValueError(f"Private or unwanted build artifact in wheel: {name}")
    with TemporaryDirectory(prefix="support-agent-wheel-") as directory:
        temporary = Path(directory)
        environment, working = temporary / "venv", temporary / "unrelated-cwd"
        venv.EnvBuilder(with_pip=False).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        purelib = subprocess.check_output(
            [str(python), "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True
        ).strip()
        installed = Path(purelib)
        # Reuse the already locked dependencies without processing the checkout's editable .pth.
        (installed / "locked_dependencies.pth").write_text(sysconfig.get_path("purelib") + "\n")
        working.mkdir()
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["UV_CACHE_DIR"] = str(temporary / "uv-cache")
        # No dependency fetch: the active development environment already provides locked dependencies.
        subprocess.run(
            [find_uv(), "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel)],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        result = subprocess.run(
            [str(python), "-I", "-c", SMOKE, str(installed)],
            cwd=working,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        executable = binary / ("support-agent.exe" if os.name == "nt" else "support-agent")
        cli = subprocess.run(
            [str(executable), "evaluate"],
            cwd=working,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout + cli.stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, nargs="?")
    args = parser.parse_args()
    if args.wheel is None:
        root = Path(__file__).resolve().parents[1]
        version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        args.wheel = root / ".local/dist" / f"support_insight_agent-{version}-py3-none-any.whl"
    try:
        print(check_wheel(args.wheel), end="")
    except subprocess.CalledProcessError as exc:
        raise SystemExit(exc.stderr or exc.stdout or "Installed-wheel verification failed") from None


if __name__ == "__main__":
    main()
