"""SQLite store — the single source of truth.

WAL mode so the dashboard can read while the bot writes. `decisions`, `fills`
and `events` are append-only: the audit trail is the point.
"""

import sqlite3
from pathlib import Path

from src.models import AccountState

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots(
  ts INTEGER NOT NULL, who TEXT NOT NULL, equity REAL, position_btc REAL,
  entry_px REAL, upnl REAL, leverage REAL, mark_px REAL, PRIMARY KEY(ts, who));
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, trigger TEXT,
  leader_pos REAL, scale REAL, target REAL, delta REAL,
  action TEXT NOT NULL, veto_reason TEXT, risk_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS orders(
  oid INTEGER PRIMARY KEY, decision_id INTEGER, ts INTEGER, side TEXT,
  px REAL, sz REAL, exec_style TEXT, status TEXT,
  filled_sz REAL DEFAULT 0, avg_px REAL, fees REAL);
CREATE TABLE IF NOT EXISTS fills(
  tid INTEGER PRIMARY KEY, oid INTEGER, ts INTEGER, side TEXT, px REAL,
  sz REAL, crossed INTEGER, closed_pnl REAL, fee REAL);
CREATE TABLE IF NOT EXISTS paper_state(
  id INTEGER PRIMARY KEY CHECK(id=1), cash REAL, position REAL,
  avg_entry REAL, realized REAL);
CREATE TABLE IF NOT EXISTS leader_fills(
  tid INTEGER PRIMARY KEY, ts INTEGER, side TEXT, px REAL, sz REAL,
  crossed INTEGER, dir TEXT);
-- History: appended only when the ladder actually CHANGES (PRD 10.1).
CREATE TABLE IF NOT EXISTS leader_open_orders(
  snapshot_ts INTEGER, oid INTEGER, side TEXT, px REAL, sz REAL,
  PRIMARY KEY(snapshot_ts, oid));
-- Current state, rewritten every cycle. The dashboard reads THIS so an emptied
-- ladder actually disappears from the overlay.
CREATE TABLE IF NOT EXISTS leader_ladder_live(
  oid INTEGER PRIMARY KEY, side TEXT, px REAL, sz REAL, ts INTEGER);
-- Append-only rung lifecycle: one row per (leader order, mirror attempt). Keyed
-- by id, not leader_oid, so a cancel+replace of the same rung keeps both records
-- and close_reason survives.
CREATE TABLE IF NOT EXISTS mirror_map(
  id INTEGER PRIMARY KEY, leader_oid INTEGER NOT NULL, our_oid INTEGER, px REAL,
  leader_sz REAL, our_sz REAL, scale_used REAL,
  created_ts INTEGER, closed_ts INTEGER, close_reason TEXT,
  leader_filled REAL DEFAULT 0, our_filled REAL DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_mirror_open ON mirror_map(leader_oid, closed_ts);
CREATE TABLE IF NOT EXISTS equity_curve(
  ts INTEGER PRIMARY KEY, equity REAL, hwm REAL, drawdown_pct REAL,
  funding_cum REAL DEFAULT 0, fees_cum REAL DEFAULT 0, realized_cum REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts INTEGER, level TEXT, kind TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts);
"""

# A ws_lost outage is "open" until a ws_recovered row lands after it.
_OUTAGE_OPEN = (
    "SELECT 1 FROM events WHERE kind='ws_lost' AND ts > "
    "(SELECT COALESCE(MAX(ts),0) FROM events WHERE kind='ws_recovered')"
)


class Store:
    def __init__(self, db_path: str | Path):
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path), timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def record_snapshot(self, who: str, s: AccountState) -> None:
        upnl = (s.mark_px - s.entry_px) * s.position if s.entry_px else 0.0
        lev = abs(s.position) * s.mark_px / s.equity if s.equity else 0.0
        self.conn.execute(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?)",
            (s.fetched_at_ms, who, s.equity, s.position, s.entry_px, upnl, lev, s.mark_px),
        )
        self.conn.commit()

    def record_decision(
        self,
        ts_ms: int,
        trigger: str,
        action: str,
        risk_state: str,
        veto_reason: str = "",
        leader_pos: float | None = None,
        scale: float | None = None,
        target: float | None = None,
        delta: float | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions(ts,trigger,leader_pos,scale,target,delta,action,"
            "veto_reason,risk_state) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts_ms, trigger, leader_pos, scale, target, delta, action, veto_reason, risk_state),
        )
        self.conn.commit()
        return cur.lastrowid

    def record_event(self, ts_ms: int, level: str, kind: str, message: str) -> None:
        self.conn.execute(
            "INSERT INTO events(ts,level,kind,message) VALUES (?,?,?,?)",
            (ts_ms, level, kind, message),
        )
        self.conn.commit()

    def halt_requested(self) -> bool:
        """True when the dashboard's HALT button was pressed since the last
        state change — the button's only job is to insert that row."""
        return (
            self.conn.execute(
                "SELECT 1 FROM events WHERE kind='halt_requested' AND id > "
                "(SELECT COALESCE(MAX(id),0) FROM events "
                " WHERE kind IN ('state_change','manual_reset'))"
            ).fetchone()
            is not None
        )

    def outage_open(self) -> bool:
        """True while a ws_lost has no matching ws_recovered — one alert per outage."""
        return self.conn.execute(_OUTAGE_OPEN).fetchone() is not None

    def mirror_get(self) -> dict[int, dict]:
        cur = self.conn.execute(
            "SELECT leader_oid, our_oid, px, leader_sz, our_sz, scale_used, "
            "leader_filled, our_filled FROM mirror_map WHERE closed_ts IS NULL"
        )
        cols = [d[0] for d in cur.description]
        return {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}

    def mirror_put(
        self,
        leader_oid: int,
        our_oid: int,
        px: float,
        leader_sz: float,
        our_sz: float,
        scale: float,
        ts_ms: int,
    ) -> None:
        self.conn.execute(
            "INSERT INTO mirror_map(leader_oid,our_oid,px,leader_sz,our_sz,scale_used,"
            "created_ts,closed_ts,close_reason,leader_filled,our_filled)"
            " VALUES (?,?,?,?,?,?,?,NULL,NULL,0,0)",
            (leader_oid, our_oid, px, leader_sz, our_sz, scale, ts_ms),
        )
        self.conn.commit()

    def mirror_fill(self, leader_oid: int, leader_filled: float, our_filled: float) -> None:
        """Cumulative fill progress on one mirrored rung."""
        self.conn.execute(
            "UPDATE mirror_map SET leader_filled=?, our_filled=? "
            "WHERE leader_oid=? AND closed_ts IS NULL",
            (leader_filled, our_filled, leader_oid),
        )
        self.conn.commit()

    def set_live_ladder(self, orders, ts_ms: int) -> bool:
        """Replace the leader's current ladder. Returns True when it CHANGED
        (the caller then appends a history snapshot)."""
        cur = {
            (r[0], r[1], r[2], r[3])
            for r in self.conn.execute("SELECT oid, side, px, sz FROM leader_ladder_live")
        }
        new = {(o.oid, o.side, o.px, o.sz) for o in orders}
        self.conn.execute("DELETE FROM leader_ladder_live")
        self.conn.executemany(
            "INSERT INTO leader_ladder_live VALUES (?,?,?,?,?)",
            [(o.oid, o.side, o.px, o.sz, ts_ms) for o in orders],
        )
        if new != cur:
            self.conn.executemany(
                "INSERT OR REPLACE INTO leader_open_orders VALUES (?,?,?,?,?)",
                [(ts_ms, o.oid, o.side, o.px, o.sz) for o in orders],
            )
        self.conn.commit()
        return new != cur

    def fill_cursor(self) -> int:
        """Where our own fill ingestion left off. Persisted so a restart does not
        re-walk the account's entire fill history."""
        row = self.conn.execute(
            "SELECT message FROM events WHERE kind='fill_cursor' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return int(row[0]) if row else 0

    def set_fill_cursor(self, ts_ms: int) -> None:
        self.conn.execute("DELETE FROM events WHERE kind='fill_cursor'")
        self.record_event(ts_ms, "info", "fill_cursor", str(ts_ms))

    def last_report_day(self) -> str:
        row = self.conn.execute(
            "SELECT message FROM events WHERE kind='daily_report' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else ""

    def mirror_close(self, leader_oid: int, ts_ms: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE mirror_map SET closed_ts=?, close_reason=? "
            "WHERE leader_oid=? AND closed_ts IS NULL",
            (ts_ms, reason, leader_oid),
        )
        self.conn.commit()

    def update_equity(self, ts_ms: int, equity: float) -> float:
        """Append to the equity curve, return drawdown % vs the PERSISTED high-water
        mark. The HWM must survive restarts or the kill-switch could be reset by a
        reboot."""
        prev = self.conn.execute("SELECT MAX(hwm) FROM equity_curve").fetchone()[0]
        hwm = max(prev if prev is not None else equity, equity)
        dd = (equity / hwm - 1) * 100 if hwm else 0.0
        self.conn.execute(
            "INSERT OR REPLACE INTO equity_curve(ts,equity,hwm,drawdown_pct) VALUES (?,?,?,?)",
            (ts_ms, equity, hwm, dd),
        )
        self.conn.commit()
        return dd

    def latest_risk_state(self) -> str:
        """HALT is sticky across restarts. The documented un-HALT path is an operator
        inserting an events row with kind='manual_reset'."""
        row = self.conn.execute(
            "SELECT kind, message FROM events "
            "WHERE kind IN ('state_change','manual_reset') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "NORMAL"
        return "NORMAL" if row[0] == "manual_reset" else row[1]
