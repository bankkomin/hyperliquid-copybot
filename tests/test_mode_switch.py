"""Mode switching and the live-mode paths that run inside the shared cycle."""

import asyncio
from unittest.mock import MagicMock

import pytest

from src.main import Deps, cycle, make_broker, startup
from src.models import AccountState, Order
from src.paper import PaperBroker
from src.store import Store

RUNG = [Order(oid=1, side="B", px=57_860, sz=0.2, ts_ms=0)]


def leader_state(orders=RUNG, position=0.0, ts=1000):
    return AccountState(
        equity=66_435, position=position, entry_px=64_249.1, mark_px=79_660,
        fetched_at_ms=ts, open_orders=orders,
    )


class FakeWatcher:
    def __init__(self, state):
        self._s = state

    async def fetch(self, now_ms):
        return self._s

    async def fetch_fills(self, since_ms):
        return []


def test_paper_mode_returns_paper_broker(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    assert isinstance(make_broker(cfg_paper, st), PaperBroker)


def test_live_mode_without_secrets_refuses(tmp_path, cfg_live, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        make_broker(cfg_live, Store(tmp_path / "t.db"))


def test_startup_cancels_all_in_live_mode(tmp_path, cfg_live):
    """Never trust orders left by a dead process."""
    st = Store(tmp_path / "t.db")
    st.mirror_put(1, 555, 57_860.0, 0.2, 0.0301, 0.1505, 1000)
    broker = MagicMock()
    broker.cancel_all.return_value = 3
    startup(Deps(cfg=cfg_live, store=st, watcher=MagicMock(), broker=broker))
    broker.cancel_all.assert_called_once()
    assert st.mirror_get() == {}  # stale mirror rows closed


def test_startup_does_not_cancel_in_paper_mode(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    broker = MagicMock()
    startup(Deps(cfg=cfg_paper, store=st, watcher=MagicMock(), broker=broker))
    broker.cancel_all.assert_not_called()


def test_rejected_order_does_not_create_a_mirror_row(tmp_path, cfg_paper):
    """A live ALO rejection returns None — recording a mirror row for it would
    mark the rung mirrored when nothing is actually resting."""
    st = Store(tmp_path / "t.db")
    broker = MagicMock()
    broker.execute.return_value = None  # exchange rejected
    broker.open = {}
    broker.state.return_value = AccountState(
        equity=10_000, position=0.0, entry_px=None, mark_px=79_660, fetched_at_ms=1000
    )
    deps = Deps(cfg=cfg_paper, store=st, watcher=FakeWatcher(leader_state()), broker=broker)
    asyncio.run(cycle(deps, now_ms=1000))
    assert st.mirror_get() == {}
    reasons = [
        r[0] for r in st.conn.execute(
            "SELECT veto_reason FROM decisions WHERE action='veto'"
        ).fetchall()
    ]
    assert "exchange_reject" in reasons


def test_our_fills_are_ingested_in_live_mode(tmp_path, cfg_live):
    st = Store(tmp_path / "t.db")
    broker = MagicMock()
    broker.execute.return_value = 555
    broker.open = {}
    broker.state.return_value = AccountState(
        equity=10_000, position=0.0, entry_px=None, mark_px=79_660, fetched_at_ms=1000
    )
    broker.funding_since.return_value = -5.0
    deps = Deps(cfg=cfg_live, store=st, watcher=FakeWatcher(leader_state()), broker=broker)
    asyncio.run(cycle(deps, now_ms=1000))
    broker.ingest_our_fills.assert_called_once()


def test_funding_bleed_alerts_over_threshold(tmp_path, cfg_live):
    st = Store(tmp_path / "t.db")
    broker = MagicMock()
    broker.execute.return_value = 555
    broker.open = {}
    broker.state.return_value = AccountState(
        equity=10_000, position=0.0, entry_px=None, mark_px=79_660, fetched_at_ms=1000
    )
    broker.funding_since.return_value = -250.0  # 2.5% of 10k > 2% threshold
    deps = Deps(cfg=cfg_live, store=st, watcher=FakeWatcher(leader_state()), broker=broker)
    asyncio.run(cycle(deps, now_ms=1000))
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "funding_bleed" in kinds
    assert st.conn.execute("SELECT funding_cum FROM equity_curve").fetchone()[0] == -250.0
