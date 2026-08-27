from src.dashboard import ReadOnlyDB, order_overlay_shapes
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
    assert db.q("INSERT INTO events(ts,level,kind,message) VALUES (1,'x','y','z')") == []
    assert db.q("SELECT COUNT(*) FROM events") == [(0,)]


def test_halt_button_writes_one_consumable_row(tmp_path):
    st = Store(tmp_path / "t.db")
    db = ReadOnlyDB(str(tmp_path / "t.db"))
    assert st.halt_requested() is False
    db.request_halt(1234)
    assert st.halt_requested() is True
    st.record_event(1235, "critical", "state_change", "HALT")
    assert st.halt_requested() is False  # consumed by the state change


def test_missing_db_returns_empty_not_crash(tmp_path):
    db = ReadOnlyDB(str(tmp_path / "nope.db"))
    assert db.q("SELECT 1") == []
