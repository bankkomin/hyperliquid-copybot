from src.watcher import parse_clearinghouse


def test_parse_real_leader_capture(leader_capture):
    ch, oo = leader_capture
    s = parse_clearinghouse(ch, oo, mark_px=79_660.0, now_ms=42)
    assert s.equity == float(ch["marginSummary"]["accountValue"])
    assert s.position == 1.33557
    assert s.entry_px == 64249.1
    assert len(s.open_orders) == len(oo)
    assert s.open_orders[0].side in ("B", "A")
    assert s.fetched_at_ms == 42


def test_parse_flat_account():
    s = parse_clearinghouse(
        {"marginSummary": {"accountValue": "1000.0"}, "assetPositions": []},
        [], mark_px=80_000.0, now_ms=1,
    )
    assert s.position == 0.0 and s.entry_px is None


def test_parse_ignores_non_btc():
    raw = {
        "marginSummary": {"accountValue": "1000.0"},
        "assetPositions": [{"position": {"coin": "ETH", "szi": "5.0", "entryPx": "3000"}}],
    }
    s = parse_clearinghouse(raw, [{"coin": "ETH", "oid": 1, "side": "B",
                                   "limitPx": "3000", "sz": "1", "timestamp": 0}],
                            mark_px=80_000.0, now_ms=1)
    assert s.position == 0.0 and s.open_orders == []
