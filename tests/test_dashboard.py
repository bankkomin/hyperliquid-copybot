from src.dashboard import (
    ReadOnlyDB,
    build_view,
    ladder_at,
    order_overlay_shapes,
    replay_range,
    slider_to_ts,
    snapshot_at,
    state_at,
)
from src.store import Store


def test_overlay_one_shape_per_pending_order_with_catseye_style():
    orders = [("leader", "B", 57_860.0, 0.2), ("copy", "A", 83_000.0, 0.05)]
    shapes, annotations = order_overlay_shapes(orders)
    assert len(shapes) == 2 and len(annotations) == 2
    buy, sell = shapes
    assert buy["y0"] == 57_860.0 and buy["line"]["dash"] == "dash"
    assert buy["line"]["color"].startswith("rgb(38, 166, 91")  # green buy
    assert sell["line"]["color"].startswith("rgb(214, 69, 65")  # red sell
    assert buy["opacity"] == 0.5
    assert "0.2 @ 57,860" in annotations[0]["text"]


def test_overlay_empty_orders_returns_empty():
    assert order_overlay_shapes([]) == ([], [])


def test_readonly_db_cannot_write(tmp_path):
    Store(tmp_path / "t.db")  # create schema
    db = ReadOnlyDB(str(tmp_path / "t.db"))
    assert db.q("SELECT COUNT(*) FROM events") == [(0,)]
    # a write through the read-only handle must fail, not corrupt state
    assert db.q("INSERT INTO events(ts,level,kind,message) VALUES (1,'x','y','z')") is None
    assert db.q("SELECT COUNT(*) FROM events") == [(0,)]


def test_halt_button_writes_one_consumable_row(tmp_path):
    st = Store(tmp_path / "t.db")
    db = ReadOnlyDB(str(tmp_path / "t.db"))
    assert st.halt_requested() is False
    db.request_halt(1234)
    assert st.halt_requested() is True
    st.record_event(1235, "critical", "state_change", "HALT")
    assert st.halt_requested() is False  # consumed by the state change


def test_failed_read_is_unknown_not_a_green_normal(tmp_path):
    """Regression: a locked/unreadable DB returned [] and painted a reassuring
    green NORMAL banner over an account that could be halted and deep in
    drawdown."""
    from src.dashboard import resolve_state

    db = ReadOnlyDB(str(tmp_path / "nope.db"))
    assert db.q("SELECT 1 FROM events") is None  # failure, not emptiness
    assert db.rows("SELECT 1 FROM events") == []  # display panels degrade quietly
    assert resolve_state(None) == "UNKNOWN"
    assert resolve_state([]) == "NORMAL"


# ---- replay ---------------------------------------------------------------

def _history(tmp_path):
    """A store with two distinct eras so 'as of' has something to get wrong."""
    from src.models import AccountState

    st = Store(tmp_path / "t.db")
    db = ReadOnlyDB(str(tmp_path / "t.db"))

    def snap(ts, pos, eq):
        st.record_snapshot("copy", AccountState(
            equity=eq, position=pos, entry_px=79_000.0, mark_px=80_000.0, fetched_at_ms=ts))
        st.record_snapshot("leader", AccountState(
            equity=66_435, position=1.3, entry_px=64_249.1, mark_px=80_000.0, fetched_at_ms=ts))
        st.update_equity(ts, eq)

    # T=1000: one rung mirrored, flat
    st.conn.execute("INSERT INTO orders(oid,ts,side,px,sz,exec_style,status)"
                    " VALUES (-1,1000,'B',57860.0,0.03,'maker','canceled')")
    st.conn.commit()
    st.mirror_put(1, -1, 57_860.0, 0.2, 0.03, 0.15, 1000)
    st.conn.execute("INSERT INTO leader_open_orders VALUES (1000,1,'B',57860.0,0.2)")
    st.conn.commit()
    snap(1000, 0.0, 10_000)

    # T=3000: he pulled it, we cancelled; a second rung opened instead
    st.mirror_close(1, 3000, "his_cancel")
    st.conn.execute("INSERT INTO orders(oid,ts,side,px,sz,exec_style,status)"
                    " VALUES (-2,3000,'B',62944.0,0.02,'maker','open')")
    st.conn.commit()
    st.mirror_put(2, -2, 62_944.0, 0.12, 0.02, 0.15, 3000)
    st.conn.execute("INSERT INTO leader_open_orders VALUES (3000,2,'B',62944.0,0.12)")
    st.conn.commit()
    snap(3000, 0.2, 10_500)
    return st, db


def test_snapshot_at_returns_the_state_of_that_moment(tmp_path):
    st, db = _history(tmp_path)
    assert snapshot_at(db, "copy", 2000)[1] == 0.0     # flat back then
    assert snapshot_at(db, "copy", 3500)[1] == 0.2     # position later
    assert snapshot_at(db, "copy", None)[1] == 0.2     # live = newest


def test_ladder_at_reconstructs_the_book_of_that_moment(tmp_path):
    st, db = _history(tmp_path)
    leader, ours = ladder_at(db, 2000)
    assert [round(p) for _, p, _ in leader] == [57_860]
    assert [round(p) for _, p, _ in ours] == [57_860]  # from mirror_map lifecycle

    leader, ours = ladder_at(db, 3500)
    assert [round(p) for _, p, _ in leader] == [62_944]
    assert [round(p) for _, p, _ in ours] == [62_944]  # the first rung is closed


def test_ladder_at_before_any_history_is_empty(tmp_path):
    st, db = _history(tmp_path)
    assert ladder_at(db, 500) == ([], [])


def test_emptied_ladder_replays_as_empty(tmp_path):
    """Regression: history is only written on change, and an empty ladder wrote
    NO rows — so replay would show the previous ladder forever."""
    from src.models import Order

    st = Store(tmp_path / "t.db")
    db = ReadOnlyDB(str(tmp_path / "t.db"))
    st.set_live_ladder([Order(oid=1, side="B", px=57_860, sz=0.2, ts_ms=0)], 1000)
    st.set_live_ladder([], 2000)  # he pulled everything
    assert ladder_at(db, 1500)[0] != []   # still resting then
    assert ladder_at(db, 2500)[0] == []   # gone, and replay knows it


def test_state_at_tracks_halt_and_reset(tmp_path):
    st = Store(tmp_path / "t.db")
    db = ReadOnlyDB(str(tmp_path / "t.db"))
    st.record_event(1000, "critical", "state_change", "HALT")
    st.record_event(3000, "info", "manual_reset", "operator")
    assert state_at(db, 500) == "NORMAL"
    assert state_at(db, 2000) == "HALT"
    assert state_at(db, 4000) == "NORMAL"
    assert state_at(db, None) == "NORMAL"


def test_slider_maps_to_timestamps_and_100_means_live():
    assert slider_to_ts(100, 1_000, 2_000) is None   # live
    assert slider_to_ts(0, 1_000, 2_000) == 1_000
    assert slider_to_ts(50, 1_000, 2_000) == 1_500
    assert slider_to_ts(50, 0, 0) is None            # no history yet


def test_replay_range_handles_an_empty_store(tmp_path):
    Store(tmp_path / "t.db")
    assert replay_range(ReadOnlyDB(str(tmp_path / "t.db"))) == (0, 0)


def test_build_view_renders_at_a_past_moment(tmp_path, cfg_paper):
    st, db = _history(tmp_path)
    past = build_view(db, cfg_paper, at_ts=2000)
    live = build_view(db, cfg_paper, at_ts=None)
    assert len(past) == len(live)
    # the replay chart is labelled, and its overlay is the OLD rung
    assert "[REPLAY]" in past[1].figure.layout.title.text
    assert any("57,860" in a.text for a in past[1].figure.layout.annotations)
    assert not any("62,944" in a.text for a in past[1].figure.layout.annotations)
