import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))

from src.config import load_config  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg_paper():
    return load_config(ROOT / "config.example.yaml")


@pytest.fixture
def cfg_live(cfg_paper):
    return cfg_paper.model_copy(update={"mode": "live"})


@pytest.fixture
def leader_capture():
    """Real Hyperliquid responses captured from the tracked account."""
    return (
        json.loads((FIXTURES / "clearinghouse.json").read_text()),
        json.loads((FIXTURES / "open_orders.json").read_text()),
    )
