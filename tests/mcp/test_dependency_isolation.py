"""The two-distribution boundary, proved in both directions.

Direction one: a wheel install of ``filekeeper-cli`` alone neither declares nor can
import the MCP SDK. Direction two: a wheel install of ``filekeeper-mcp`` brings
Core and the SDK and exposes the console script. The second direction therefore
fails if the adapter's dependency is ever dropped from its metadata.
"""

from __future__ import annotations

import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ADAPTER_ROOT = PROJECT_ROOT / "packages" / "filekeeper-mcp"


def _project_version(pyproject: Path) -> str:
    """The version the build will stamp, so a bump never breaks the proof."""
    with pyproject.open("rb") as handle:
        return str(tomllib.load(handle)["project"]["version"])


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
    core_version = _project_version(PROJECT_ROOT / "pyproject.toml")
    adapter_version = _project_version(ADAPTER_ROOT / "pyproject.toml")
    core = next(core_dir.glob(f"filekeeper_cli-{core_version}-*.whl"))
    adapter = next(adapter_dir.glob(f"filekeeper_mcp-{adapter_version}-*.whl"))
    return core, adapter


def _fresh_venv(tmp_path: Path, name: str) -> Path:
    directory = tmp_path / name
    _run([_uv(), "venv", str(directory)], cwd=tmp_path)
    return directory


def _install(venv: Path, *wheels: Path, no_deps: bool = False) -> None:
    """Install built wheels.

    ``no_deps`` keeps the version under test exactly the wheel just built: without
    it, ``uv`` may satisfy the adapter's ``filekeeper-cli>=0.1,<0.2`` requirement
    from the index, so the probe would import a previously published release
    instead of this build. That only breaks once PyPI holds a different version in
    the same range, i.e. at release time.
    """
    command = [_uv(), "pip", "install", "--python", str(venv / "bin" / "python")]
    if no_deps:
        command.append("--no-deps")
    command.extend(str(wheel) for wheel in wheels)
    _run(command, cwd=venv.parent)


def _install_requirements(venv: Path, *requirements: str) -> None:
    """Install runtime dependencies from the index, with their own dependencies."""
    _run(
        [
            _uv(),
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            *requirements,
        ],
        cwd=venv.parent,
    )


def _probe(venv: Path, source: str, tmp_path: Path, name: str) -> str:
    script = tmp_path / name
    script.write_text(source, encoding="utf-8")
    return _run([str(venv / "bin" / "python"), str(script)], cwd=tmp_path).stdout


_INSTALLED_STATE_SOURCE = (
    "import importlib.metadata as metadata\n"
    "import json\n"
    "dist = metadata.distribution({distribution!r})\n"
    "print('dist-version', dist.version)\n"
    "record = dist.read_text('direct_url.json')\n"
    "source = json.loads(record).get('url') if record else 'an index (no direct_url.json)'\n"
    "print('dist-source', source)\n"
)


def _assert_installed_from_build(
    venv: Path,
    distribution: str,
    wheel: Path,
    expected_version: str,
    tmp_path: Path,
) -> None:
    """Guard: the installed distribution must be the wheel this run built.

    A mismatch means the installer resolved the requirement from somewhere else —
    almost always the index — and the probe would then be testing a published
    release rather than this build. The message names the wheel and the source the
    installed distribution actually came from.
    """
    output = _probe(
        venv,
        _INSTALLED_STATE_SOURCE.format(distribution=distribution),
        tmp_path,
        f"installed_state_{distribution.replace('-', '_')}.py",
    )
    state = dict(line.split(" ", 1) for line in output.strip().splitlines() if " " in line)
    if state.get("dist-version") != expected_version:
        raise AssertionError(
            f"{distribution} is installed as {state.get('dist-version')!r} but the "
            f"wheel just built ({wheel}) declares {expected_version!r}; the installed "
            f"distribution came from {state.get('dist-source')!r} — the install must "
            "name the built wheel rather than resolving from the index"
        )


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
    core_version = _project_version(PROJECT_ROOT / "pyproject.toml")
    venv = _fresh_venv(tmp_path, "core-only")
    # --no-deps: core declares no third-party dependencies, and the install must
    # not reach the index for anything — least of all for filekeeper itself.
    _install(venv, core, no_deps=True)
    _assert_installed_from_build(venv, "filekeeper-cli", core, core_version, tmp_path)

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

    assert f"core-ok {core_version}" in output
    assert "sdk-present False" in output


def test_installing_the_adapter_pulls_core_and_the_sdk(
    wheels: tuple[Path, Path], tmp_path: Path
) -> None:
    core, adapter = wheels
    core_version = _project_version(PROJECT_ROOT / "pyproject.toml")
    adapter_version = _project_version(ADAPTER_ROOT / "pyproject.toml")
    venv = _fresh_venv(tmp_path, "adapter")

    # The wheels go in with --no-deps so both come from this build: the adapter's
    # `filekeeper-cli>=0.1,<0.2` requirement would otherwise be satisfied from the
    # index by whatever version is published, and the probe would import that.
    _install(venv, core, adapter, no_deps=True)
    _assert_installed_from_build(venv, "filekeeper-cli", core, core_version, tmp_path)
    _assert_installed_from_build(venv, "filekeeper-mcp", adapter, adapter_version, tmp_path)

    # The SDK still has to be genuinely present, so install exactly the adapter's
    # declared non-core runtime requirements from the index.
    sdk_requirements = [
        requirement
        for requirement in _metadata_requirements(adapter)
        if not requirement.startswith("filekeeper")
    ]
    assert sdk_requirements, "the adapter must declare its own runtime dependencies"
    _install_requirements(venv, *sdk_requirements)

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

    assert f"adapter-ok {core_version} {adapter_version}" in output
    assert (venv / "bin" / "filekeeper-mcp").exists()
    assert (venv / "bin" / "keeper").exists()
