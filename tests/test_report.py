from src.models import AccountState
from src.report import render_daily
from src.store import Store

DAY = "2026-08-27"
TS = 1_787_788_800_000  # 2026-08-27T00:00:00Z + a few hours


def test_report_contains_headline_numbers(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    st.record_snapshot(
        "copy",
        AccountState(equity=10_241, position=0.18634, entry_px=79_880.0,
                     mark_px=80_500.0, fetched_at_ms=TS + 3_600_000),
    )
    st.record_snapshot(
        "leader",
        AccountState(equity=66_435, position=1.33557, entry_px=64_249.1,
                     mark_px=80_500.0, fetched_at_ms=TS + 3_600_000),
    )
    st.update_equity(TS + 3_600_000, 10_241)
    st.record_decision(TS + 3_600_000, "poll", "veto", "NORMAL", veto_reason="B2_parity")

    html, tg = render_daily(st, DAY, cfg_paper)
    assert "10,241" in tg and "B2_parity" in tg
    assert "PAPER" in tg
    assert "<title>" in html.lower() and "10,241" in html


def test_report_on_empty_day_does_not_crash(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    html, tg = render_daily(st, DAY, cfg_paper)
    assert "Copybot daily" in tg and "<table>" in html


def test_fill_lag_is_bounded_to_the_matching_fill(tmp_path, cfg_paper):
    """Regression: the join had no time bound, so after weeks of laddering at
    repeated rung prices it averaged the age of the whole leader fill history."""
    st = Store(tmp_path / "t.db")
    old = TS - 10 * 86_400_000  # his fill at the same price ten days ago
    for ts in (old, TS + 1000):
        st.conn.execute(
            "INSERT INTO leader_fills VALUES (?,?,'B',57860.0,0.2,0,'Open Long')",
            (ts, ts),
        )
    st.conn.execute(
        "INSERT INTO fills(oid,ts,side,px,sz,crossed,closed_pnl,fee)"
        " VALUES (1,?,'B',57860.0,0.03,0,0,0.1)",
        (TS + 9000,),  # ours landed 8s after his
    )
    st.conn.commit()
    _, tg = render_daily(st, DAY, cfg_paper)
    assert "fill lag 8s" in tg  # not 864,000s


def test_funding_reports_the_latest_value_not_max(tmp_path, cfg_paper):
    """Regression: funding paid is negative and unwritten rows default to 0, so
    MAX() reported $0.00 no matter how much carry the position bled."""
    st = Store(tmp_path / "t.db")
    st.update_equity(TS + 1000, 10_000)  # funding_cum defaults to 0
    st.update_equity(TS + 2000, 9_900)
    st.conn.execute("UPDATE equity_curve SET funding_cum=-412.75 WHERE ts=?", (TS + 2000,))
    st.conn.commit()
    _, tg = render_daily(st, DAY, cfg_paper)
    assert "funding $-412.75" in tg


def test_maker_percentage_counts_takers(tmp_path, cfg_paper):
    st = Store(tmp_path / "t.db")
    for crossed in (0, 0, 0, 1):  # 3 maker, 1 taker
        st.conn.execute(
            "INSERT INTO fills(oid,ts,side,px,sz,crossed,closed_pnl,fee)"
            " VALUES (1,?,'B',80000,0.01,?,0,0.5)",
            (TS + 1000, crossed),
        )
    st.conn.commit()
    _, tg = render_daily(st, DAY, cfg_paper)
    assert "maker 75%" in tg
