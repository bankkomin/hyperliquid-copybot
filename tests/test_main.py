import asyncio

from src.main import cycle
from src.models import Order
from src.store import Store
from tests.conftest import RUNG, FakeWatcher, leader_state


def test_cycle_mirrors_new_leader_order(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))
    mirror = st.mirror_get()
    assert 1 in mirror and mirror[1]["our_sz"] > 0
    assert ("order",) in st.conn.execute("SELECT action FROM decisions").fetchall()


def test_cycle_is_idempotent(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))
    asyncio.run(cycle(deps, now_ms=2000))  # second run must not duplicate
    assert len(deps.broker.open) == 1


def test_leader_fill_becomes_our_fill_not_cancel(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))  # rung mirrored

    w._s = leader_state([], position=1.53557, ts=2000)  # his order GONE (filled)
    w._fills = [{"tid": 9, "oid": 1, "time": 1500, "side": "B", "px": "57860.0",
                 "sz": "0.2", "crossed": False, "dir": "Open Long", "coin": "BTC"}]
    asyncio.run(cycle(deps, now_ms=2000))

    assert deps.broker.position > 0  # we FILLED, not canceled
    reason = st.conn.execute(
        "SELECT close_reason FROM mirror_map WHERE leader_oid=1"
    ).fetchone()[0]
    assert reason == "his_fill"
    assert st.conn.execute("SELECT COUNT(*) FROM leader_fills").fetchone()[0] == 1


def test_leader_cancel_is_not_a_fill(tmp_path, cfg_paper, make_deps):
    """Leader position held flat so reconciliation stays out of the way — this
    isolates the cancel path: his order vanishing WITHOUT a fill must not book one."""
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=0.0))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))
    w._s = leader_state([], position=0.0, ts=2000)  # gone, but NO fill reported
    asyncio.run(cycle(deps, now_ms=2000))
    assert deps.broker.position == 0.0
    assert st.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    reason = st.conn.execute(
        "SELECT close_reason FROM mirror_map WHERE leader_oid=1"
    ).fetchone()[0]
    assert reason == "his_cancel"


def test_first_cycle_acquires_leader_position_via_reconciliation(tmp_path, cfg_paper, make_deps):
    """Documented consequence of copying a leader who already holds a position:
    we market-buy our scaled share at TODAY's price, not his average entry."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state([], position=1.33557)))
    asyncio.run(cycle(deps, now_ms=1000))
    scale = cfg_paper.paper.start_equity / 66_435
    assert abs(deps.broker.position - 1.33557 * scale) < 0.001
    crossed = st.conn.execute("SELECT crossed FROM fills").fetchone()[0]
    assert crossed == 1  # taker, with the cost that implies


def test_fetch_failure_reuses_last_leader_and_ages_into_warning(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state([], ts=1000))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))  # baseline fetch OK

    async def boom(now_ms):
        raise ConnectionError

    w.fetch = boom
    asyncio.run(cycle(deps, now_ms=100_000))  # 99s stale: still NORMAL
    assert st.latest_risk_state() == "NORMAL"
    asyncio.run(cycle(deps, now_ms=400_000))  # 399s > 300s max
    assert st.latest_risk_state() == "WARNING"
    n = st.conn.execute("SELECT COUNT(*) FROM events WHERE kind='ws_lost'").fetchone()[0]
    assert n == 1  # once per outage, not per cycle


def test_first_cycle_fetch_failure_does_not_crash(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state([]))

    async def boom(now_ms):
        raise ConnectionError

    w.fetch = boom
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))  # no baseline: must return cleanly
    assert st.latest_risk_state() == "NORMAL"


def test_halt_cancels_and_flattens(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))  # HWM established ~10k, rung mirrored
    deps.broker.market_fill("B", 0.1, 79_660, 1500)  # give us a position
    deps.broker.cash -= 6_000  # simulate a catastrophic loss
    deps.broker._save()

    asyncio.run(cycle(deps, now_ms=2000))
    assert st.latest_risk_state() == "HALT"
    assert deps.broker.open == {}
    assert deps.broker.position == 0.0
    assert st.mirror_get() == {}


def test_halt_is_sticky_across_cycles(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG)))
    st.record_event(1, "critical", "state_change", "HALT")
    asyncio.run(cycle(deps, now_ms=1000))
    assert st.latest_risk_state() == "HALT"
    assert deps.broker.open == {}  # no new mirror orders placed


def test_reconciliation_closes_drift(tmp_path, cfg_paper, make_deps):
    """His taker trade has no resting order to mirror — reconciliation catches it."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state([], position=1.33557)))
    asyncio.run(cycle(deps, now_ms=1000))
    assert deps.broker.position > 0  # taker top-up happened
    triggers = [r[0] for r in st.conn.execute("SELECT trigger FROM decisions").fetchall()]
    assert "reconcile" in triggers


def test_emptied_leader_ladder_clears_the_live_table(tmp_path, cfg_paper, make_deps):
    """Regression: the dashboard overlay read the newest history snapshot, so an
    emptied ladder never disappeared from the chart."""
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=0.0))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))
    assert st.conn.execute("SELECT COUNT(*) FROM leader_ladder_live").fetchone()[0] == 1
    w._s = leader_state([], position=0.0, ts=2000)
    asyncio.run(cycle(deps, now_ms=2000))
    assert st.conn.execute("SELECT COUNT(*) FROM leader_ladder_live").fetchone()[0] == 0


def test_unchanged_ladder_is_not_rewritten_to_history(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG, position=0.0)))
    for ts in (1000, 2000, 3000):
        asyncio.run(cycle(deps, now_ms=ts))
    n = st.conn.execute("SELECT COUNT(*) FROM leader_open_orders").fetchone()[0]
    assert n == 1  # one history row, not one per cycle


def test_leader_zero_equity_does_not_crash_the_cycle(tmp_path, cfg_paper, make_deps):
    """Regression: ZeroDivisionError was swallowed by run()'s handler, silently
    freezing the mirror while our position stayed in the market."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state([], position=0.0, equity=0.0)))
    asyncio.run(cycle(deps, now_ms=1000))  # must not raise
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "leader_zero_equity" in kinds


def test_rebalance_cancels_are_not_logged_as_his_cancel(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=0.0))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))
    w._s = leader_state(  # he amended the rung's price
        [Order(oid=1, side="B", px=61_000, sz=0.2, ts_ms=0)], position=0.0, ts=2000
    )
    asyncio.run(cycle(deps, now_ms=2000))
    reasons = [
        r[0] for r in st.conn.execute("SELECT close_reason FROM mirror_map").fetchall()
    ]
    assert "rebalance" in reasons  # closed row kept alongside the re-placed one
    live = st.mirror_get()
    assert live[1]["px"] == 61_000  # and we are now mirroring the amended price


def test_halt_button_from_dashboard_is_honored(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))
    st.record_event(1500, "critical", "halt_requested", "dashboard button")
    asyncio.run(cycle(deps, now_ms=2000))
    assert st.latest_risk_state() == "HALT"
    assert deps.broker.open == {} and deps.broker.position == 0.0


def test_ladder_is_stable_across_cycles(tmp_path, cfg_paper, make_deps):
    """Regression (severe): mirror_map stored a RECONSTRUCTED leader_sz (our
    floor-rounded sz / scale), which always landed below his true size, so every
    rung looked 'amended' and the whole ladder cancel/replaced every 60 seconds
    — destroying maker queue position and burning the action allowance."""
    st = Store(tmp_path / "t.db")
    rungs = [
        Order(oid=1, side="B", px=57_860, sz=0.20, ts_ms=0),
        Order(oid=2, side="B", px=62_944, sz=0.12, ts_ms=0),
        Order(oid=3, side="B", px=73_521, sz=0.05, ts_ms=0),
    ]
    deps = make_deps(st, FakeWatcher(leader_state(rungs, position=0.0)))
    asyncio.run(cycle(deps, now_ms=1000))
    placed = st.conn.execute("SELECT COUNT(*) FROM mirror_map").fetchone()[0]
    for ts in (2000, 3000, 4000):
        asyncio.run(cycle(deps, now_ms=ts))
    assert st.conn.execute("SELECT COUNT(*) FROM mirror_map").fetchone()[0] == placed
    assert len(st.mirror_get()) == 3  # same three rungs, never churned


def test_halt_retries_until_flat(tmp_path, cfg_paper, make_deps):
    """Regression: the flatten fired once, during the crash that caused HALT —
    exactly when an IOC misses — and was never retried."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))
    deps.broker.cash -= 6_000
    deps.broker._save()
    asyncio.run(cycle(deps, now_ms=2000))
    assert st.latest_risk_state() == "HALT"

    # Something leaves us exposed again while HALT persists.
    deps.broker.market_fill("B", 0.05, 79_660, 2500)
    assert deps.broker.position != 0.0
    asyncio.run(cycle(deps, now_ms=3000))
    assert deps.broker.position == 0.0  # re-flattened, not abandoned


def test_reconcile_skips_sub_minimum_dust(tmp_path, cfg_paper, make_deps):
    """Regression: when the leader goes flat, our dust produced a sub-$10 IOC
    that the exchange rejects every cycle forever, each logged as a success."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(st, FakeWatcher(leader_state([], position=0.0)))
    deps.broker.market_fill("B", 0.00005, 79_660, 500)  # ~$4 of dust
    asyncio.run(cycle(deps, now_ms=1000))
    triggers = [r[0] for r in st.conn.execute("SELECT trigger FROM decisions").fetchall()]
    assert "reconcile" not in triggers


def test_failed_cancel_keeps_the_mirror_row(tmp_path, cfg_paper, make_deps):
    """Regression: closing the row on a rejected cancel orphaned a live order —
    diff_ladders can only cancel rungs it still has a row for."""
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=0.0))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))

    real_execute = deps.broker.execute
    deps.broker.execute = lambda a, ts: None if a.kind == "cancel" else real_execute(a, ts)
    w._s = leader_state([], position=0.0, ts=2000)  # he cancels the rung
    asyncio.run(cycle(deps, now_ms=2000))
    assert 1 in st.mirror_get()  # row survives so we retry the cancel
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "cancel_failed" in kinds


def test_divergence_and_anomaly_alerts(tmp_path, cfg_paper, make_deps):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=1.0))
    deps = make_deps(st, w)
    asyncio.run(cycle(deps, now_ms=1000))
    w._s = leader_state(RUNG, position=3.0, ts=2000)  # +200% in one cycle
    asyncio.run(cycle(deps, now_ms=2000))
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "leader_anomaly" in kinds
