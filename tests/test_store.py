from src.models import AccountState
from src.store import Store


def make_store(tmp_path):
    return Store(tmp_path / "t.db")


def test_schema_and_snapshot_roundtrip(tmp_path):
    st = make_store(tmp_path)
    st.record_snapshot(
        "leader",
        AccountState(
            equity=66435, position=1.33557, entry_px=64249.1, mark_px=79660, fetched_at_ms=1000
        ),
    )
    row = st.conn.execute("SELECT who, equity FROM snapshots").fetchone()
    assert row == ("leader", 66435.0)


def test_hwm_survives_reopen(tmp_path):
    st = make_store(tmp_path)
    assert st.update_equity(1, 10_000) == 0.0
    assert round(st.update_equity(2, 8_000), 1) == -20.0
    st2 = Store(tmp_path / "t.db")  # simulated restart
    assert round(st2.update_equity(3, 8_000), 1) == -20.0  # HWM persisted, not reset


def test_mirror_map_lifecycle(tmp_path):
    st = make_store(tmp_path)
    st.mirror_put(111, 222, 57860.0, 0.2, 0.0301, 0.1505, 1000)
    assert 111 in st.mirror_get()
    st.mirror_close(111, 2000, "his_cancel")
    assert 111 not in st.mirror_get()  # mirror_get returns only open rows


def test_manual_reset_unhalts(tmp_path):
    st = make_store(tmp_path)
    st.record_event(1, "critical", "state_change", "HALT")
    assert st.latest_risk_state() == "HALT"
    st.record_event(2, "info", "manual_reset", "operator resume")
    assert st.latest_risk_state() == "NORMAL"  # the documented un-HALT path


def test_outage_open_tracks_one_alert_per_outage(tmp_path):
    st = make_store(tmp_path)
    assert st.outage_open() is False
    st.record_event(10, "warning", "ws_lost", "x")
    assert st.outage_open() is True
    st.record_event(20, "info", "ws_recovered", "x")
    assert st.outage_open() is False
