import asyncio

from src.main import Deps, cycle
from src.models import AccountState, Order
from src.paper import PaperBroker
from src.store import Store


def leader_state(orders, position=1.33557, ts=1000, equity=66_435):
    return AccountState(
        equity=equity, position=position, entry_px=64_249.1, mark_px=79_660,
        fetched_at_ms=ts, open_orders=orders,
    )


class FakeWatcher:
    def __init__(self, state, fills=None):
        self._s, self._fills = state, fills or []

    async def fetch(self, now_ms):
        return self._s

    async def fetch_fills(self, since_ms):
        return [f for f in self._fills if f["time"] >= since_ms]


def make_deps(cfg, st, watcher):
    return Deps(cfg=cfg, store=st, watcher=watcher,
                broker=PaperBroker(st, cfg.paper.start_equity))


RUNG = [Order(oid=1, side="B", px=57_860, sz=0.2, ts_ms=0)]


def test_cycle_mirrors_new_leader_order(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = make_deps(cfg_paper, st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))
    mirror = st.mirror_get()
    assert 1 in mirror and mirror[1]["our_sz"] > 0
    assert ("order",) in st.conn.execute("SELECT action FROM decisions").fetchall()


def test_cycle_is_idempotent(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = make_deps(cfg_paper, st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))
    asyncio.run(cycle(deps, now_ms=2000))  # second run must not duplicate
    assert len(deps.broker.open) == 1


def test_leader_fill_becomes_our_fill_not_cancel(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG))
    deps = make_deps(cfg_paper, st, w)
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


def test_leader_cancel_is_not_a_fill(tmp_path, cfg_paper):
    """Leader position held flat so reconciliation stays out of the way — this
    isolates the cancel path: his order vanishing WITHOUT a fill must not book one."""
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=0.0))
    deps = make_deps(cfg_paper, st, w)
    asyncio.run(cycle(deps, now_ms=1000))
    w._s = leader_state([], position=0.0, ts=2000)  # gone, but NO fill reported
    asyncio.run(cycle(deps, now_ms=2000))
    assert deps.broker.position == 0.0
    assert st.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    reason = st.conn.execute(
        "SELECT close_reason FROM mirror_map WHERE leader_oid=1"
    ).fetchone()[0]
    assert reason == "his_cancel"


def test_first_cycle_acquires_leader_position_via_reconciliation(tmp_path, cfg_paper):
    """Documented consequence of copying a leader who already holds a position:
    we market-buy our scaled share at TODAY's price, not his average entry."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(cfg_paper, st, FakeWatcher(leader_state([], position=1.33557)))
    asyncio.run(cycle(deps, now_ms=1000))
    scale = cfg_paper.paper.start_equity / 66_435
    assert abs(deps.broker.position - 1.33557 * scale) < 0.001
    crossed = st.conn.execute("SELECT crossed FROM fills").fetchone()[0]
    assert crossed == 1  # taker, with the cost that implies


def test_fetch_failure_reuses_last_leader_and_ages_into_warning(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state([], ts=1000))
    deps = make_deps(cfg_paper, st, w)
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


def test_first_cycle_fetch_failure_does_not_crash(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state([]))

    async def boom(now_ms):
        raise ConnectionError

    w.fetch = boom
    deps = make_deps(cfg_paper, st, w)
    asyncio.run(cycle(deps, now_ms=1000))  # no baseline: must return cleanly
    assert st.latest_risk_state() == "NORMAL"


def test_halt_cancels_and_flattens(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = make_deps(cfg_paper, st, FakeWatcher(leader_state(RUNG)))
    asyncio.run(cycle(deps, now_ms=1000))  # HWM established ~10k, rung mirrored
    deps.broker.market_fill("B", 0.1, 79_660, 1500)  # give us a position
    deps.broker.cash -= 6_000  # simulate a catastrophic loss
    deps.broker._save()

    asyncio.run(cycle(deps, now_ms=2000))
    assert st.latest_risk_state() == "HALT"
    assert deps.broker.open == {}
    assert deps.broker.position == 0.0
    assert st.mirror_get() == {}


def test_halt_is_sticky_across_cycles(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    deps = make_deps(cfg_paper, st, FakeWatcher(leader_state(RUNG)))
    st.record_event(1, "critical", "state_change", "HALT")
    asyncio.run(cycle(deps, now_ms=1000))
    assert st.latest_risk_state() == "HALT"
    assert deps.broker.open == {}  # no new mirror orders placed


def test_reconciliation_closes_drift(tmp_path, cfg_paper):
    """His taker trade has no resting order to mirror — reconciliation catches it."""
    st = Store(tmp_path / "t.db")
    deps = make_deps(cfg_paper, st, FakeWatcher(leader_state([], position=1.33557)))
    asyncio.run(cycle(deps, now_ms=1000))
    assert deps.broker.position > 0  # taker top-up happened
    triggers = [r[0] for r in st.conn.execute("SELECT trigger FROM decisions").fetchall()]
    assert "reconcile" in triggers


def test_divergence_and_anomaly_alerts(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    w = FakeWatcher(leader_state(RUNG, position=1.0))
    deps = make_deps(cfg_paper, st, w)
    asyncio.run(cycle(deps, now_ms=1000))
    w._s = leader_state(RUNG, position=3.0, ts=2000)  # +200% in one cycle
    asyncio.run(cycle(deps, now_ms=2000))
    kinds = [r[0] for r in st.conn.execute("SELECT kind FROM events").fetchall()]
    assert "leader_anomaly" in kinds
