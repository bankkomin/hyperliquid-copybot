"""M3: crash / restart / shutdown resilience."""

import asyncio
from unittest.mock import MagicMock

from src.main import Deps, _sleep_unless_stopped, cycle, shutdown, startup
from src.models import AccountState, RiskState
from src.store import Store
from tests.conftest import FakeWatcher, leader_state


# ---- startup recovery ----------------------------------------------------



def test_restart_does_not_duplicate_orders(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(position=0.0)))
    asyncio.run(cycle(deps, now_ms=1000))

    st2 = Store(tmp_path / "t.db")
    deps2 = make_deps(st2, FakeWatcher(leader_state(position=0.0)))
    startup(deps2)
    asyncio.run(cycle(deps2, now_ms=2000))
    assert len(deps2.broker.open) == 1  # not 2
    assert len(st2.mirror_get()) == 1


def test_halt_survives_restart_and_is_announced(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    st.record_event(1, "critical", "state_change", "HALT")
    deps = make_deps(st, FakeWatcher(leader_state(position=0.0)))
    startup(deps)
    assert st.latest_risk_state() == RiskState.HALT.value
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "startup_halted" in kinds



def test_manual_reset_rebases_the_hwm(tmp_path):
    """Regression: without re-basing, drawdown is still measured against the
    pre-crash peak, so run_monitors re-HALTs on the very next cycle and the
    documented un-HALT path can never resume the bot."""
    st = Store(tmp_path / "t.db")
    st.update_equity(1, 20_000)
    assert round(st.update_equity(2, 13_000), 1) == -35.0  # kill-switch trips
    st.record_event(3, "critical", "state_change", "HALT")
    st.record_event(4, "info", "manual_reset", "operator resume")
    assert st.latest_risk_state() == "NORMAL"
    assert round(st.update_equity(5, 13_000), 1) == 0.0  # re-based, not -35%


def test_halt_is_enforced_even_when_the_leader_api_is_down(tmp_path, cfg_paper, make_deps):
    """Regression: HALT enforcement sat behind the leader fetch, so a restart
    during a leader outage left a halted account fully exposed."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(position=0.0)))
    asyncio.run(cycle(deps, now_ms=1000))  # mirror a rung, take a position
    deps.broker.market_fill("B", 0.05, 79_660, 1500)
    st.record_event(1600, "critical", "state_change", "HALT")

    async def boom(now_ms):
        raise ConnectionError

    deps.watcher.fetch = boom
    asyncio.run(cycle(deps, now_ms=2000))
    assert deps.broker.position == 0.0  # flattened without the leader API
    assert deps.broker.open == {}


def test_startup_keeps_mirror_rows_when_the_book_is_not_verifiably_clean(tmp_path, cfg_live):
    """Regression: wiping rows we did not confirm cancelled orphans live orders
    while the first cycle re-places the same rungs — double size, half unmanaged."""
    st = Store(tmp_path / "t.db")
    st.mirror_put(1, 555, 57_860.0, 0.2, 0.0301, 0.1505, 1000)
    broker = MagicMock()
    broker.cancel_all.return_value = 0
    broker.book_is_clean.return_value = False  # API failed / orders remain
    startup(Deps(cfg=cfg_live, store=st, watcher=MagicMock(), broker=broker))
    assert 1 in st.mirror_get()  # kept, so we can still cancel it
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "startup_dirty_book" in kinds


def test_stale_stop_request_is_cleared_at_startup(tmp_path, cfg_paper, make_deps):
    """Regression: a stop request left by stop_copybot.bat run against an
    already-dead process made every future start run one cycle and exit."""
    st = Store(tmp_path / "t.db")
    st.record_event(1, "info", "stop_requested", "bat")
    deps = make_deps(st, FakeWatcher(leader_state(position=0.0)))
    startup(deps)
    assert st.stop_requested() is False


# ---- graceful shutdown ---------------------------------------------------


def test_sleep_wakes_early_on_stop(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(position=0.0)))
    st.record_event(1, "info", "stop_requested", "bat")
    assert asyncio.run(_sleep_unless_stopped(deps, seconds=60)) is True


def test_shutdown_keeps_the_position(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(position=0.0)))
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
