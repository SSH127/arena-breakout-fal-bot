from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ItemProfile:
    """OCR vocabulary and routing data for one exchange target."""

    key: str
    display_name: str
    contact_names: tuple[str, ...]
    category_names: tuple[str, ...]
    shop_name_parts: tuple[str, ...]
    stash_name_parts: tuple[str, ...]
    excluded_name_parts: tuple[str, ...] = ()
    exchange_limit_markers: tuple[str, ...] = (
        "兑换次数已达上限",
        "已达兑换上限",
        "限购次数已用完",
        "本期限购已达上限",
        "已售罄",
    )
    max_items_per_listing: int = 5


FAL_HANDGUARD = ItemProfile(
    key="fal_standard_handguard",
    display_name="FAL四导轨标准护木",
    contact_names=("兰德尔", "费舍"),
    category_names=("核心配件", "心配件"),
    shop_name_parts=("四导轨", "标准"),
    stash_name_parts=("FAL标准护", "标准护", "准护"),
    excluded_name_parts=("加长", "枪管", "枪托"),
    max_items_per_listing=5,
)


ITEM_PROFILES: dict[str, ItemProfile] = {
    FAL_HANDGUARD.key: FAL_HANDGUARD,
}

