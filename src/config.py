"""YAML config loader. Paper mode is the shipped default — see config.example.yaml."""

from pathlib import Path

import yaml
from pydantic import BaseModel


class Mirror(BaseModel):
    # Order-mirroring and short-copying are unconditional — the whole product is
    # a faithful mirror, so they were never real switches.
    scale_rebalance_pct: float = 5
    drift_threshold_pct: float = 1.0


class Risk(BaseModel):
    mirror_parity_tolerance: float = 1.05
    max_drawdown_pct: float = -35
    stop_loss_overlay: float | None = None
    leader_staleness_max_s: int = 300
    funding_alert_pct_30d: float = 2.0


class Execution(BaseModel):
    maker_wait_s: int = 90
    taker_slippage_cap_pct: float = 0.15


class Storage(BaseModel):
    db_path: str = "data/copybot.db"
    snapshot_interval_s: int = 60


class Dashboard(BaseModel):
    port: int = 8061
    refresh_s: int = 15


class Paper(BaseModel):
    start_equity: float = 10_000


class Telegram(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class Config(BaseModel):
    leader: str
    mode: str = "paper"
    api_url: str = "https://api.hyperliquid.xyz"
    our_address: str = ""
    mirror: Mirror = Mirror()
    risk: Risk = Risk()
    execution: Execution = Execution()
    storage: Storage = Storage()
    dashboard: Dashboard = Dashboard()
    paper: Paper = Paper()
    telegram: Telegram = Telegram()


def load_config(path: str | Path) -> Config:
    return Config.model_validate(yaml.safe_load(Path(path).read_text()))
