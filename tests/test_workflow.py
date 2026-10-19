from __future__ import annotations

import unittest

from falbot.limits import parse_market_limit
from falbot.models import (
    ExchangeLimitReached,
    ListingFull,
    MerchantSlotsFull,
    StopReason,
    WarehouseFull,
)
from falbot.workflow import AutoWorkflow


class FakeDriver:
    def __init__(
        self,
        *,
        initial_listed: int = 0,
        full_after: int | None = None,
        listed_after_full: int = 0,
        listing_blocked: bool = False,
        merchant_slots_full: bool = False,
        exchange_limited: bool = False,
    ) -> None:
        self.initial_listed = initial_listed
        self.full_after = full_after
        self.listed_after_full = listed_after_full
        self.listing_blocked = listing_blocked
        self.merchant_slots_full = merchant_slots_full
        self.exchange_limited = exchange_limited
        self.exchange_calls = 0
        self.list_calls = 0
        self.returns = 0
        self.messages: list[str] = []

    def prepare(self) -> None:
        pass

    def list_available_stock(self) -> int:
        self.list_calls += 1
        if self.merchant_slots_full:
            raise MerchantSlotsFull
        if self.listing_blocked:
            raise ListingFull
        if self.list_calls == 1:
            return self.initial_listed
        return self.listed_after_full

    def ensure_exchange_ready(self) -> None:
        pass

    def exchange_once(self) -> None:
        if self.exchange_limited:
            self.exchange_limited = False
            raise ExchangeLimitReached
        if self.full_after is not None and self.exchange_calls >= self.full_after:
            # Only block once; after listing, the next exchange can proceed.
            self.full_after = None
            raise WarehouseFull
        self.exchange_calls += 1

    def return_to_exchange(self) -> None:
        self.returns += 1

    def log(self, message: str) -> None:
        self.messages.append(message)


class AutoWorkflowTests(unittest.TestCase):
    def test_lists_existing_stock_before_exchange(self) -> None:
        driver = FakeDriver(initial_listed=2)

        report = AutoWorkflow(driver, exchange_limit=3).run()

        self.assertEqual(report.exchanged, 3)
        self.assertEqual(report.listing_batches, 2)
        self.assertEqual(report.stop_reason, StopReason.COMPLETED)
        self.assertGreaterEqual(driver.returns, 1)

    def test_warehouse_full_switches_to_listing_then_resumes(self) -> None:
        driver = FakeDriver(full_after=1, listed_after_full=1)

        report = AutoWorkflow(driver, exchange_limit=3).run()

        self.assertEqual(report.exchanged, 3)
        self.assertEqual(report.stop_reason, StopReason.COMPLETED)
        self.assertGreaterEqual(report.listing_batches, 1)

    def test_listing_limit_is_terminal(self) -> None:
        driver = FakeDriver(listing_blocked=True)

        report = AutoWorkflow(driver, exchange_limit=3).run()

        self.assertEqual(report.stop_reason, StopReason.LISTING_LIMIT)
        self.assertEqual(driver.exchange_calls, 0)

    def test_merchant_slots_full_is_terminal(self) -> None:
        driver = FakeDriver(merchant_slots_full=True)

        report = AutoWorkflow(driver, exchange_limit=3).run()

        self.assertEqual(report.stop_reason, StopReason.MERCHANT_SLOTS_FULL)
        self.assertEqual(driver.exchange_calls, 0)

    def test_exchange_limit_lists_stock_then_stops(self) -> None:
        driver = FakeDriver(exchange_limited=True, listed_after_full=2)

        report = AutoWorkflow(driver, exchange_limit=3).run()

        self.assertEqual(report.stop_reason, StopReason.EXCHANGE_LIMIT)
        self.assertEqual(report.listing_batches, 2)


class MarketLimitTests(unittest.TestCase):
    def test_video_slot_counter_is_merchant_capacity(self) -> None:
        text = "我的上架物品（45/45）（+10.情报站LV.3+10）本周上架45/1000"
        self.assertEqual(parse_market_limit(text), StopReason.MERCHANT_SLOTS_FULL)

    def test_video_slot_toasts_are_merchant_capacity(self) -> None:
        self.assertEqual(
            parse_market_limit("当前售单已达上限"),
            StopReason.MERCHANT_SLOTS_FULL,
        )
        self.assertEqual(
            parse_market_limit("当前上架数量已满"),
            StopReason.MERCHANT_SLOTS_FULL,
        )

    def test_counter_cut_off_by_floating_window_is_not_full(self) -> None:
        # Recorded run: the panel hid "00", OCR read "本周上架45/1".
        text = "我的上架物品（18/45）（+10.情报站LV.3+10）本周上架45/1"
        self.assertIsNone(parse_market_limit(text))
        self.assertIsNone(parse_market_limit("我的上架物品（18/4"))

    def test_real_counters_below_limit_are_fine(self) -> None:
        text = "我的上架物品（18/45）本周上架45/1000"
        self.assertIsNone(parse_market_limit(text))

    def test_weekly_counter_is_listing_quota(self) -> None:
        text = "我的上架物品（12/45）本周上架1000/1000"
        self.assertEqual(parse_market_limit(text), StopReason.LISTING_LIMIT)


if __name__ == "__main__":
    unittest.main()

