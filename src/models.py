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
    # His TRUE size. Never reconstruct it as sz/scale — our sz is floor-rounded,
    # so the round-trip lands below his real size and makes every rung look
    # amended on the next cycle.
    leader_sz: float = 0.0


class Verdict(BaseModel):
    approved: bool
    reason: str = ""
