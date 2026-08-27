# Hyperliquid Copybot

Mirrors a Hyperliquid BTC-perp trader's **whole book** — his resting order
ladder, his sizing proportions, his margin config and his risk profile — scaled
to your equity. Runs standalone from a double-click, with a
[paul.catseye.today](https://paul.catseye.today/)-style tracking dashboard.

Leader: [`0xdae4df7207feb3b350e4284c8efe5f7dac37f637`](https://hyperbot.network/trader/0xdae4df7207feb3b350e4284c8efe5f7dac37f637)

**Design: [PRD.md](PRD.md) · Phase plans: [docs/plans/](docs/plans/)**

---

## Quick start

1. Install **Python 3.10+** from python.org (the only prerequisite).
2. Double-click **`start_copybot.bat`**.

That's it. The first run creates the virtual environment, installs
dependencies, copies `config.example.yaml` → `config.yaml`, creates
`logs/`, `data/` and `reports/`, starts the bot in the background (no console
window) and opens the dashboard at <http://localhost:8061>.

Later runs skip straight to launching. Dependencies re-install automatically
whenever `requirements.txt` changes.

To stop: double-click **`stop_copybot.bat`**. It pulls your resting orders but
**keeps your position** — stopping the bot never liquidates a healthy book.

> **It starts in `paper` mode.** Nothing touches real money until you
> deliberately edit `config.yaml`. Leave it in paper for the two-week shadow run
> described in [the M1 plan](docs/plans/2026-08-27-M1-shadow.md).

### Windows note

Keep the folder path short (e.g. `C:\copybot`). Deep paths break `pip` with
`WinError 206`; the launcher detects this and tells you how to fix it.

---

## What it actually does

Every 60 seconds:

1. Reads the leader's account state, resting ladder and recent fills.
2. Mirrors his ladder: **our open orders = his open orders × scale, at his exact
   prices**, so when price reaches a rung we fill as makers alongside him.
3. Reconciles any position drift over 1% (his taker trades, missed fills).
4. Runs the risk backstops and records every decision to SQLite.

`scale = our_equity / leader_equity`, applied to every rung and the position, so
his pyramid structure and leverage profile carry over exactly.

## Risk model — read this

This bot **copies the leader's risk management rather than replacing it**. He
averages down into dips with pyramiding size and has never placed a stop in nine
months of history. Mirroring him faithfully means inheriting that.

What protects the account:

| Backstop | Behaviour |
|---|---|
| **Mirror parity (B2)** | We can never be *more* exposed than `leader × scale × 1.05`. |
| **Drawdown kill-switch** | −35% from the high-water mark → HALT: cancel everything, flatten, stop copying. |
| **Connection watchdog** | Leader data stale → WARNING: no new orders; existing ladder kept. |
| **HALT button** | On the dashboard. Cancels and flattens within one cycle. |
| **Stop-loss overlay** | **Off by default** (he trades without stops). Set `risk.stop_loss_overlay` to opt in. |

HALT is **sticky across restarts**, and the high-water mark is persisted, so a
reboot cannot clear the kill-switch. To resume after a HALT:

```bash
venv\Scripts\python.exe -c "import time; from src.config import load_config; from src.store import Store; c=load_config('config.yaml'); Store(c.storage.db_path).record_event(int(time.time()*1000),'info','manual_reset','operator resume')"
```

This also re-bases the high-water mark to your current equity, otherwise the
kill-switch would trip again on the very next cycle.

## Honest economics

Over nine months the leader's realised ladder P&L was roughly **break-even after
funding and fees** (+$2,553 realised, −$1,765 funding, −$269 fees). His +22%
came almost entirely from one directional position: 1.336 BTC accumulated around
$64k during the crash. A copier buys that same position at *today's* price, so
there is no entry-price edge — only his timing and discipline, minus your own
costs. If the daily report's tracking-error and cost lines do not beat simply
holding BTC, the correct decision is to stop.

## Going live

1. Create a Hyperliquid **API wallet** (app.hyperliquid.xyz → More → API).
   Never use your main wallet key.
2. `copy secrets.example.yaml secrets.yaml` and paste the API wallet key.
3. In `config.yaml` set `mode: live`. Optionally point `api_url` at
   `https://api.hyperliquid-testnet.xyz` first.
4. Restart. The bot refuses to start live without a well-formed `secrets.yaml`.

Before risking real size, run the testnet fire-drill in
[docs/plans/M3-drill-log.md](docs/plans/M3-drill-log.md).

## Dashboard

<http://localhost:8061> — risk-state banner, BTC candles (Hyperliquid's own)
with **both accounts' pending orders drawn as labelled dashed lines**, fill
markers, leverage and equity curves, drawdown vs the kill-switch, mirror health,
the live decision log, and a red HALT button.

## Layout

```
start_copybot.bat / stop_copybot.bat   double-click entry points
config.yaml                            your settings (created on first run)
secrets.yaml                           live API wallet key (git-ignored)
data/copybot.db                        SQLite: the single source of truth
logs/copybot.jsonl                     structured logs, rotated daily
reports/daily-YYYYMMDD.html            daily reports
src/  watcher sizer risk paper live executor store dashboard report main
tests/                                 112 tests
```

Run the tests with:

```bash
venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

```bash
venv\Scripts\python.exe -m pytest tests/ -q
```

## Disclaimer

Trading perpetual futures with leverage can lose more than your deposit. This
software copies a stranger's trades and has no opinion about whether that is
wise. Use paper mode until the numbers convince you, and never fund it with
money you cannot afford to lose.
