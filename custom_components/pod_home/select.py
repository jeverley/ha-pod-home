"""Select platform for pod_home - the Charge Mode control."""
from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CHARGE_PRIORITY_ALWAYS_ON,
    CHARGE_PRIORITY_BASIC_OPTIONS,
    CHARGE_PRIORITY_SCHEDULE,
    CHARGE_PRIORITY_SMART_OPTIONS,
    DOMAIN,
    SCHEDULE_MODE_BASIC_CHARGING,
)
from .entity import PodHomeEntity, PodHomeOptimisticWriteMixin, async_setup_dynamic_chargers
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
        [PodHomeChargeModeSelect],
    )


class PodHomeChargeModeSelect(PodHomeOptimisticWriteMixin, PodHomeEntity, SelectEntity):
    """Settable Charge Mode - "respect the schedule/cost plan vs prioritise charging over it",
    on the charger device since preferences are charger-scoped. Smart Charging offers Lowest
    cost/Complete charge (maxPrice-based, see charging_priority_label()/
    max_price_for_charging_priority() in helpers.py); Basic Charging offers Schedule/Always on
    (charge-overrides-based, see charge_priority_label_basic()). Relevant in both schemes; only
    tariff-gated in Smart Charging (a single-rate tariff makes the two options indistinguishable).

    Basic Charging writes too: Always on POSTs `requestedAt` only, no `endAt` key at all, via
    async_set_always_on() (client.py); Schedule DELETEs the active override, only when one is
    actually active.

    PodHomeOptimisticWriteMixin masks current_option's read-your-own-write race after each
    write."""

    _attr_translation_key = "charge_mode"
    _attr_name = "Charge mode"
    _attr_icon = "mdi:sort-variant"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_charge_mode"

    def _is_basic_charging(self, charger) -> bool:
        """Shared by every property/method below so schedule_mode() isn't re-derived
        independently in each one - mirrors _smart_mode_available's pattern (entity.py)."""
        return (
            charger is not None
            and schedule_mode(charger.delegated_control_status) == SCHEDULE_MODE_BASIC_CHARGING
        )

    @property
    def options(self) -> list[str]:
        if self._is_basic_charging(self.charger):
            return CHARGE_PRIORITY_BASIC_OPTIONS
        return CHARGE_PRIORITY_SMART_OPTIONS

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        charger = self.charger
        if self._is_basic_charging(charger):
            # Schedule vs Always on doesn't depend on tariff shape at all, unlike Smart
            # Charging's cost-vs-completion choice below.
            return True
        return charge_priority_available(charger.tariff_windows)

    @property
    def current_option(self) -> str | None:
        optimistic = self._read_optimistic_value()
        if optimistic is not None:
            return optimistic
        charger = self.charger
        if not charger:
            return None
        if self._is_basic_charging(charger):
            return charge_priority_label_basic(charger.always_on_active)
        return charging_priority_label(charger.max_price, charger.tariff_windows)

    async def async_select_option(self, option: str) -> None:
        charger = self.charger
        if not charger:
            raise HomeAssistantError("No charger to set Charge Mode for")
        if self._is_basic_charging(charger):
            if option == CHARGE_PRIORITY_ALWAYS_ON:
                if not charger.always_on_active:
                    await self.coordinator.api.async_set_always_on(self.ppid, dt_util.utcnow())
                    self._set_optimistic_value(option)
                    await self.coordinator.async_request_refresh()
            elif option == CHARGE_PRIORITY_SCHEDULE:
                if charger.always_on_active:
                    await self.coordinator.api.async_delete_charge_override(self.ppid)
                    self._set_optimistic_value(option)
                    await self.coordinator.async_request_refresh()
            else:
                raise HomeAssistantError(f"Unrecognized Basic Charging option {option!r}")
            return
        max_price = max_price_for_charging_priority(option, charger.tariff_windows)
        if max_price is None:
            raise HomeAssistantError(
                f"Couldn't determine a maxPrice to write for Charge Mode option {option!r} "
                "- either it's unrecognized, or this charger's tariff data isn't known yet"
            )
        await self.coordinator.api.async_set_charge_priority_max_price(self.ppid, max_price)
        self._set_optimistic_value(option)
        await self.coordinator.async_request_refresh()
