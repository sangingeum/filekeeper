"""Home directory resolution and configuration.

Two kinds of state exist, and they live in different places on purpose:

* the **registry** (workspace registrations and settings) lives in the user's
  home, because it spans workspaces;
* **per-workspace state** (the future operation journal and blob store) lives in
  ``<workspace>/.filekeeper/`` by default, so it travels with the checkout.
  ``state_location = "global"`` opts into one shared store under the home
  directory instead.

``config.toml`` in the home directory holds tunable limits. It is read on every
invocation; writing it is left to the user (``keeper init`` creates it with the
defaults spelled out). ``state_location`` is written only as a commented example:
a generated file must not turn a default into an explicit operator decision,
because an explicit decision refuses the global fallback.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .errors import StorageError, ValidationError

CONFIG_FILENAME = "config.toml"
STATE_DIR_NAME = ".filekeeper"


@dataclass(frozen=True, slots=True)
class Config:
    """Limits and behaviour switches. Values are bytes unless stated otherwise."""

    max_read_bytes: int = 5 * 1024 * 1024
    max_write_bytes: int = 20 * 1024 * 1024
    max_search_results: int = 500
    max_directory_entries: int = 2000
    max_recursion_depth: int = 25
    max_line_length: int = 2000
    max_history_blob_bytes: int = 4 * 1024 * 1024
    fsync: bool = True
    state_location: str = "project"
    # True only when the configuration file named ``state_location`` explicitly.
    # A workspace whose root cannot host its state directory may fall back to the
    # global store when the setting was left at the default, but an explicit
    # operator choice is honoured and fails loudly instead of being redirected.
    state_location_explicit: bool = False

    def __post_init__(self) -> None:
        if self.state_location not in {"project", "global"}:
            raise ValidationError(
                "state_location must be 'project' or 'global'",
                details={"state_location": self.state_location},
            )
        for field_info in fields(self):
            value = getattr(self, field_info.name)
            if isinstance(value, int) and not isinstance(value, bool) and value <= 0:
                raise ValidationError(
                    f"{field_info.name} must be positive", details={"value": value}
                )


def resolve_home(override: str | os.PathLike[str] | None = None) -> Path:
    """Resolve Filekeeper's home directory: explicit, ``FILEKEEPER_HOME``, XDG, then default."""

    if override is not None:
        return Path(override).expanduser()
    from_env = os.environ.get("FILEKEEPER_HOME")
    if from_env:
        return Path(from_env).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg).expanduser() / "filekeeper"
    return Path.home() / ".local" / "share" / "filekeeper"


def config_path(home: Path) -> Path:
    return home / CONFIG_FILENAME


def load_config(home: Path) -> Config:
    """Load ``config.toml``; a missing file yields defaults. Unknown keys are ignored."""

    path = config_path(home)
    if not path.exists():
        return Config()
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise StorageError(
            f"cannot read configuration file: {getattr(exc, 'strerror', None) or exc}",
            details={"path": str(path)},
        ) from exc

    section = raw.get("filekeeper", raw)
    if not isinstance(section, dict):
        raise StorageError("configuration file must contain a table", details={"path": str(path)})

    known = {field_info.name for field_info in fields(Config)} - {"state_location_explicit"}
    values: dict[str, object] = {}
    for key, value in section.items():
        if key in known:
            values[key] = value
    try:
        explicit = "state_location" in section
        return Config(**values, state_location_explicit=explicit)  # type: ignore[arg-type]
    except TypeError as exc:
        raise StorageError(
            f"invalid configuration value: {exc}", details={"path": str(path)}
        ) from exc


DEFAULT_CONFIG_TOML = """\
# Filekeeper configuration.
# All sizes are bytes. Delete a key to fall back to the built-in default.

[filekeeper]
max_read_bytes = 5242880
max_write_bytes = 20971520
max_search_results = 500
max_directory_entries = 2000
max_recursion_depth = 25
max_line_length = 2000

# Files above this size are skipped by history: the mutation is still recorded,
# but its previous bytes are not stored, so it cannot be undone. This keeps one
# large edit from silently filling the blob store.
max_history_blob_bytes = 4194304

# Durability of atomic writes: flush and fsync the temporary file and the
# parent directory before reporting success.
fsync = true

# Per-workspace state lives in <workspace>/.filekeeper/ by default. When the
# workspace root cannot host that directory (a read-only checkout, or a root at
# /), Filekeeper falls back to the global store under this home directory and
# says so in the response.
#
# Uncommenting the next line pins the choice instead of leaving it at the
# built-in default. "project" then fails loudly when the root cannot host the
# directory instead of being redirected; "global" always uses the shared store.
# An uncommented key is an operator decision, so keep it commented unless you
# mean it.
# state_location = "project"
"""
