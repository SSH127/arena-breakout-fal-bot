from __future__ import annotations

import re

from .models import StopReason

MIN_SLOT_LIMIT = 10
MIN_WEEKLY_LIMIT = 100


def parse_market_limit(text: str) -> StopReason | None:
    """Classify market capacity from stable OCR text shown by the game."""

    normalized = text.replace(" ", "")
    if any(
        marker in normalized
        for marker in ("当前售单已达上限", "当前上架数量已满")
    ):
        return StopReason.MERCHANT_SLOTS_FULL
    if any(
        marker in normalized
        for marker in ("本周上架次数已达上限", "本周上架已达上限")
    ):
        return StopReason.LISTING_LIMIT

    # A floating window or a toast can hide the end of a counter, so OCR sees
    # "45/1" instead of "45/1000". Treating that as "full" stops a healthy run.
    # Real limits are never that small, so a truncated denominator is ignored.
    slots = re.search(r"我的上架物品[（(]?(\d+)/(\d+)", normalized)
    if slots and int(slots.group(2)) >= MIN_SLOT_LIMIT and int(slots.group(1)) >= int(slots.group(2)):
        return StopReason.MERCHANT_SLOTS_FULL

    weekly = re.search(r"本周上架[:：]?(\d+)/(\d+)", normalized)
    if weekly and int(weekly.group(2)) >= MIN_WEEKLY_LIMIT and int(weekly.group(1)) >= int(weekly.group(2)):
        return StopReason.LISTING_LIMIT
    return None

