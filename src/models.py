"""Core data models shared by every module."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel


class RiskState(str, Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    HALT = "HALT"


class Order(BaseModel):
    oid: int
    side: Literal["B", "A"]  # B=buy, A=sell (Hyperliquid convention)
    px: float
    sz: float
    ts_ms: int


class AccountState(BaseModel):
    equity: float
    position: float  # signed BTC — shorts are copied, so this can be negative
    entry_px: float | None
    mark_px: float
    fetched_at_ms: int
    open_orders: list[Order] = []


class MirrorAction(BaseModel):
    kind: Literal["place", "cancel"]
    side: Literal["B", "A"]
    px: float
    sz: float
    leader_oid: int
    our_oid: int | None = None


class Verdict(BaseModel):
    approved: bool
    reason: str = ""
