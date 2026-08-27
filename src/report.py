"""Daily report: self-contained HTML in reports/ + a Telegram summary.

ponytail: text and tables only — no inline chart image, the dashboard already
charts equity and a PNG would drag in a kaleido dependency.
"""

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.config import Config
from src.store import Store

log = structlog.get_logger(__name__)


def _day_bounds_ms(day: str) -> tuple[int, int]:
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    lo = int(start.timestamp() * 1000)
    return lo, lo + 86_400_000


def render_daily(store: Store, day: str, cfg: Config | None = None) -> tuple[str, str]:
    lo, hi = _day_bounds_ms(day)
    q = store.conn.execute

    ours = q(
        "SELECT equity, position_btc, entry_px FROM snapshots WHERE who='copy' AND ts<? "
        "ORDER BY ts DESC LIMIT 1", (hi,)
    ).fetchone() or (0.0, 0.0, None)
    leader = q(
        "SELECT position_btc FROM snapshots WHERE who='leader' AND ts<? "
        "ORDER BY ts DESC LIMIT 1", (hi,)
    ).fetchone() or (0.0,)
    curve = q(
        "SELECT equity, hwm, drawdown_pct FROM equity_curve WHERE ts<? ORDER BY ts DESC LIMIT 1",
        (hi,)
    ).fetchone() or (0.0, 0.0, 0.0)
    day_open = q(
        "SELECT equity FROM equity_curve WHERE ts>=? ORDER BY ts LIMIT 1", (lo,)
    ).fetchone() or (curve[0],)

    orders = q(
        "SELECT COUNT(*) FROM decisions WHERE ts>=? AND ts<? AND action='order'", (lo, hi)
    ).fetchone()[0]
    vetoes = q(
        "SELECT veto_reason, COUNT(*) FROM decisions WHERE ts>=? AND ts<? AND action='veto' "
        "GROUP BY veto_reason", (lo, hi)
    ).fetchall()
    fills = q(
        "SELECT COUNT(*), COALESCE(SUM(fee),0), COALESCE(SUM(crossed),0) FROM fills "
        "WHERE ts>=? AND ts<?", (lo, hi)
    ).fetchone()
    state = store.latest_risk_state()

    n_fills, fees, takers = fills
    maker_pct = 100.0 * (n_fills - takers) / n_fills if n_fills else 0.0
    day_pct = (ours[0] / day_open[0] - 1) * 100 if day_open[0] else 0.0
    veto_txt = ", ".join(f"{r or 'n/a'} x{c}" for r, c in vetoes) or "none"
    mode = (cfg.mode if cfg else "paper").upper()

    tg = (
        f"Copybot daily - {day} ({mode})\n"
        f"State: {state}\n"
        f"Equity: ${ours[0]:,.0f} ({day_pct:+.2f}% day, HWM {curve[2]:.1f}%)\n"
        f"Position: {ours[1]:.5f} BTC"
        + (f" @ ${ours[2]:,.0f}" if ours[2] else "")
        + f" | leader {leader[0]:.5f} BTC\n"
        f"Today: {orders} orders, {n_fills} fills (maker {maker_pct:.0f}%), fees ${fees:,.2f}\n"
        f"Vetoes: {veto_txt}"
    )

    rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td></tr>"
        for k, v in [
            ("State", state), ("Mode", mode),
            ("Equity", f"${ours[0]:,.2f}"), ("Day change", f"{day_pct:+.2f}%"),
            ("Drawdown vs HWM", f"{curve[2]:.2f}%"),
            ("Our position", f"{ours[1]:.5f} BTC"),
            ("Leader position", f"{leader[0]:.5f} BTC"),
            ("Orders", orders), ("Fills", n_fills),
            ("Maker %", f"{maker_pct:.0f}%"), ("Fees", f"${fees:,.2f}"),
            ("Vetoes", veto_txt),
        ]
    )
    html = (
        f"<!doctype html><meta charset='utf-8'><title>Copybot {day}</title>"
        "<style>body{background:#0d1117;color:#e6edf3;font-family:system-ui;padding:32px}"
        "table{border-collapse:collapse}td{padding:6px 18px 6px 0;border-bottom:1px solid #30363d}"
        "td:first-child{color:#8b949e}</style>"
        f"<h1>Copybot daily - {day}</h1><table>{rows}</table>"
    )
    return html, tg


def send_telegram(cfg: Config, text: str) -> None:
    if not (cfg.telegram.enabled and cfg.telegram.bot_token and cfg.telegram.chat_id):
        return
    try:
        data = urllib.parse.urlencode(
            {"chat_id": cfg.telegram.chat_id, "text": text}
        ).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{cfg.telegram.bot_token}/sendMessage",
            data=data, timeout=10,
        ).read()
    except Exception:
        log.warning("telegram_send_failed")


def write_daily(store: Store, day: str, cfg: Config) -> Path:
    html, tg = render_daily(store, day, cfg)
    out = Path("reports") / f"daily-{day.replace('-', '')}.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(html, encoding="utf-8")
    send_telegram(cfg, tg)
    log.info("daily_report_written", path=str(out))
    return out


if __name__ == "__main__":  # smoke check against the live DB
    import sys

    from src.config import load_config

    c = load_config("config.yaml")
    d = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(json.dumps({"path": str(write_daily(Store(c.storage.db_path), d, c))}))
