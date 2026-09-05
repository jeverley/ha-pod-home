"""Select platform for pod_home - the Charge Priority control."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CHARGE_PRIORITY_BASIC_OPTIONS,
    CHARGE_PRIORITY_SMART_OPTIONS,
    DOMAIN,
    SCHEDULE_MODE_BASIC_CHARGING,
)
from .entity import PodHomeEntity, async_setup_dynamic_chargers
from .helpers import (
    charge_priority_available,
    charge_priority_label_basic,
    charging_priority_label,
    max_price_for_charging_priority,
    schedule_mode,
)

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [PodHomeChargingStrategySelect],
    )


class PodHomeChargingStrategySelect(PodHomeEntity, SelectEntity):
    """Settable Charge Priority - the same underlying "respect the schedule/cost plan vs
    prioritise charging over it" choice in both Charging Modes, on the charger device since
    preferences are charger-scoped. Matches the app's own wording exactly per mode rather than a
    shared generic label (per the user's explicit decision - see DECISIONS.md): Smart Charging
    offers Lowest cost/Complete charge (maxPrice-based - read/write both go through maxPrice, not
    chargingStrategy, see charging_priority_label()/max_price_for_charging_priority() in
    helpers.py); Basic Charging offers Schedule/Always on (charge-overrides-based - see
    charge_priority_label_basic() in helpers.py and PodHomeCharger.always_on_active,
    coordinator.py). NOT gated by mode for existence - relevant in both; only tariff-gated in
    Smart Charging (a single-rate tariff makes Lowest cost/Complete charge indistinguishable).

    Basic Charging is READ-ONLY for now - the write shape for creating/clearing Always on's
    indefinite override has never been confirmed live (an explicit `endAt: null` from this
    integration's own Boost button was rejected, 403; the app's own request is evidently shaped
    differently - see DECISIONS.md). async_select_option raises a clear error rather than
    guessing at an unconfirmed write."""

    _attr_translation_key = "charging_strategy"
    _attr_name = "Charge priority"
    _attr_icon = "mdi:sort-variant"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_charging_strategy"

    @property
    def options(self) -> list[str]:
        charger = self.charger
        mode = schedule_mode(charger.delegated_control_status) if charger else None
        if mode == SCHEDULE_MODE_BASIC_CHARGING:
            return CHARGE_PRIORITY_BASIC_OPTIONS
        return CHARGE_PRIORITY_SMART_OPTIONS

    @property
    def available(self) -> bool:
        charger = self.charger
        if not super().available or not charger:
            return False
        if schedule_mode(charger.delegated_control_status) == SCHEDULE_MODE_BASIC_CHARGING:
            # Schedule vs Always on doesn't depend on tariff shape at all, unlike Smart
            # Charging's cost-vs-completion choice below.
            return True
        return charge_priority_available(charger.tariff_windows)

    @property
    def current_option(self) -> str | None:
        charger = self.charger
        if not charger:
            return None
        if schedule_mode(charger.delegated_control_status) == SCHEDULE_MODE_BASIC_CHARGING:
            return charge_priority_label_basic(charger.always_on_active)
        return charging_priority_label(charger.max_price, charger.tariff_windows)

    async def async_select_option(self, option: str) -> None:
        charger = self.charger
        if not charger:
            raise HomeAssistantError("No charger to set Charge Priority for")
        if schedule_mode(charger.delegated_control_status) == SCHEDULE_MODE_BASIC_CHARGING:
            raise HomeAssistantError(
                "Setting Schedule/Always on isn't supported yet - use the Pod Home app for now"
                " (see DECISIONS.md - the write endpoint hasn't been confirmed live)"
            )
        max_price = max_price_for_charging_priority(option, charger.tariff_windows)
        if max_price is None:
            raise HomeAssistantError(
                f"Couldn't determine a maxPrice to write for Charge Priority option {option!r} "
                "- either it's unrecognized, or this charger's tariff data isn't known yet"
            )
        await self.coordinator.api.async_set_charge_priority_max_price(self.ppid, max_price)
        await self.coordinator.async_request_refresh()
