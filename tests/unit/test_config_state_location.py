"""A generated configuration must not express an operator decision.

``keeper init`` writes the defaults out so the file is discoverable. The state
location is the one setting where "written down" changes behaviour: an explicit
choice refuses the global fallback. The generated file therefore leaves it
commented, and only a file that really contains the key counts as explicit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from filekeeper.core.config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG_TOML,
    Config,
    config_path,
    load_config,
)
from filekeeper.core.service import init_home


def test_the_generated_configuration_has_no_live_state_location() -> None:
    section = tomllib.loads(DEFAULT_CONFIG_TOML)["filekeeper"]

    assert "state_location" not in section
    assert 'state_location = "project"' in DEFAULT_CONFIG_TOML


def test_a_missing_configuration_is_not_explicit(tmp_path: Path) -> None:
    config = load_config(tmp_path / "absent-home")

    assert config.state_location == "project"
    assert config.state_location_explicit is False


def test_a_configuration_from_init_is_not_explicit(tmp_path: Path) -> None:
    home = init_home(tmp_path / "home")
    config = load_config(home)

    assert config_path(home).name == CONFIG_FILENAME
    assert config.state_location == "project"
    assert config.state_location_explicit is False


def test_a_hand_written_project_choice_is_explicit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_path(home).write_text('[filekeeper]\nstate_location = "project"\n', encoding="utf-8")

    config = load_config(home)

    assert config.state_location == "project"
    assert config.state_location_explicit is True


def test_a_hand_written_global_choice_is_explicit(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_path(home).write_text('[filekeeper]\nstate_location = "global"\n', encoding="utf-8")

    config = load_config(home)

    assert config.state_location == "global"
    assert config.state_location_explicit is True


def test_the_default_config_object_is_not_explicit() -> None:
    assert Config().state_location_explicit is False
