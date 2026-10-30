"""The two-distribution boundary, proved in both directions.

Direction one: a wheel install of ``filekeeper-cli`` alone neither declares nor can
import the MCP SDK. Direction two: a wheel install of ``filekeeper-mcp`` brings
Core and the SDK and exposes the console script. The second direction therefore
fails if the adapter's dependency is ever dropped from its metadata.
"""

from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = PROJECT_ROOT / "packages" / "filekeeper-mcp"


def _uv() -> str:
    import shutil

    found = shutil.which("uv")
    assert found, "uv is required for the isolation proof"
    return found


def _run(command: list[str], *, cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"{command!r} failed with {completed.returncode}"
            f"\nSTDOUT\n{completed.stdout}\nSTDERR\n{completed.stderr}"
        )
    return completed


def _metadata_requirements(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        assert len(metadata_names) == 1, metadata_names
        metadata = archive.read(metadata_names[0]).decode("utf-8")
    return [
        line.removeprefix("Requires-Dist:").strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]


@pytest.fixture(scope="module")
def wheels(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    output = tmp_path_factory.mktemp("wheels")
    core_dir = output / "core"
    adapter_dir = output / "adapter"
    _run(
        [_uv(), "build", "--wheel", "--out-dir", str(core_dir), str(PROJECT_ROOT)],
        cwd=output,
    )
    _run(
        [_uv(), "build", "--wheel", "--out-dir", str(adapter_dir), str(ADAPTER_ROOT)],
        cwd=output,
    )
    core = next(core_dir.glob("filekeeper_cli-0.1.0-*.whl"))
    adapter = next(adapter_dir.glob("filekeeper_mcp-0.1.0-*.whl"))
    return core, adapter


def _fresh_venv(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    _run([_uv(), "venv", str(directory)], cwd=tmp_path)
    return directory


def _install(venv: Path, *wheels: Path, no_deps: bool = False) -> None:
    command = [_uv(), "pip", "install", "--python", str(venv / "bin" / "python")]
    if no_deps:
        command.append("--no-deps")
    command.extend(str(wheel) for wheel in wheels)
    _run(command, cwd=venv.parent)


def _probe(venv: Path, source: str, tmp_path: Path, name: str) -> str:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return _run([str(venv / "bin" / "python"), str(script)], cwd=tmp_path).stdout


def test_core_metadata_declares_no_third_party_dependencies(wheels: tuple[Path, Path]) -> None:
    core, _adapter = wheels
    assert _metadata_requirements(core) == []


def test_adapter_metadata_requires_core_and_the_mcp_sdk(
    wheels: tuple[Path, Path],
) -> None:
    _core, adapter = wheels
    requirements = _metadata_requirements(adapter)
    assert any(requirement.startswith("filekeeper") for requirement in requirements)
    assert any(requirement.startswith("mcp") for requirement in requirements)


def test_installing_only_core_leaves_the_sdk_absent(
    wheels: tuple[Path, Path], tmp_path: Path
) -> None:
    core, _adapter = wheels
    venv = _fresh_venv(tmp_path, "core-only")
    _install(venv, core, no_deps=True)

    output = _probe(
        venv,
        (
            "import importlib.util\n"
            "import filekeeper\n"
            "import filekeeper.cli.app\n"
            "import filekeeper.core.service\n"
            "print('core-ok', filekeeper.__version__)\n"
            "print('sdk-present', importlib.util.find_spec('mcp') is not None)\n"
        ),
        tmp_path,
        "core_probe.py",
    )

    assert "core-ok 0.1.0" in output
    assert "sdk-present False" in output


def test_installing_the_adapter_pulls_core_and_the_sdk(
    wheels: tuple[Path, Path], tmp_path: Path
) -> None:
    core, adapter = wheels
    venv = _fresh_venv(tmp_path, "adapter")
    _install(venv, core, adapter)

    output = _probe(
        venv,
        (
            "import filekeeper\n"
            "import filekeeper_mcp\n"
            "import filekeeper_mcp.server\n"
            "import mcp\n"
            "import mcp.types\n"
            "print('adapter-ok', filekeeper.__version__, filekeeper_mcp.__version__)\n"
        ),
        tmp_path,
        "adapter_probe.py",
    )

    assert "adapter-ok 0.1.0 0.1.0" in output
    assert (venv / "bin" / "filekeeper-mcp").exists()
    assert (venv / "bin" / "keeper").exists()
