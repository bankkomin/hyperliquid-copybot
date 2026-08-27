"""M3: crash / restart / shutdown resilience."""

import asyncio
from unittest.mock import MagicMock

from src.main import Deps, _sleep_unless_stopped, cycle, shutdown, startup
from src.models import AccountState, Order, RiskState
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


def paper_deps(cfg, st):
    return Deps(cfg=cfg, store=st, watcher=FakeWatcher(leader_state()),
                broker=PaperBroker(st, cfg.paper.start_equity))


# ---- startup recovery ----------------------------------------------------

def test_live_startup_cancels_all_and_clears_mirror(tmp_path, cfg_live):
    st = Store(tmp_path / "t.db")
    st.mirror_put(1, 555, 57_860.0, 0.2, 0.0301, 0.1505, 1000)
    broker = MagicMock()
    broker.cancel_all.return_value = 1
    startup(Deps(cfg=cfg_live, store=st, watcher=MagicMock(), broker=broker))
    broker.cancel_all.assert_called_once()
    assert st.mirror_get() == {}


def test_paper_startup_keeps_the_rehydrated_book(tmp_path, cfg_paper):
    """Paper orders live in SQLite, so the mirror rows still describe real
    orders — clearing them would strand the book un-mirrored forever."""
    st = Store(tmp_path / "t.db")
    deps = paper_deps(cfg_paper, st)
    asyncio.run(cycle(deps, now_ms=1000))
    assert len(st.mirror_get()) == 1

    st2 = Store(tmp_path / "t.db")
    deps2 = paper_deps(cfg_paper, st2)
    startup(deps2)
    assert len(st2.mirror_get()) == 1  # preserved
    assert len(deps2.broker.open) == 1  # and the broker rehydrated it


def test_restart_does_not_duplicate_orders(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = paper_deps(cfg_paper, st)
    asyncio.run(cycle(deps, now_ms=1000))

    st2 = Store(tmp_path / "t.db")
    deps2 = paper_deps(cfg_paper, st2)
    startup(deps2)
    asyncio.run(cycle(deps2, now_ms=2000))
    assert len(deps2.broker.open) == 1  # not 2
    assert len(st2.mirror_get()) == 1


def test_halt_survives_restart_and_is_announced(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    st.record_event(1, "critical", "state_change", "HALT")
    deps = paper_deps(cfg_paper, st)
    startup(deps)
    assert st.latest_risk_state() == RiskState.HALT.value
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "startup_halted" in kinds


def test_hwm_is_not_reset_by_restart(tmp_path, cfg_paper):
    """The kill-switch must not be clearable by rebooting."""
    st = Store(tmp_path / "t.db")
    st.update_equity(1, 20_000)
    st2 = Store(tmp_path / "t.db")
    assert round(st2.update_equity(2, 13_000), 1) == -35.0


# ---- graceful shutdown ---------------------------------------------------

def test_stop_request_is_seen_and_clears(tmp_path):
    st = Store(tmp_path / "t.db")
    assert st.stop_requested() is False
    st.record_event(1, "info", "stop_requested", "bat")
    assert st.stop_requested() is True
    st.record_event(2, "info", "stopped", "done")
    assert st.stop_requested() is False


def test_sleep_wakes_early_on_stop(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = paper_deps(cfg_paper, st)
    st.record_event(1, "info", "stop_requested", "bat")
    assert asyncio.run(_sleep_unless_stopped(deps, seconds=60)) is True


def test_shutdown_keeps_the_position(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = paper_deps(cfg_paper, st)
    asyncio.run(cycle(deps, now_ms=1000))
    deps.broker.market_fill("B", 0.05, 79_660, 1500)
    before = deps.broker.position
    shutdown(deps)
    assert deps.broker.position == before  # never liquidated on stop
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "stopped" in kinds


def test_live_shutdown_cancels_resting_orders(tmp_path, cfg_live):
    st = Store(tmp_path / "t.db")
    st.mirror_put(1, 555, 57_860.0, 0.2, 0.0301, 0.1505, 1000)
    broker = MagicMock()
    broker.cancel_all.return_value = 1
    broker.state.return_value = AccountState(
        equity=10_000, position=0.2, entry_px=79_000, mark_px=79_660, fetched_at_ms=1
    )
    deps = Deps(cfg=cfg_live, store=st, watcher=MagicMock(), broker=broker,
                last_leader=leader_state())
    shutdown(deps)
    broker.cancel_all.assert_called_once()
    assert st.mirror_get() == {}
