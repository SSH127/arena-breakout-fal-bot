from __future__ import annotations

from typing import Protocol

from .models import (
    ExchangeLimitReached,
    ListingFull,
    MerchantSlotsFull,
    StopReason,
    WarehouseFull,
    WorkflowReport,
)


class AutomationDriver(Protocol):
    """Game-specific operations used by the deterministic decision loop."""

    def prepare(self) -> None: ...

    def list_available_stock(self) -> int: ...

    def ensure_exchange_ready(self) -> None: ...

    def exchange_once(self) -> None: ...

    def return_to_exchange(self) -> None: ...

    def log(self, message: str) -> None: ...


class AutoWorkflow:
    """Decide whether to list, exchange, switch pages, or stop.

    OCR and mouse operations stay in the driver. This class only owns policy,
    which makes every transition testable without opening the game.
    """

    def __init__(self, driver: AutomationDriver, exchange_limit: int) -> None:
        if exchange_limit < 1:
            raise ValueError("exchange_limit must be at least 1")
        self.driver = driver
        self.exchange_limit = exchange_limit

    def run(self) -> WorkflowReport:
        exchanged = 0
        listing_batches = 0
        self.driver.prepare()

        try:
            initial = self.driver.list_available_stock()
            listing_batches += initial
        except ListingFull:
            return WorkflowReport(exchanged, listing_batches, StopReason.LISTING_LIMIT)
        except MerchantSlotsFull:
            return WorkflowReport(exchanged, listing_batches, StopReason.MERCHANT_SLOTS_FULL)

        if initial:
            self.driver.log(f"开局先上架了 {initial} 次，再回去兑换")
            self.driver.return_to_exchange()
        else:
            self.driver.ensure_exchange_ready()

        while exchanged < self.exchange_limit:
            gained_this_round = 0
            warehouse_full = False
            exchange_limited = False

            while exchanged < self.exchange_limit:
                self.driver.log(f"第 {exchanged + 1}/{self.exchange_limit} 次兑换")
                try:
                    self.driver.exchange_once()
                except ExchangeLimitReached:
                    self.driver.log("该货物已达到兑换上限，停止兑换。")
                    exchange_limited = True
                    break
                except WarehouseFull:
                    warehouse_full = True
                    self.driver.log("空间不足，停止兑换，转去上架")
                    break
                exchanged += 1
                gained_this_round += 1

            self.driver.log(
                f"本轮兑换 {gained_this_round} 次，累计 {exchanged}/{self.exchange_limit}。"
            )

            # Reaching the configured limit still drains this target item from
            # storage. This keeps AUTO distinct from exchange-only mode.
            try:
                listed = self.driver.list_available_stock()
                listing_batches += listed
            except ListingFull:
                return WorkflowReport(exchanged, listing_batches, StopReason.LISTING_LIMIT)
            except MerchantSlotsFull:
                return WorkflowReport(
                    exchanged, listing_batches, StopReason.MERCHANT_SLOTS_FULL
                )

            if exchanged >= self.exchange_limit:
                self.driver.log("兑换已到上限，上架完这一批后结束。")
                return WorkflowReport(exchanged, listing_batches, StopReason.COMPLETED)

            if exchange_limited:
                self.driver.log("限购后已上架仓库中的目标货物，自动流程结束。")
                return WorkflowReport(
                    exchanged, listing_batches, StopReason.EXCHANGE_LIMIT
                )

            if warehouse_full and gained_this_round == 0 and listed == 0:
                self.driver.log("仓库仍然满，且没有可上架的目标货物，安全停止。")
                return WorkflowReport(exchanged, listing_batches, StopReason.NO_PROGRESS)

            if warehouse_full:
                self.driver.return_to_exchange()
                continue

            return WorkflowReport(exchanged, listing_batches, StopReason.COMPLETED)

        return WorkflowReport(exchanged, listing_batches, StopReason.COMPLETED)

