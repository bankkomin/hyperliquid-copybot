# PRD — Hyperliquid Copybot for `0xdae4...7637` ("Paul Wei")

**Status:** Draft v3 · 2026-08-27
**Repo:** `bankkomin/hyperliquid-copybot`
**Leader:** [`0xdae4df7207feb3b350e4284c8efe5f7dac37f637`](https://hyperbot.network/trader/0xdae4df7207feb3b350e4284c8efe5f7dac37f637) — BTC perp only, tracked at [paul.catseye.today](https://paul.catseye.today/)

> **v3 changes:** switched to a FULL MIRROR — the leader's resting order ladder, sizing proportions, margin config, and risk profile are all copied (§4–§6). Our own risk layer reduced to account-level backstops (mirror parity, −35% kill-switch, watchdog); leverage clamp, add-throttle, and stop overlay removed from defaults.
> **v2 changes:** added data storage schema (§10), dashboard + daily report spec with examples (§11), standalone double-click operation (§12).

---

## 1. Goal

**Fully replicate the leader's book on our own Hyperliquid account, scaled to our equity** — his position, his resting order ladder, his sizing proportions, and his risk profile. The leader's risk management IS his ladder structure (pyramid sizing deeper into dips, maker-only entries, partial distribution into rallies, no stops) — so the bot copies that structure faithfully instead of replacing it. Our own risk layer is reduced to **account-level backstops** (drawdown kill-switch, connection watchdog) that only fire when the mirror itself is in danger, not to second-guess his trades.

**Standalone requirement:** the whole product is one folder on the VPS. Double-click `start_copybot.bat` → Python runs in the background, the tracking dashboard opens in the browser. No terminal babysitting.

**Non-goals (v1):** multi-leader support, non-BTC assets, spot copying, vault deposits, cloud hosting.

---

## 2. What we learned about the leader (drives the whole design)

From 9 months of on-chain data (1,343 orders, 341 fills, Nov 2025 → Aug 2026):

| Fact | Number | Design consequence |
|---|---|---|
| Style | Long-only laddered accumulation ("grid"-looking) | Copy **position state**, not every fill |
| Maker fills | 84% | If we taker-copy, we pay costs he doesn't → maker-first execution |
| Realized grid PnL | ≈ +$500 net of funding+fees (noise) | The copyable alpha is the **directional position**, not the churn |
| Unrealized PnL | +$20.6k on 1.336 BTC @ $64,249 avg entry | Late copier buys HIS entry at TODAY's price — no entry-price edge |
| Stop losses | **ZERO** (no trigger orders, no TP/SL, ever) | **We must add our own stop overlay** |
| Behavior in drawdown | Averages down, pyramids size (0.05 → 0.20 BTC deeper) | Resting ladder would grow him ~40% bigger into a 28% crash — cap this |
| Fill frequency | ~1.2 fills/day, order batches every 1–2 days | Low-frequency; polling + WS both viable; latency is NOT critical |
| Funding paid | −$1,765 over 9 months (long perp) | Funding monitor needed; long-bias bleeds carry |

> **Key insight:** this leader survived because BTC V-shaped. A copybot that mirrors him 1:1 inherits an **uncapped average-down martingale with no stop**. The risk layer is not optional polish — it is the product.

---

## 3. System overview

```mermaid
flowchart LR
    subgraph HL[Hyperliquid]
        WS[WebSocket<br/>userFills / orderUpdates<br/>of LEADER]
        API[REST info API<br/>clearinghouseState<br/>openOrders]
        EX[Exchange endpoint<br/>place / cancel orders]
    end

    subgraph BOT["Copybot — ONE process (start_copybot.bat)"]
        W[1. Watcher<br/>leader state tracker]
        S[2. Sizer<br/>scale to our equity]
        R[3. Risk Gate<br/>HARD limits, kill switch]
        E[4. Executor<br/>maker-first orders]
        DB[(SQLite copybot.db<br/>WAL mode)]
        D[5. Dash dashboard<br/>localhost:8061<br/>daemon thread]
        N[Telegram<br/>alerts + daily report]
    end

    B[Browser<br/>auto-opened by .bat] --> D

    WS --> W
    API --> W
    W -->|target position Δ| S
    S -->|proposed order| R
    R -->|approved order| E
    R -->|VETO / HALT| N
    E --> EX
    E --> DB
    W --> DB
    DB --> D
```

Every order flows **Watcher → Sizer → Risk Gate → Executor**. Nothing reaches the exchange without passing the Risk Gate. The gate can only shrink or veto an order, never enlarge it. The dashboard is read-only over SQLite — it can never place an order.

---

## 4. Copy strategy: full order-mirror + position reconciliation

Three approaches exist across open-source copybots — we use two of them, layered:

| Approach | Used by | Role here |
|---|---|---|
| **Order-mirror** — replicate the leader's *resting* orders (place/cancel/replace), scaled | (none of the referenced bots do this fully) | ✓ **PRIMARY.** The ladder is his strategy AND his risk management — copying it gives us his maker fills, his pyramid sizing, and his entry prices, not laggy taker copies |
| **Position-sync** — converge our position toward `leader_position × scale` | [MaxIsOntoSomething](https://github.com/MaxIsOntoSomething/Hyperliquid_Copy_Trader), [jestersimpps](https://github.com/jestersimpps/hyperliquid-copytrader) (drift sync) | ✓ **RECONCILIATION.** Runs every 60s; catches anything the order-mirror missed (downtime, partial fills, his taker trades) and converges position drift > 1% |
| **Fill-mirror** — react to each leader fill with a taker copy | [zkOSAI](https://github.com/zkOSAI/hyperliquid-copy-trading-bot) | ✗ Not used standalone — his fills hit OUR mirrored orders at the same prices; taker copy only appears inside reconciliation |

### 4.1 Order-mirror mechanics

The Watcher subscribes to the leader's `orderUpdates` WS stream and polls `frontendOpenOrders` (60s fallback). The target state is always: *our open orders = his open orders × scale, same prices, same side*.

```
his ladder changes (place / cancel / modify)
        → diff his ladder vs our mirrored ladder (via mirror_map)
        → place / cancel ours to match, each order sized sz_his × scale
        → risk backstops (§6) check the LADDER TOTAL, not each order's direction
```

- **Same prices as his** → when BTC dips to his rung, both accounts fill together as makers. No lag, no taker fee, entry parity.
- His fills reduce his order; ours reduce ours proportionally — the books stay parallel without any fill-chasing.
- His taker trades (16% of his activity) have no resting order to mirror → picked up by the 60s reconciliation as a scaled taker copy (slippage cap applies).
- Ladder diffs are idempotent: crash-restart → re-diff from scratch against `mirror_map`, no duplicates.

```mermaid
sequenceDiagram
    participant L as Leader account
    participant W as Watcher
    participant S as Sizer
    participant R as Risk Gate
    participant E as Executor
    participant H as Hyperliquid

    L->>W: WS fill event (or 60s poll tick)
    W->>H: GET clearinghouseState (leader)
    W->>H: GET clearinghouseState (ours)
    W->>S: leader_pos = 1.336 BTC, leader_equity = $66k
    S->>S: target = leader_pos × (our_equity / leader_equity)
    S->>S: delta = target − our_pos
    alt |delta| < drift threshold (1% of target)
        S-->>W: no-op (skip dust)
    else
        S->>R: proposed order (side, size, ref price)
        R->>R: run ALL risk checks (Section 6)
        alt any check fails
            R-->>W: VETO → log + Telegram alert
        else
            R->>E: approved (possibly size-reduced)
            E->>H: place limit @ best passive price
            E->>E: not filled in 90s? cross the spread (taker cap applies)
        end
    end
```

**What this means concretely today:** his 14 resting buys ($57.9k–$73.5k, 0.05→0.20 BTC pyramiding deeper) get mirrored as 14 resting buys on our account at the same prices, each sized × scale. If BTC crashes into the ladder, we average down exactly as he does, proportionally — that *is* the strategy being copied, accepted deliberately (see §6 backstops and §14 economics for what still protects the account).

---

## 5. Position sizing (priority #1)

### 5.1 Core formula — everything scales by one ratio

One number drives the whole mirror, applied to every order AND the position:

```
scale            = our_equity / leader_equity      # both = perp accountValue, refreshed every sync
mirror_order_sz  = leader_order_sz × scale         # per resting order, same limit price as his
position_target  = leader_position × scale         # reconciliation target (60s loop)
round: always DOWN to sz_decimals (BTC: 5)
```

Because *every* rung uses the same ratio, his sizing structure is preserved automatically — the 0.05 → 0.12 → 0.20 BTC pyramid, the % of account each rung represents, and his leverage profile all carry over exactly. We also mirror his **margin config: cross margin, 3× leverage setting** (read from his `clearinghouseState`).

Worked example at current state (our account = $10,000, scale = 10,000 / 66,435 = **0.1505**):

| | Leader | Ours (× 0.1505) | % of own equity |
|---|---|---|---|
| Position | 1.33557 BTC ($107.5k, 1.62×) | 0.20103 BTC ($16.2k, 1.62×) | same |
| Rung @ $73,521 | 0.05 BTC ($3.7k) | 0.00752 BTC ($553) | 5.5% both |
| Rung @ $62,944 | 0.12 BTC ($7.6k) | 0.01806 BTC ($1,137) | 11.4% both |
| Rung @ $57,860 | 0.20 BTC ($11.6k) | 0.03010 BTC ($1,742) | 17.4% both |
| **If full ladder fills** | ~1.85 BTC, ~2.9× on shrunken equity | ~0.278 BTC, ~2.9× | **identical risk profile** |

```mermaid
flowchart TD
    A["Leader ladder event<br/>(place / cancel / fill)"] --> B["Diff his open orders<br/>vs mirror_map"]
    B --> C["Each new/changed order:<br/>sz × 0.1505, SAME price"]
    C --> D{"sz ≥ $10 HL min<br/>after rounding?"}
    D -- no --> E["Skip rung, log it<br/>(account too small for this rung)"]
    D -- yes --> F["Backstop check (§6):<br/>ladder TOTAL ≤ mirror of his total"]
    F --> G["Place / cancel to match"]
    H["60s reconciliation:<br/>position drift > 1%?"] --> I["Taker top-up<br/>(slippage cap 0.15%)"]
```

### 5.2 Sizing rules

| # | Rule | Value (default) | Why |
|---|---|---|---|
| S1 | Single scale ratio everywhere | `our_equity / leader_equity`, live both sides | Preserves his ladder shape, rung %, and leverage profile exactly |
| S2 | Mirror his margin config | cross, 3× (as read from his account) | His liquidation math is part of his risk management — copy it |
| S3 | Min notional | $10 (Hyperliquid rule) | A rung that rounds below $10 is skipped and **logged** — at $10k equity every current rung clears it; below ~$3k equity the small rungs start dropping and the mirror degrades (§15 Q1) |
| S4 | Drift threshold (reconciliation) | 1% of target position | Don't taker-chase every $ of equity fluctuation |
| S5 | Rounding | Always round **down** | Never exceed his scaled size |
| S6 | Scale refresh | Every sync; re-size ladder only when scale moves > 5% | Avoid churning 14 cancel/replaces because equity wiggled 0.3% |

There is deliberately **no leverage clamp and no add-throttle** in mirror mode — his ladder-driven leverage path (currently up to ~2.9× if the full ladder fills) is the strategy we chose to copy. The account-level backstops in §6 are the only overrides.

---

## 6. Risk backstops (mirror-faithful)

**Philosophy change (v3):** we copy the leader's risk management — pyramid ladder, maker entries, partial distribution, no stops. So the bot has no per-trade risk opinions: no leverage clamp, no add-throttle, no stop-loss overlay by default. What remains are **account-level backstops** that fire only when the *mirror itself* is broken or the account is threatened with destruction — the two things the leader's risk management cannot see from his side.

### 6.1 Hard gates — every order, any failure = veto

```mermaid
flowchart TD
    O[Proposed order] --> B1{"B1 Asset allowlist<br/>BTC only"}
    B1 -- fail --> V[VETO + alert]
    B1 --> B2{"B2 Mirror parity<br/>our total resting + position<br/>≤ leader total × scale × 1.05"}
    B2 -- fail --> V
    B2 --> B3{"B3 Price integrity<br/>mirror order = HIS price;<br/>reconciliation taker within 1% of mark"}
    B3 -- fail --> V
    B3 --> B4{"B4 State fresh<br/>leader data < 5 min old"}
    B4 -- fail --> V
    B4 --> B5{"B5 System state<br/>= NORMAL or WARNING"}
    B5 -- fail --> V
    B5 --> OK[Approved → Executor]
```

B2 is the one structural guarantee: **we can never be MORE exposed than the leader, proportionally.** Any bug that would over-mirror gets vetoed there.

### 6.2 Standing monitors (run continuously, independent of orders)

| # | Monitor | Trigger | Action |
|---|---|---|---|
| M1 | **Drawdown kill-switch** — the ONE deliberate deviation from the leader | Account equity < **−35%** from high-water mark (wide enough to ride his ladder drawdowns; configurable) | → HALT: cancel all orders, flatten, stop copying, manual restart required |
| M2 | Connection watchdog | WS silent > 5 min AND poll fails | → WARNING + alert. **Resting mirror orders are KEPT** — they match his standing intent (his are GTC for days); reconcile ladder + position on reconnect |
| M3 | Leader anomaly | Leader position changes > 50% in < 10 min, or leverage config change | Alert + WARNING (mirror continues — even his short flips are copied now; the alert is for the human, not a brake) |
| M4 | Funding bleed | Cumulative funding paid > 2% of equity per 30d | Alert only (he paid ~0.6%/mo peak) |
| M5 | Divergence audit | Our ladder or position differs > 5% from `leader × scale` | Force full re-mirror; if it persists 3 cycles → WARNING + alert |
| M6 | *(optional, OFF by default)* Stop-loss overlay | uPnL < configurable threshold | Available in config for anyone who wants training wheels; default off because the leader trades without stops and that is what we are copying |

### 6.3 Risk state machine

```mermaid
stateDiagram-v2
    [*] --> NORMAL
    NORMAL --> WARNING: WS lost / anomaly / drift persists
    WARNING --> NORMAL: condition clears
    NORMAL --> HALT: drawdown −35% HWM / HALT button
    WARNING --> HALT: drawdown −35% HWM
    HALT --> [*]: manual restart only
```

- **NORMAL** — full mirror: ladder + position + reconciliation.
- **WARNING** — no NEW mirror orders; existing ladder kept; closes/cancels still mirrored (never block risk reduction).
- **HALT** — flat, no orders, manual intervention required. The bot must never auto-exit HALT.

---

## 7. Execution

Leader earns his economics by being 84% maker — and the order-mirror gives us that for free:

1. **Mirror orders**: GTC limit at **his exact price** (ALO/post-only where possible). Maker by construction; they fill when his fill.
2. **Reconciliation top-ups** (his taker trades, missed fills, drift): limit at the passive touch, 90 s, then cross as IOC with slippage cap **0.15%** from mark.
3. Fills/partials recorded in SQLite; remainders handled by the next reconciliation cycle (retries are free).

HALT flattening (M1 kill-switch or dashboard button) skips the patience: 30 s maker attempt, then taker. Getting out matters more than fee savings.

---

## 8. Configuration (single `config.yaml`)

```yaml
leader: "0xdae4df7207feb3b350e4284c8efe5f7dac37f637"
assets: ["BTC"]

mirror:
  scale: equity_ratio         # our_equity / leader_equity, refreshed each sync
  copy_orders: true           # mirror his resting ladder (place/cancel/replace)
  copy_margin_config: true    # adopt his cross-margin 3× setting
  copy_shorts: true           # mirror everything, including his rare short flips
  scale_rebalance_pct: 5      # re-size ladder only when scale moves > 5%
  drift_threshold_pct: 1.0    # reconciliation trigger

risk:
  mirror_parity_tolerance: 1.05  # B2: never > leader × scale × this
  max_drawdown_pct: -35          # M1 kill-switch (the one deviation from him)
  stop_loss_overlay: null        # M6: off by default = faithful mirror; set e.g. -20 to enable
  leader_staleness_max_s: 300
  funding_alert_pct_30d: 2.0

execution:
  maker_wait_s: 90
  maker_wait_close_s: 30
  taker_slippage_cap_pct: 0.15

storage:
  db_path: "data/copybot.db"
  snapshot_interval_s: 60

dashboard:
  port: 8061
  refresh_s: 15
  daily_report_utc: "00:05"   # also posted to Telegram

mode: paper                   # paper | live  — paper is the default, always
telegram: { enabled: true }
```

---

## 9. Tech stack & repo layout

Match the option bot's conventions — same developer, same review muscle memory:

- **Python 3.10+**, asyncio, official [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
- **SQLite** (WAL mode) for all state + append-only audit log
- **Plotly Dash** for the tracking dashboard (same stack as the option bot's dashboard — reuse the dark theme CSS)
- **structlog** JSON logging to `logs/`, **Pydantic** models, **pytest**
- Own venv inside the repo (`venv\`) — the bare-`python` hijack gotcha applies here too; the `.bat` always calls `venv\Scripts\pythonw.exe` by explicit path

```
hyperliquid-copybot/
├── start_copybot.bat       # double-click: bot + dashboard in background, opens browser
├── stop_copybot.bat        # double-click: clean shutdown
├── config.yaml
├── requirements.txt
├── venv/                   # local venv (not committed)
├── data/
│   └── copybot.db          # SQLite — the single source of truth
├── logs/
│   ├── copybot.jsonl       # structlog output (rotated daily)
│   └── copybot.pid         # written on start, used by stop_copybot.bat
├── reports/
│   └── daily-YYYYMMDD.html # self-contained daily report snapshots
├── src/
│   ├── main.py             # async loop wiring + Dash daemon thread + pid file
│   ├── watcher.py          # WS subscribe + poll, leader/our state snapshots
│   ├── sizer.py            # §5 — pure function, fully unit-tested
│   ├── risk.py             # §6 — gates + monitors + state machine, pure where possible
│   ├── executor.py         # §7 — order placement, maker-first
│   ├── store.py            # §10 — SQLite schema, writes, snapshot reads
│   ├── dashboard.py        # §11 — Dash app, reads SQLite only
│   └── report.py           # §11.3 — daily HTML + Telegram summary
└── tests/
```

Sizer and risk gate are **pure functions** (state in → decision out) so every rule in Sections 5–6 gets a table-driven unit test.

---

## 10. Data storage (SQLite `data/copybot.db`)

One SQLite file is the single source of truth — bot writes, dashboard/report read. WAL mode so the dashboard never blocks the bot (same pattern as the option bot's collector/dashboard split). At 1-minute snapshots this grows ~25 MB/year — no retention policy needed, keep everything forever for backtesting the copy quality.

### 10.1 Schema

```sql
-- Leader + our account state, every sync (~60s) and on every fill trigger
CREATE TABLE snapshots (
  ts          INTEGER NOT NULL,        -- epoch ms
  who         TEXT    NOT NULL,        -- 'leader' | 'copy'
  equity      REAL, position_btc REAL, entry_px REAL,
  upnl        REAL, leverage REAL, mark_px REAL,
  PRIMARY KEY (ts, who)
);

-- EVERY sizer/risk decision, including no-ops and vetoes (full audit trail)
CREATE TABLE decisions (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,
  trigger      TEXT,                   -- 'ws_fill' | 'poll' | 'monitor_M1' ...
  leader_pos   REAL, scale REAL, raw_target REAL,
  target       REAL, delta REAL,
  action       TEXT NOT NULL,          -- 'order' | 'skip_dust' | 'veto' | 'halt'
  veto_reason  TEXT,                   -- 'R2_leverage' | 'R4_add_throttle' ...
  risk_state   TEXT NOT NULL           -- NORMAL | WARNING | REDUCING | HALT
);

-- Our orders and their lifecycle
CREATE TABLE orders (
  oid INTEGER PRIMARY KEY, decision_id INTEGER REFERENCES decisions(id),
  ts INTEGER, side TEXT, px REAL, sz REAL,
  exec_style TEXT,                     -- 'maker' | 'taker_fallback' | 'risk_close'
  status TEXT,                         -- open | filled | canceled | rejected
  filled_sz REAL DEFAULT 0, avg_px REAL, fees REAL
);

-- Our fills (from our own userFills stream)
CREATE TABLE fills (
  tid INTEGER PRIMARY KEY, oid INTEGER, ts INTEGER,
  side TEXT, px REAL, sz REAL, crossed INTEGER,  -- crossed=1 → taker
  closed_pnl REAL, fee REAL
);

-- Hourly rollup for the equity/PnL charts + HWM for the kill-switch
CREATE TABLE equity_curve (
  ts INTEGER PRIMARY KEY, equity REAL, hwm REAL,
  drawdown_pct REAL, funding_cum REAL, fees_cum REAL, realized_cum REAL
);

-- Leader's fills and resting orders (for the catseye-style chart overlays, §11.1)
CREATE TABLE leader_fills (
  tid INTEGER PRIMARY KEY, ts INTEGER, side TEXT, px REAL, sz REAL,
  crossed INTEGER, dir TEXT                    -- 'Open Long', 'Close Long', ...
);
CREATE TABLE leader_open_orders (
  snapshot_ts INTEGER, oid INTEGER, side TEXT, px REAL, sz REAL,
  PRIMARY KEY (snapshot_ts, oid)               -- full ladder re-recorded when it changes
);

-- The mirror mapping: which of OUR orders mirrors which of HIS (§4.1 diffing)
CREATE TABLE mirror_map (
  leader_oid INTEGER PRIMARY KEY, our_oid INTEGER,
  px REAL, leader_sz REAL, our_sz REAL, scale_used REAL,
  created_ts INTEGER, closed_ts INTEGER, close_reason TEXT  -- 'his_cancel'|'his_fill'|'rebalance'|'halt'
);

-- State transitions and alerts (drives the dashboard banner + Telegram)
CREATE TABLE events (
  id INTEGER PRIMARY KEY, ts INTEGER,
  level TEXT,                          -- info | warning | critical
  kind  TEXT,                          -- 'state_change' | 'veto' | 'ws_lost' | 'stop_fired' ...
  message TEXT
);
```

### 10.2 Storage rules

- **Append-only** for `decisions`, `fills`, `events` — never UPDATE history; the audit trail is the point.
- `decisions` logs **every** cycle including `skip_dust` no-ops → the dashboard can prove the bot is alive and *choosing* not to trade, which is different from being dead.
- HWM (high-water mark) lives in `equity_curve` and survives restarts — the −20% kill-switch must not reset by rebooting the bot.
- Dashboard/report processes open the DB **read-only** (`mode=ro` URI) — physically cannot corrupt bot state.

---

## 11. Dashboard & reports

### 11.1 Dashboard (Dash on `http://localhost:8061`)

Runs as a daemon thread inside the bot process (one process = one pid = simple `.bat` lifecycle). Reads SQLite only, refreshes every 15 s via `dcc.Interval`. Follows the option bot's snapshot pattern: one cached read per refresh, zero queries in callbacks.

**Look & feel: modeled on [paul.catseye.today](https://paul.catseye.today/)** — dark theme, price chart first with fills and order ladders drawn *on* the candles, time-series for leverage and cumulative PnL below, then the copybot's own risk/decision panels. Full layout, top to bottom:

```
┌──────────────────────────────────────────────────────────────────────┐
│  ● NORMAL   mode: PAPER   ws: connected 12s ago   leader data: 43s   │  ← state banner
│             (green NORMAL / yellow WARNING / orange REDUCING / red HALT)
├──────────────────────────────────────────────────────────────────────┤
│  BTC PRICE · CANDLES (catseye-style)         [1d] [4h] [1h] [15m]    │
│                                                                      │
│      Hyperliquid candles                                  [chart]    │
│      ─ ─ ─  leader resting buy ladder (green dashed lines)           │
│      ─ ─ ─  our resting orders (bright green/red lines)              │
│      ▲▼ leader fills (solid marker)  △▽ our fills (hollow marker)    │
│      ───  kill-switch equity level; stop overlay if enabled (red)    │
│      Order lines: [Off] [Top 4] [Top 8] [All]   Fills: [All][Taker]  │
├──────────────────────────────────────────────────────────────────────┤
│  ACTUAL LEVERAGE % (ours vs leader, signed)               [chart]    │
│  CUMULATIVE PnL  [USD | BTC]  (ours vs leader × scale)    [chart]    │
├───────────────────────────┬──────────────────────────────────────────┤
│  COPY ACCOUNT             │  LEADER (0xdae4...7637)                  │
│  Equity      $10,241      │  Equity        $66,435                   │
│  Position    0.18634 BTC  │  Position      1.33557 BTC               │
│  Entry       $79,880      │  Entry         $64,249                   │
│  uPnL        +$112 (+1.1%)│  uPnL          +$20,584                  │
│  Leverage    1.46× ▓▓▓▓░  │  Leverage      1.62×                     │
│  Drift vs target: 0.3% ✓  │  Scale: 0.1505                           │
├───────────────────────────┴──────────────────────────────────────────┤
│  EQUITY CURVE (ours vs leader, indexed to 100)         [chart]       │
│  DRAWDOWN from HWM  −2.1%   (kill-switch at −20%)      [chart]       │
├──────────────────────────────────────────────────────────────────────┤
│  MIRROR HEALTH & BACKSTOPS (last 24h)                                │
│  Ladder mirrored: 14/14 rungs ✓   parity: 99.7% of leader × scale    │
│  M1 kill-switch: −2.1% of −35% HWM    M4 funding 30d: 0.4% ✓         │
├──────────────────────────────────────────────────────────────────────┤
│  RECENT DECISIONS                                                    │
│  12:31:05  poll     Δ+0.005  order   maker filled @ 80,412           │
│  12:19:44  ws_fill  Δ+0.031  VETO    R4_add_throttle                 │
│  11:58:02  poll     Δ 0.000  skip_dust                               │
├──────────────────────────────────────────────────────────────────────┤
│  ACTIVE ORDERS (ours + leader ladder, catseye-style table)           │
│  Who     Side  Price     BTC Size  Notional  % of Acct  Created      │
│  leader  Buy   $57,860   0.20000   $11,572    17.4%     08-21 13:30  │
│  ours    Buy   $59,715   0.01806   $1,078     10.5%     08-27 09:12  │
├──────────────────────────────────────────────────────────────────────┤
│  OUR FILLS (maker % · fees · realized)     LEADER FEED (his fills)   │
└──────────────────────────────────────────────────────────────────────┘
```

Chart section — what we copy from catseye and what we defer:

| Catseye feature | v1 | How |
|---|---|---|
| Candles 1d/4h/1h/15m (Hyperliquid's own data) | ✓ | `candleSnapshot` info API, fetched on refresh, cached in memory (not stored in DB) |
| Order lines on chart (Off/Top 4/8/All) | ✓ | `leader_open_orders` + our `orders` → horizontal dashed lines |
| Fill markers, buy/sell colored, taker filter | ✓ | `leader_fills` + our `fills` → triangle markers; ours hollow so the two accounts are distinguishable at a glance |
| Stop-overlay levels drawn on the chart | ✓ (ours only — catseye has none because the leader has none) | computed from position entry + §6.2 M1 levels |
| Leverage % time series (signed) | ✓ | `snapshots` both accounts, two lines |
| Cumulative PnL with USD/BTC toggle | ✓ | `equity_curve` (ours) vs leader `snapshots` × scale |
| Playback / replay of history | ✗ defer to M4 | nice-to-have; SQLite history makes it possible later |
| EMA/SMA/Bollinger indicators | ✗ defer to M4 | not needed to supervise a copybot |

All charts are Plotly (one stack, same as the option bot — no need for catseye's lightweight-charts dependency; Plotly candlestick + shapes covers everything above).

Panel spec:

| Panel | Source table | Purpose |
|---|---|---|
| State banner | `events`, latest `snapshots` | One glance: alive? which risk state? data fresh? |
| Price + candles with overlays | `candleSnapshot` API, `leader_fills`, `leader_open_orders`, `orders`, `fills` | The catseye view, both accounts on one chart: what he did, what we copied, where our stops sit |
| Leverage % chart | `snapshots` | Are we tracking his exposure within our 1.5× clamp? |
| Cumulative PnL chart (USD/BTC) | `equity_curve`, leader `snapshots` | Copy P&L vs `leader × scale` benchmark over time |
| Copy vs Leader cards | `snapshots` | The core question: are we actually mirroring him? |
| Equity curves (indexed) | `equity_curve`, leader `snapshots` | Is copy P&L tracking leader P&L after costs? |
| Drawdown gauge | `equity_curve` | Distance to the −20% kill-switch, visually |
| Risk gate activity | `decisions` (vetoes), monitor state | Proof the gates work; throttle budget remaining |
| Decisions table | `decisions` | The audit trail, live — includes no-ops |
| Active orders table | `leader_open_orders`, `orders` | Catseye-style: side, price, size, notional, % of acct, created — both accounts interleaved |
| Fills tables | `fills`, `leader_fills` | Maker ratio (target ≥ 60%), fees paid, slippage vs leader's price |

### 11.2 Kill switch on the dashboard

One red **"HALT NOW"** button — the only write the dashboard is allowed, implemented as writing a `halt_requested` event row that the bot's main loop picks up within one cycle. No order-placement UI, ever.

### 11.3 Daily report (auto, 00:05 UTC)

`report.py` renders a self-contained HTML file to `reports/daily-YYYYMMDD.html` (charts inlined — openable years later without the bot running) and posts a text summary to Telegram:

```
📊 Copybot daily — 2026-08-27 (PAPER)
State: NORMAL all day
Equity: $10,241 (+0.4% day, +2.4% since start, HWM −2.1%)
Position: 0.18634 BTC @ $79,880 avg | leader 1.33557 BTC (drift 0.3%)
Today: 3 syncs → 2 orders (both maker), 1 veto (R4 add-throttle)
Costs today: fees $1.10 · funding −$3.20 · slippage vs leader +$0.4
Copy quality 7d: maker 71% · avg fill lag 38s · tracking error 0.8%
```

The **copy-quality metrics** (maker %, fill lag vs leader's fill time, tracking error vs `leader_pnl × scale`) are the M1→M2 go/no-go numbers from §14.

---

## 12. Standalone operation (double-click run)

### 12.1 `start_copybot.bat`

```bat
@echo off
cd /d %~dp0
if exist logs\copybot.pid (
  echo Copybot already running (logs\copybot.pid exists). Use stop_copybot.bat first.
  pause & exit /b 1
)
start "" venv\Scripts\pythonw.exe -m src.main
timeout /t 4 >nul
start http://localhost:8061
```

- `pythonw.exe` → **no console window**; the process lives in the background, all output goes to `logs/copybot.jsonl`.
- `main.py` writes `logs/copybot.pid` on startup and removes it on clean exit; the stale-pid check stops double-starts (two bots = double orders).
- After 4 s the default browser opens straight to the dashboard.

### 12.2 `stop_copybot.bat`

```bat
@echo off
cd /d %~dp0
if not exist logs\copybot.pid ( echo Not running. & pause & exit /b 0 )
set /p PID=<logs\copybot.pid
taskkill /pid %PID% >nul 2>&1       & rem polite: triggers graceful-shutdown handler
timeout /t 10 >nul
taskkill /f /pid %PID% >nul 2>&1    & rem force if still alive
del logs\copybot.pid
echo Copybot stopped.
pause
```

Graceful shutdown = cancel all our resting orders → flush SQLite → exit. **The position is kept** (stopping the bot must not flatten a healthy position); a Telegram message confirms "stopped, position 0.186 BTC unmanaged".

### 12.3 Crash & reboot behavior

```mermaid
flowchart LR
    A[Windows reboot /<br/>python crash] --> B[Orders: GTC limits<br/>still resting on exchange!]
    B --> C[start_copybot.bat<br/>manual or Task Scheduler at logon]
    C --> D[main.py startup:<br/>1. cancel ALL our open orders<br/>2. read position + HWM from DB<br/>3. re-mirror his full ladder from scratch<br/>4. position reconciliation cycle<br/>5. resume NORMAL]
```

- Startup **always begins by canceling every open order on our account** — never trust orders left by a dead process.
- HWM and risk state are persisted (§10), so a reboot cannot reset the drawdown kill-switch.
- Optional: a Task Scheduler entry (`At log on`, run `start_copybot.bat`) makes the VPS fully hands-off. Not created automatically — documented in README for the user to enable.
- Position-sync architecture makes crash recovery free: whatever happened while down, the first cycle converges to `leader × scale` through the normal risk gates.

---

## 13. Milestones

| Phase | Deliverable | Exit criteria |
|---|---|---|
| **M1 Shadow** | Watcher + Sizer + Risk Gate in `paper` mode **+ SQLite store + dashboard + daily report + .bat runbook** | 2 weeks running standalone on the VPS; decisions match leader moves; zero crashes; risk vetoes reviewed via dashboard |
| **M2 Live-small** | Executor live, account funded with **test-size capital only** | Full ladder mirrored (parity ≥ 99%); 2+ rungs filled alongside his as maker; drift < 1%; kill-switch fire-drilled on testnet |
| **M3 Hardened** | Watchdogs, HALT drill, restart-recovery test (kill -9 mid-sync, reboot test) | Restart converges to correct position with no duplicate orders; stale-pid + startup-cancel verified |
| **M4 Optional** | Order-ladder mirroring flag, multi-leader, remote dashboard access | Only if M2/M3 economics justify it |

**Paper mode first is non-negotiable** — MaxIsOntoSomething ships a simulated mode for exactly this reason, and it matches the option-bot's own paper-trade discipline. The dashboard moving into M1 means paper mode is *watchable* from day one — that's what makes the 2-week shadow review real.

---

## 14. Honest economics (set expectations before writing code)

- The leader's realized "grid" PnL over 9 months was ~breakeven after funding and fees. **The only thing worth copying is his directional BTC accumulation** — which a copier enters at *today's* price, not his $64k average.
- Copying costs we pay that he doesn't: taker fees + slippage on whatever fraction misses maker fills, plus the same funding bleed.
- Therefore the realistic value proposition is: *a disciplined, risk-capped way to hold laddered BTC long exposure that follows a proven accumulator's timing* — not a money printer. If the M1 shadow report's tracking-error and cost lines don't show copy-P&L clearly positive vs. simply holding BTC, the correct decision at M1 exit is to stop.

## 15. Open questions

1. Copy capital size for M2? With full ladder mirroring the floor is real: below ~$3k equity his 0.05 BTC rungs round under the $10 minimum and the mirror degrades. $10k mirrors every current rung cleanly.
2. Dashboard reachable from outside the VPS (phone)? v1 binds to localhost only; remote access would need auth and is deferred to M4.
3. *(resolved v3)* Short flips and the resting ladder are now copied — full mirror is the product.

## 16. References

- Leader tracker: [paul.catseye.today](https://paul.catseye.today/) · [Hyperbot profile](https://hyperbot.network/trader/0xdae4df7207feb3b350e4284c8efe5f7dac37f637)
- [jestersimpps/hyperliquid-copytrader](https://github.com/jestersimpps/hyperliquid-copytrader) — WS fill detection + drift-sync + balance-ratio sizing + React dashboard (Node/TS)
- [MaxIsOntoSomething/Hyperliquid_Copy_Trader](https://github.com/MaxIsOntoSomething/Hyperliquid_Copy_Trader) — Python, equity-ratio sizing, leverage adjustment, simulated mode, SQLite
- [chainstacklabs/hyperliquid-trading-bot](https://github.com/chainstacklabs/hyperliquid-trading-bot) — clean Hyperliquid Python SDK usage patterns + risk controls
- [hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk) — official SDK (WS subscriptions: `userFills`, `orderUpdates`; info: `clearinghouseState`)
