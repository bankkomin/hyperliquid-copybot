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


# ---- shared cycle scaffolding -------------------------------------------
from src.models import AccountState, Order  # noqa: E402

RUNG = [Order(oid=1, side="B", px=57_860, sz=0.2, ts_ms=0)]


def leader_state(orders=None, position=1.33557, ts=1000, equity=66_435):
    return AccountState(
        equity=equity, position=position, entry_px=64_249.1, mark_px=79_660,
        fetched_at_ms=ts, open_orders=RUNG if orders is None else orders,
    )


class FakeWatcher:
    """Stands in for the Hyperliquid info API in cycle tests."""

    def __init__(self, state, fills=None):
        self._s, self._fills = state, fills or []

    async def fetch(self, now_ms):
        return self._s

    async def fetch_fills(self, since_ms):
        return [f for f in self._fills if f["time"] >= since_ms]


@pytest.fixture
def make_deps(cfg_paper):
    """Deps wired to a real Store + PaperBroker and a fake watcher."""
    from src.main import Deps
    from src.paper import PaperBroker

    def _make(store, watcher, cfg=None):
        cfg = cfg or cfg_paper
        return Deps(cfg=cfg, store=store, watcher=watcher,
                    broker=PaperBroker(store, cfg.paper.start_equity))

    return _make
