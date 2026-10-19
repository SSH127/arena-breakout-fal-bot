from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunMode(str, Enum):
    AUTO = "auto"
    EXCHANGE = "exchange"
    LIST = "list"


class StopReason(str, Enum):
    COMPLETED = "completed"
    EXCHANGE_LIMIT = "exchange_limit"
    LISTING_LIMIT = "listing_limit"
    MERCHANT_SLOTS_FULL = "merchant_slots_full"
    NO_PROGRESS = "no_progress"


class WarehouseFull(RuntimeError):
    """The exchange dialog cannot put another item into storage."""


class ExchangeLimitReached(RuntimeError):
    """The selected trader item has reached its configured purchase limit."""


class ListingFull(RuntimeError):
    """The account has no remaining listing attempts."""


class MerchantSlotsFull(RuntimeError):
    """All merchant/listing slots are occupied."""


@dataclass(frozen=True)
class WorkflowReport:
    exchanged: int
    listing_batches: int
    stop_reason: StopReason

