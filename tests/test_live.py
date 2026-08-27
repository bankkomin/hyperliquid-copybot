from unittest.mock import MagicMock

import pytest

from src.live import LiveBroker, build_live_broker
from src.models import MirrorAction
from src.store import Store

OK_RESTING = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 555}}]}}}


def broker(tmp_path):
    ex, info = MagicMock(), MagicMock()
    ex.order.return_value = OK_RESTING
    return LiveBroker(ex, info, "0xME", Store(tmp_path / "t.db")), ex


def test_place_uses_alo_at_his_exact_price(tmp_path):
    b, ex = broker(tmp_path)
    oid = b.execute(
        MirrorAction(kind="place", side="B", px=57_860.0, sz=0.0301, leader_oid=1), now_ms=1
    )
    assert oid == 555
    kwargs = ex.order.call_args.kwargs
    assert kwargs["limit_px"] == 57_860.0  # HIS price, never our own idea of one
    assert kwargs["order_type"] == {"limit": {"tif": "Alo"}}  # post-only = maker
    assert kwargs["is_buy"] is True and kwargs["sz"] == 0.0301


def test_place_persists_the_order_row(tmp_path):
    b, _ = broker(tmp_path)
    b.execute(MirrorAction(kind="place", side="B", px=57_860.0, sz=0.03, leader_oid=1), now_ms=7)
    row = b.store.conn.execute("SELECT oid, status, exec_style FROM orders").fetchone()
    assert row == (555, "open", "maker")


def test_taker_caps_slippage_both_directions(tmp_path):
    b, ex = broker(tmp_path)
    b.market_fill("B", 0.01, 80_000.0, now_ms=1)
    assert ex.order.call_args.kwargs["limit_px"] == 80_120.0  # mark * 1.0015
    b.market_fill("A", 0.01, 80_000.0, now_ms=1)
    assert ex.order.call_args.kwargs["limit_px"] == 79_880.0  # mark * 0.9985


def test_rejected_alo_returns_none_not_phantom_oid(tmp_path):
    b, ex = broker(tmp_path)
    ex.order.return_value = {
        "status": "ok",
        "response": {"data": {"statuses": [{"error": "Post only order would have matched"}]}},
    }
    oid = b.execute(
        MirrorAction(kind="place", side="B", px=80_100.0, sz=0.01, leader_oid=1), now_ms=1
    )
    assert oid is None  # the cycle must not record a mirror row
    assert b.store.conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_top_level_err_returns_none(tmp_path):
    b, ex = broker(tmp_path)
    ex.order.return_value = {"status": "err", "response": "Insufficient margin"}
    assert b.execute(
        MirrorAction(kind="place", side="B", px=70_000.0, sz=0.01, leader_oid=1), now_ms=1
    ) is None


def test_order_exception_returns_none(tmp_path):
    b, ex = broker(tmp_path)
    ex.order.side_effect = ConnectionError("boom")
    assert b.execute(
        MirrorAction(kind="place", side="B", px=70_000.0, sz=0.01, leader_oid=1), now_ms=1
    ) is None


def test_cancel_all_cancels_every_open_order(tmp_path):
    b, ex = broker(tmp_path)
    b.info.open_orders.return_value = [{"coin": "BTC", "oid": 1}, {"coin": "BTC", "oid": 2}]
    assert b.cancel_all() == 2
    assert ex.cancel.call_count == 2


def test_cancel_all_survives_one_failing_cancel(tmp_path):
    """Safety-critical: one 429 must not leave the rest of the ladder resting."""
    b, ex = broker(tmp_path)
    b.info.open_orders.return_value = [{"coin": "BTC", "oid": n} for n in (1, 2, 3)]
    ex.cancel.side_effect = [None, ConnectionError("429"), None]
    assert b.cancel_all() == 2  # CONFIRMED cancels, not attempts
    assert ex.cancel.call_count == 3  # kept going past the failure


def test_cancel_all_ignores_other_coins(tmp_path):
    b, ex = broker(tmp_path)
    b.info.open_orders.return_value = [{"coin": "ETH", "oid": 9}, {"coin": "BTC", "oid": 1}]
    assert b.cancel_all() == 1
    ex.cancel.assert_called_once_with("BTC", 1)


def test_ingest_our_fills_persists_exchange_tids(tmp_path):
    b, _ = broker(tmp_path)
    b.info.user_fills_by_time.return_value = [
        {"coin": "BTC", "tid": 11, "oid": 555, "time": 1000, "side": "B", "px": "57860.0",
         "sz": "0.03", "crossed": False, "closedPnl": "0.0", "fee": "0.26"},
        {"coin": "ETH", "tid": 12, "oid": 1, "time": 1001, "side": "B", "px": "3000",
         "sz": "1", "crossed": True, "closedPnl": "0", "fee": "1"},
    ]
    assert b.ingest_our_fills(2000) == 1  # BTC only
    row = b.store.conn.execute("SELECT tid, sz, crossed FROM fills").fetchone()
    assert row == (11, 0.03, 0)


def test_ingest_our_fills_is_idempotent(tmp_path):
    b, _ = broker(tmp_path)
    fill = {"coin": "BTC", "tid": 11, "oid": 5, "time": 1000, "side": "B", "px": "1",
            "sz": "1", "crossed": False, "closedPnl": "0", "fee": "0"}
    b.info.user_fills_by_time.return_value = [fill]
    b.ingest_our_fills(2000)
    b.info.user_fills_by_time.return_value = [fill]  # exchange replays it
    b.ingest_our_fills(3000)
    assert b.store.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


def test_live_mode_without_secrets_refuses(tmp_path, cfg_live, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no secrets.yaml here
    with pytest.raises(SystemExit):
        build_live_broker(cfg_live, Store(tmp_path / "t.db"))


def test_live_mode_with_malformed_key_refuses(tmp_path, cfg_live, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "secrets.yaml").write_text("api_wallet_key: 'not-a-key'\n")
    with pytest.raises(SystemExit):
        build_live_broker(cfg_live, Store(tmp_path / "t.db"))
