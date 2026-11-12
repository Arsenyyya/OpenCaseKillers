"""A source checkout succeeding must not hide resources missing from the release wheel."""

import importlib.util
from pathlib import Path
import subprocess
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_wheel", ROOT / "scripts/check_wheel.py")
wheel_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wheel_check)


def test_wheel_installed_outside_checkout_has_providers_prompts_and_migrations(tmp_path):
    cache = ROOT / ".cache/uv"
    build = subprocess.run(
        [
            wheel_check.find_uv(),
            "build",
            "--wheel",
            "--offline",
            "--out-dir",
            str(tmp_path),
            "--cache-dir",
            str(cache),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, build.stdout
    result = wheel_check.check_wheel(wheels[0])
    assert '"installed_wheel": "passed"' in result
    assert '"status": "not_run"' in result


def test_wheel_checker_rejects_private_build_inputs_before_install(tmp_path):
    wheel = tmp_path / "unsafe-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as package:
        package.writestr("support_agent/resources/config/issuer.key", "synthetic private input")
    with pytest.raises(ValueError, match="Private or unwanted"):
        wheel_check.check_wheel(wheel)
