from pathlib import Path

import pytest

# Frozen config fixture, decoupled from the human-tuned configs/default.yaml so
# operator edits to the real config never break the loader value-assertions.
_FROZEN_CONFIG_PATH = Path(__file__).parent / "fixtures" / "frozen_config.yaml"


@pytest.fixture
def frozen_config_path() -> Path:
    """Path to the frozen test config — pass to ``load_app_config(...)``."""

    return _FROZEN_CONFIG_PATH
