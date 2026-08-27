from src.models import MirrorAction
from src.paper import MAKER_FEE, PaperBroker
from src.store import Store


def place(pb, st, leader_oid, px, sz, leader_sz, now_ms=1000, scale=0.1505):
    a = MirrorAction(kind="place", side="B", px=px, sz=sz, leader_oid=leader_oid)
    oid = pb.execute(a, now_ms=now_ms)
    st.mirror_put(leader_oid, oid, px, leader_sz, sz, scale, now_ms)
    return oid


def test_place_then_leader_fill_creates_our_fill(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    place(pb, st, 7, 57_860.0, 0.0301, 0.2)
    pb.on_leader_fill(7, fill_sz=0.2, px=57_860.0, now_ms=2000)
    s = pb.state(mark_px=57_860.0, now_ms=3000)
    assert s.position == 0.0301
    assert not s.open_orders  # our order consumed
    fee = 0.0301 * 57_860.0 * MAKER_FEE
    assert round(10_000 - s.equity, 4) == round(fee, 4)  # only the fee, at fill price


def test_partial_leader_fill_leaves_remainder(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    place(pb, st, 7, 57_860.0, 0.0300, 0.2)
    pb.on_leader_fill(7, fill_sz=0.1, px=57_860.0, now_ms=2000)  # half his rung
    s = pb.state(mark_px=57_860.0, now_ms=3000)
    assert s.position == 0.015
    assert s.open_orders[0].sz == 0.015


def test_cancel_removes_resting_order(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    oid = place(pb, st, 8, 60_000.0, 0.01, 0.0664, now_ms=1)
    pb.execute(
        MirrorAction(kind="cancel", side="B", px=60_000.0, sz=0.01, leader_oid=8, our_oid=oid),
        now_ms=2,
    )
    assert not pb.state(mark_px=60_000.0, now_ms=3).open_orders


def test_restart_rehydrates_book_and_equity(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    place(pb, st, 7, 57_860.0, 0.0301, 0.2)
    pb.on_leader_fill(7, fill_sz=0.2, px=57_860.0, now_ms=2000)
    equity_before = pb.state(mark_px=57_860.0, now_ms=3000).equity

    pb2 = PaperBroker(Store(tmp_path / "t.db"), start_equity=10_000)  # restart
    s = pb2.state(mark_px=57_860.0, now_ms=4000)
    assert s.position == 0.0301  # position survived
    assert round(s.equity, 4) == round(equity_before, 4)  # NOT reset to 10k


def test_restart_rehydrates_open_orders(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    place(pb, st, 7, 57_860.0, 0.0301, 0.2)
    pb2 = PaperBroker(Store(tmp_path / "t.db"), start_equity=10_000)
    assert len(pb2.open) == 1
    pb2.on_leader_fill(7, fill_sz=0.2, px=57_860.0, now_ms=5000)  # still fillable
    assert pb2.position == 0.0301


def test_same_cycle_double_fill_keeps_both_rows(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    for loid, px in [(1, 60_000.0), (2, 59_000.0)]:
        place(pb, st, loid, px, 0.01, 0.0664, now_ms=1)
        pb.on_leader_fill(loid, fill_sz=0.0664, px=px, now_ms=2000)  # same timestamp
    assert st.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2


def test_cancel_all_clears_book(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    place(pb, st, 1, 60_000.0, 0.01, 0.0664)
    place(pb, st, 2, 59_000.0, 0.01, 0.0664)
    assert pb.cancel_all() == 2
    assert pb.open == {}


def test_multi_partial_rung_fills_completely(tmp_path):
    """Regression: ratio must be cumulative against his ORIGINAL rung size, or a
    rung filled in partials leaves us permanently under-mirrored."""
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    place(pb, st, 7, 57_860.0, 0.0300, 0.2)
    pb.on_leader_fill(7, fill_sz=0.1, px=57_860.0, now_ms=2000)  # half
    pb.on_leader_fill(7, fill_sz=0.1, px=57_860.0, now_ms=3000)  # rest
    assert pb.position == 0.03  # fully mirrored, not 0.0225
    assert not pb.open  # no phantom remainder resting


def test_oid_not_reused_after_restart(tmp_path):
    """Regression: reusing a retired oid would clobber its orders row and orphan
    the fills that reference it."""
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    first = place(pb, st, 7, 57_860.0, 0.0301, 0.2)
    pb.on_leader_fill(7, fill_sz=0.2, px=57_860.0, now_ms=2000)  # order retired

    pb2 = PaperBroker(Store(tmp_path / "t.db"), start_equity=10_000)
    second = place(pb2, Store(tmp_path / "t.db"), 8, 60_000.0, 0.01, 0.0664)
    assert second != first
    statuses = dict(st.conn.execute("SELECT oid, status FROM orders"))
    assert statuses[first] == "filled"  # history intact


def test_entry_px_survives_a_flip_through_zero(tmp_path):
    """Regression: entry_px was derived from a value that also carried realized
    PnL, so after a flip it reported a price the position never had."""
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    pb.market_fill("B", 1.0, 100.0, now_ms=1)  # long 1 @ 100
    pb.market_fill("A", 2.0, 110.0, now_ms=2)  # sell 2 -> short 1 @ 110
    s = pb.state(mark_px=110.0, now_ms=3)
    assert s.position == -1.0
    assert s.entry_px == 110.0  # not 120
    assert abs(s.equity - (pb.cash + 0.0)) < 1e-9  # flat uPnL at the entry price


def test_round_trip_realizes_pnl_into_equity(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    pb.market_fill("B", 0.1, 80_000.0, now_ms=1)
    pb.market_fill("A", 0.1, 81_000.0, now_ms=2)
    s = pb.state(mark_px=81_000.0, now_ms=3)
    assert s.position == 0.0
    fees = 0.1 * 80_000 * 0.00045 + 0.1 * 81_000 * 0.00045
    assert abs(s.equity - (10_000 + 100 - fees)) < 1e-6  # +$100 realized


def test_market_fill_books_taker_fee_and_shorts(tmp_path):
    st = Store(tmp_path / "t.db")
    pb = PaperBroker(st, start_equity=10_000)
    pb.market_fill("A", 0.05, 80_000.0, now_ms=1)
    assert pb.position == -0.05
    row = st.conn.execute("SELECT crossed FROM fills").fetchone()
    assert row[0] == 1
