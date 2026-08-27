from src.models import AccountState, MirrorAction, Order, RiskState, Verdict


def test_models_construct():
    o = Order(oid=1, side="B", px=57860.0, sz=0.2, ts_ms=0)
    s = AccountState(
        equity=66435.0,
        position=1.33557,
        entry_px=64249.1,
        mark_px=79660.0,
        fetched_at_ms=0,
        open_orders=[o],
    )
    a = MirrorAction(kind="place", side="B", px=57860.0, sz=0.0301, leader_oid=1)
    assert s.open_orders[0].px == 57860.0
    assert a.our_oid is None
    assert Verdict(approved=False, reason="B2_parity").approved is False
    assert RiskState.NORMAL.value == "NORMAL"


def test_position_can_be_negative_shorts_are_copied():
    s = AccountState(
        equity=1000.0, position=-0.5, entry_px=80000.0, mark_px=79000.0, fetched_at_ms=0
    )
    assert s.position == -0.5
