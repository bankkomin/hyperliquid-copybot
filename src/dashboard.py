"""Read-only tracking dashboard, modeled on paul.catseye.today.

Runs as a daemon thread inside the bot process and opens the DB read-only, so it
physically cannot place an order or corrupt bot state. The single exception is
the HALT button, which inserts one `halt_requested` event row.
"""

import json
import sqlite3
import time
import urllib.request

import dash
import plotly.graph_objects as go
from dash import Input, Output, State, dcc, html

from src.config import Config

GREEN, RED = "rgb(38, 166, 91)", "rgb(214, 69, 65)"
BG, PANEL, TEXT, MUTED = "#0d1117", "#161b22", "#e6edf3", "#8b949e"
STATE_COLORS = {"NORMAL": GREEN, "WARNING": "#d4a72c", "HALT": RED, "UNKNOWN": RED}

_candle_cache: dict = {"ts": 0.0, "data": []}
CANDLE_TTL_S = 60


def order_overlay_shapes(orders):
    """Pending-order overlay: one dashed line per live order at its limit price,
    labeled `who: size @ price`, buys green / sells red, 50% opacity until filled.

    `orders` is a list of (who, side, px, sz). This is the M1 acceptance
    criterion — verified side by side against paul.catseye.today.
    """
    shapes, annotations = [], []
    for who, side, px, sz in orders:
        color = GREEN if side == "B" else RED
        shapes.append(
            dict(
                type="line", xref="paper", x0=0, x1=1, y0=px, y1=px, opacity=0.5,
                line=dict(color=color, width=1, dash="dash"),
            )
        )
        annotations.append(
            dict(
                xref="paper", x=1, y=px, xanchor="left", showarrow=False,
                font=dict(size=10, color=color), text=f"{who}: {sz:g} @ {px:,.0f}",
            )
        )
    return shapes, annotations


def fetch_candles(interval: str = "1h", api_url: str = "https://api.hyperliquid.xyz"):
    """Hyperliquid's own candles (not Binance/Coinbase), cached 60s in memory."""
    if time.time() - _candle_cache["ts"] < CANDLE_TTL_S and _candle_cache["data"]:
        return _candle_cache["data"]
    end = int(time.time() * 1000)
    body = {
        "type": "candleSnapshot",
        "req": {"coin": "BTC", "interval": interval,
                "startTime": end - 45 * 24 * 3600 * 1000, "endTime": end},
    }
    try:
        req = urllib.request.Request(
            f"{api_url}/info",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
        _candle_cache.update(ts=time.time(), data=data)
        return data
    except Exception:
        return _candle_cache["data"]


class ReadOnlyDB:
    """Read-only view over the bot's SQLite file."""

    def __init__(self, path: str):
        self.path = path

    def q(self, sql: str, args=()) -> list[tuple] | None:
        """Rows, or None when the read FAILED.

        None and [] must stay distinguishable: a locked database returning []
        would paint a green NORMAL banner with 0.0% drawdown over an account
        that could be halted and deep in drawdown.
        """
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=5)
            try:
                return conn.execute(sql, args).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            return None

    def rows(self, sql: str, args=()) -> list[tuple]:
        """Rows for display panels, where empty and unreadable look the same."""
        return self.q(sql, args) or []

    def request_halt(self, ts_ms: int) -> None:
        """The dashboard's ONLY write. The bot consumes it on the next cycle."""
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute(
                "INSERT INTO events(ts,level,kind,message) VALUES (?,?,?,?)",
                (ts_ms, "critical", "halt_requested", "dashboard button"),
            )
            conn.commit()
        finally:
            conn.close()


def _card(title, rows):
    return html.Div(
        [
            html.Div(title, style={"color": MUTED, "fontSize": 12, "letterSpacing": 1}),
            html.Table(
                [
                    html.Tr(
                        [
                            html.Td(k, style={"color": MUTED, "paddingRight": 16}),
                            html.Td(v, style={"color": TEXT, "fontWeight": 600}),
                        ]
                    )
                    for k, v in rows
                ],
                style={"width": "100%", "marginTop": 8},
            ),
        ],
        style={"background": PANEL, "padding": 16, "borderRadius": 8, "flex": 1},
    )


def _table(headers, rows):
    return html.Table(
        [html.Tr([html.Th(h, style={"color": MUTED, "textAlign": "left"}) for h in headers])]
        + [
            html.Tr([html.Td(str(c), style={"color": TEXT, "paddingRight": 14}) for c in r])
            for r in rows
        ],
        style={"width": "100%", "fontSize": 13},
    )


def resolve_state(rows) -> str:
    """Same rule as store.latest_risk_state, applied to a read-only query result:
    a manual_reset row means an operator cleared the HALT.

    `None` means the query failed — report UNKNOWN, never a reassuring NORMAL.
    """
    if rows is None:
        return "UNKNOWN"
    if not rows:
        return "NORMAL"
    kind, message = rows[0][0], rows[0][1]
    return "NORMAL" if kind == "manual_reset" else message


# ---- "as of" reconstruction (replay) -------------------------------------
#
# Every panel can be rendered either live or as it stood at some past moment.
# `at_ts=None` means now; otherwise every query is bounded by that timestamp.


def snapshot_at(db: ReadOnlyDB, who: str, at_ts: int | None = None):
    sql = (
        "SELECT equity, position_btc, entry_px, upnl, leverage, mark_px FROM snapshots "
        "WHERE who=?"
    )
    args: tuple = (who,)
    if at_ts is not None:
        sql += " AND ts<=?"
        args += (at_ts,)
    rows = db.rows(sql + " ORDER BY ts DESC LIMIT 1", args)
    return rows[0] if rows else None


def ladder_at(db: ReadOnlyDB, at_ts: int | None = None):
    """(leader_orders, our_orders) as (side, px, sz), live or as of `at_ts`."""
    if at_ts is None:
        leader = db.rows("SELECT side, px, sz FROM leader_ladder_live ORDER BY px DESC")
        ours = db.rows("SELECT side, px, sz FROM orders WHERE status='open' ORDER BY px DESC")
        return leader, ours

    # History is written only when his ladder changes, so take the newest
    # snapshot at or before `at_ts`. px=0 is the empty-ladder marker.
    leader = db.rows(
        "SELECT side, px, sz FROM leader_open_orders WHERE snapshot_ts="
        "(SELECT MAX(snapshot_ts) FROM leader_open_orders WHERE snapshot_ts<=?) "
        "AND px > 0 ORDER BY px DESC",
        (at_ts,),
    )
    # `orders.status` only records the CURRENT state, so our historical book comes
    # from the mirror_map lifecycle (created_ts .. closed_ts) instead.
    ours = db.rows(
        "SELECT o.side, m.px, m.our_sz FROM mirror_map m JOIN orders o ON o.oid=m.our_oid "
        "WHERE m.created_ts<=? AND (m.closed_ts IS NULL OR m.closed_ts>?) "
        "ORDER BY m.px DESC",
        (at_ts, at_ts),
    )
    return leader, ours


def state_at(db: ReadOnlyDB, at_ts: int | None = None) -> str:
    sql = "SELECT kind, message FROM events WHERE kind IN ('state_change','manual_reset')"
    args: tuple = ()
    if at_ts is not None:
        sql += " AND ts<=?"
        args = (at_ts,)
    return resolve_state(db.q(sql + " ORDER BY id DESC LIMIT 1", args))


def replay_range(db: ReadOnlyDB) -> tuple[int, int]:
    """(first, last) recorded timestamps; (0, 0) when there is no history yet."""
    rows = db.rows("SELECT MIN(ts), MAX(ts) FROM snapshots")
    if not rows or rows[0][0] is None:
        return 0, 0
    return int(rows[0][0]), int(rows[0][1])


def slider_to_ts(pct: float, lo: int, hi: int) -> int | None:
    """Slider position -> timestamp. 100 (or no history) means LIVE."""
    if pct >= 100 or not hi or hi <= lo:
        return None
    return int(lo + (hi - lo) * pct / 100)


def build_view(db: ReadOnlyDB, cfg: Config, at_ts: int | None = None):
    """All panels for one refresh. Pure-ish: DB in, Dash children out.

    `at_ts=None` renders live; a timestamp renders the whole dashboard as it
    stood at that moment (replay).
    """
    leader = snapshot_at(db, "leader", at_ts)
    ours = snapshot_at(db, "copy", at_ts)
    state = state_at(db, at_ts)

    last_ts = db.rows("SELECT MAX(ts) FROM snapshots")
    age_s = (time.time() * 1000 - (last_ts[0][0] or 0)) / 1000 if last_ts and last_ts[0][0] else 0

    # Pending orders of BOTH accounts — the overlay source.
    leader_orders, our_orders = ladder_at(db, at_ts)
    overlay = [("leader", s, p, z) for s, p, z in leader_orders]
    overlay += [("ours", s, p, z) for s, p, z in our_orders]

    banner = html.Div(
        [
            html.Span("● ", style={"color": STATE_COLORS.get(state, MUTED), "fontSize": 20}),
            html.Span(state, style={"color": STATE_COLORS.get(state, MUTED), "fontWeight": 700}),
            html.Span(f"   mode: {cfg.mode.upper()}", style={"color": MUTED, "marginLeft": 24}),
            html.Span(
                f"   REPLAY {time.strftime('%Y-%m-%d %H:%M', time.gmtime(at_ts / 1000))}Z"
                if at_ts
                else f"   leader data: {age_s:.0f}s ago",
                style={"color": "#d4a72c" if at_ts else MUTED, "marginLeft": 24,
                       "fontWeight": 700 if at_ts else 400},
            ),
            html.Button(
                "HALT NOW", id="halt-btn", n_clicks=0,
                style={"float": "right", "background": RED, "color": "white",
                       "border": "none", "padding": "6px 16px", "borderRadius": 6,
                       "cursor": "pointer", "fontWeight": 700},
            ),
        ],
        style={"background": PANEL, "padding": 14, "borderRadius": 8, "marginBottom": 12},
    )

    candles = fetch_candles(api_url=cfg.api_url)
    if at_ts:  # don't show the future during replay
        candles = [c for c in candles if c["t"] <= at_ts]
    fig = go.Figure()
    if candles:
        fig.add_trace(
            go.Candlestick(
                x=[c["t"] for c in candles],
                open=[float(c["o"]) for c in candles],
                high=[float(c["h"]) for c in candles],
                low=[float(c["l"]) for c in candles],
                close=[float(c["c"]) for c in candles],
                increasing_line_color=GREEN, decreasing_line_color=RED, name="BTC",
            )
        )
    shapes, annotations = order_overlay_shapes(overlay)
    for f_who, marker in (("leader_fills", "triangle-up"), ("fills", "triangle-up-open")):
        fills = db.rows(
            f"SELECT ts, px, side FROM {f_who} WHERE ts<=? ORDER BY ts DESC LIMIT 200",
            (at_ts if at_ts else 1 << 62,),
        )
        if fills:
            fig.add_trace(
                go.Scatter(
                    x=[f[0] for f in fills], y=[f[1] for f in fills], mode="markers",
                    name=f_who,
                    marker=dict(
                        symbol=[marker if f[2] == "B" else marker.replace("up", "down")
                                for f in fills],
                        size=9,
                        color=[GREEN if f[2] == "B" else RED for f in fills],
                    ),
                )
            )
    fig.update_layout(
        shapes=shapes, annotations=annotations, template="plotly_dark",
        paper_bgcolor=PANEL, plot_bgcolor=PANEL, height=460,
        margin=dict(l=40, r=140, t=30, b=20), xaxis_rangeslider_visible=False,
        title="BTC price - candles (Hyperliquid) with pending-order overlay"
        + (" [REPLAY]" if at_ts else ""),
    )

    curve = db.rows(
        "SELECT ts, equity, drawdown_pct FROM equity_curve WHERE ts<=? ORDER BY ts",
        (at_ts if at_ts else 1 << 62,),
    )
    eq_fig = go.Figure()
    if curve:
        eq_fig.add_trace(
            go.Scatter(x=[c[0] for c in curve], y=[c[1] for c in curve],
                       name="copy equity", line=dict(color=GREEN))
        )
    eq_fig.update_layout(
        template="plotly_dark", paper_bgcolor=PANEL, plot_bgcolor=PANEL, height=240,
        margin=dict(l=40, r=20, t=30, b=20), title="Equity curve",
    )

    dd = curve[-1][2] if curve else 0.0
    fmt = lambda v, p="": f"{p}{v:,.2f}" if isinstance(v, float) else str(v)  # noqa: E731
    cards = html.Div(
        [
            _card(
                "COPY ACCOUNT",
                [("Equity", fmt(ours[0], "$") if ours else "-"),
                 ("Position", f"{ours[1]:.5f} BTC" if ours else "-"),
                 ("Entry", fmt(ours[2], "$") if ours and ours[2] else "-"),
                 ("uPnL", fmt(ours[3], "$") if ours else "-"),
                 ("Leverage", f"{ours[4]:.2f}x" if ours else "-"),
                 ("Drawdown", f"{dd:.1f}% (kill-switch {cfg.risk.max_drawdown_pct}%)")],
            ),
            _card(
                f"LEADER ({cfg.leader[:6]}...{cfg.leader[-4:]})",
                [("Equity", fmt(leader[0], "$") if leader else "-"),
                 ("Position", f"{leader[1]:.5f} BTC" if leader else "-"),
                 ("Entry", fmt(leader[2], "$") if leader and leader[2] else "-"),
                 ("uPnL", fmt(leader[3], "$") if leader else "-"),
                 ("Leverage", f"{leader[4]:.2f}x" if leader else "-"),
                 ("Scale", f"{ours[0] / leader[0]:.4f}" if ours and leader and leader[0] else "-")],
            ),
        ],
        style={"display": "flex", "gap": 12, "marginBottom": 12},
    )

    decisions = db.rows(
        "SELECT ts, trigger, action, veto_reason, risk_state FROM decisions "
        "WHERE ts<=? ORDER BY id DESC LIMIT 15",
        (at_ts if at_ts else 1 << 62,),
    )
    active = html.Div(
        [
            html.Div("ACTIVE ORDERS (ours + leader ladder)",
                     style={"color": MUTED, "fontSize": 12, "marginBottom": 8}),
            _table(
                ["Who", "Side", "Price", "BTC Size", "Notional"],
                [(w, "Buy" if s == "B" else "Sell", f"${p:,.0f}", f"{z:.5f}", f"${p * z:,.0f}")
                 for w, s, p, z in overlay],
            ),
        ],
        style={"background": PANEL, "padding": 16, "borderRadius": 8, "marginTop": 12},
    )
    recent = html.Div(
        [
            html.Div("RECENT DECISIONS", style={"color": MUTED, "fontSize": 12, "marginBottom": 8}),
            _table(
                ["Time", "Trigger", "Action", "Reason", "State"],
                [(time.strftime("%m-%d %H:%M", time.gmtime(d[0] / 1000)), d[1], d[2],
                  d[3] or "", d[4]) for d in decisions],
            ),
        ],
        style={"background": PANEL, "padding": 16, "borderRadius": 8, "marginTop": 12},
    )

    return [banner, dcc.Graph(figure=fig), dcc.Graph(figure=eq_fig), cards, active, recent]


def _replay_bar(cfg: Config):
    """Scrub back through recorded history. 100 = live.

    ponytail: the slider is a PERCENTAGE of the recorded range, not a timestamp,
    so its bounds never change as new data arrives and nothing fights the user
    mid-drag.
    """
    btn = {"background": PANEL, "color": TEXT, "border": f"1px solid {MUTED}",
           "padding": "4px 12px", "borderRadius": 6, "cursor": "pointer",
           "marginRight": 6}
    return html.Div(
        [
            html.Div(
                [
                    html.Span("REPLAY", style={"color": MUTED, "fontSize": 12,
                                               "letterSpacing": 1, "marginRight": 16}),
                    html.Button("< step", id="step-back", n_clicks=0, style=btn),
                    html.Button("play", id="play-btn", n_clicks=0, style=btn),
                    html.Button("step >", id="step-fwd", n_clicks=0, style=btn),
                    html.Button("latest", id="live-btn", n_clicks=0, style=btn),
                    html.Span(id="replay-label",
                              style={"color": MUTED, "marginLeft": 16, "fontSize": 13}),
                ],
                style={"marginBottom": 8},
            ),
            dcc.Slider(id="replay-slider", min=0, max=100, step=0.5, value=100,
                       marks=None, tooltip={"placement": "bottom"},
                       updatemode="mouseup"),
            dcc.Interval(id="play-tick", interval=1000, disabled=True),
            dcc.Store(id="playing", data=False),
        ],
        style={"background": PANEL, "padding": "14px 20px", "borderRadius": 8,
               "marginBottom": 12},
    )


def make_app(cfg: Config) -> dash.Dash:
    db = ReadOnlyDB(cfg.storage.db_path)
    app = dash.Dash(__name__, title="KK Copybot")
    app.layout = html.Div(
        [
            html.H2("Hyperliquid Copybot", style={"color": TEXT, "marginBottom": 4}),
            _replay_bar(cfg),
            html.Div(id="content"),
            dcc.Interval(id="tick", interval=cfg.dashboard.refresh_s * 1000),
            html.Div(id="halt-ack", style={"color": RED, "marginTop": 8}),
        ],
        style={"background": BG, "minHeight": "100vh", "padding": 20,
               "fontFamily": "system-ui, sans-serif"},
    )

    @app.callback(
        Output("content", "children"),
        Output("replay-label", "children"),
        Input("tick", "n_intervals"),
        Input("replay-slider", "value"),
    )
    def refresh(_n, pct):
        lo, hi = replay_range(db)
        at_ts = slider_to_ts(pct if pct is not None else 100, lo, hi)
        label = (
            "live"
            if at_ts is None
            else time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(at_ts / 1000))
        )
        return build_view(db, cfg, at_ts), label

    @app.callback(
        Output("playing", "data"),
        Output("play-btn", "children"),
        Output("play-tick", "disabled"),
        Input("play-btn", "n_clicks"),
        Input("live-btn", "n_clicks"),
        State("playing", "data"),
        prevent_initial_call=True,
    )
    def toggle_play(_p, _l, playing):
        if dash.ctx.triggered_id == "live-btn":
            return False, "play", True
        playing = not playing
        return playing, ("pause" if playing else "play"), not playing

    @app.callback(
        Output("replay-slider", "value"),
        Input("play-tick", "n_intervals"),
        Input("step-back", "n_clicks"),
        Input("step-fwd", "n_clicks"),
        Input("live-btn", "n_clicks"),
        State("replay-slider", "value"),
        prevent_initial_call=True,
    )
    def move(_tick, _b, _f, _l, pct):
        pct = 100 if pct is None else pct
        who = dash.ctx.triggered_id
        if who == "live-btn":
            return 100
        if who == "step-back":
            return max(0, pct - 0.5)
        if who == "step-fwd":
            return min(100, pct + 0.5)
        return min(100, pct + 0.5)  # play tick

    @app.callback(
        Output("halt-ack", "children"),
        Input("halt-btn", "n_clicks"),
        prevent_initial_call=True,
    )
    def halt(_n_clicks):
        db.request_halt(int(time.time() * 1000))
        return "HALT requested - the bot will cancel and flatten on its next cycle."

    return app
