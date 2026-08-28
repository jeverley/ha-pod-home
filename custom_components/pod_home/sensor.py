"""Sensor platform for pod_home."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CHARGING_STATE_OPTIONS, DEFAULT_CURRENCY, DOMAIN
from .entity import PodHomeEntity, async_setup_dynamic_chargers
from .helpers import known_or_none

if TYPE_CHECKING:
    from . import PodHomeConfigEntry

# Coordinator-backed, read-only platform - the coordinator centralizes inbound polling, but
# PARALLEL_UPDATES still needs setting explicitly per the quality-scale rule.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: PodHomeConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_setup_dynamic_chargers(
        entry,
        entry.runtime_data,
        async_add_entities,
        [
            PodHomeStatusSensor,
            PodHomeLastChargeDurationSensor,
            PodHomeLastChargeEnergySensor,
            PodHomeLastChargeCostSensor,
            PodHomeEnergyMonthSensor,
            PodHomeCostMonthSensor,
        ],
    )


class PodHomeStatusSensor(PodHomeEntity, SensorEntity):
    """Charging state, e.g. Available/Charging. See const.py for which values are confirmed
    live vs. still a guess - anything beyond Available has not been seen in a real response."""

    _attr_translation_key = "status"
    _attr_name = "Status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CHARGING_STATE_OPTIONS

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_status"

    @property
    def native_value(self) -> str | None:
        charger = self.charger
        if not charger:
            return None
        # known_or_none, not the raw value: an unrecognized chargingState passed straight to a
        # SensorDeviceClass.ENUM entity makes HA core itself log an error on every state read
        # (checked against _attr_options every access, not just once) - see helpers.py.
        return known_or_none(charger.charging_state, self._attr_options)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        # charging_state is None whenever this poll's connectivity-status fetch failed (see
        # coordinator.py's _safe_call) - that's a missing value, not an unrecognized one, and
        # must not be reported as if the API sent a real unfamiliar value.
        if not charger or charger.charging_state is None:
            return None
        if charger.charging_state in self._attr_options:
            return None
        # The raw value is still surfaced here rather than silently dropped, since it's a real
        # API value CHARGING_STATE_OPTIONS doesn't know about yet.
        return {"raw_charging_state": charger.charging_state}


class PodHomeLastChargeDurationSensor(PodHomeEntity, SensorEntity):
    """Duration (seconds) of the most recent charge session found in /charges."""

    _attr_translation_key = "last_charge_duration"
    _attr_name = "Last Charge Duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_icon = "mdi:timer"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_last_charge_duration"

    @property
    def native_value(self) -> int | None:
        charger = self.charger
        if not charger or not charger.latest_charge:
            return None
        return charger.latest_charge.duration


class PodHomeLastChargeEnergySensor(PodHomeEntity, SensorEntity):
    """Energy (kWh) delivered in the most recent charge session.

    Session snapshot, NOT dashboard-safe: this jumps between arbitrary session totals rather
    than accumulating, which is exactly the wrong shape for the Energy Dashboard even though
    it shares a device_class with PodHomeEnergyMonthSensor. Use Energy This Month for that.
    """

    _attr_translation_key = "last_charge_energy"
    _attr_name = "Last Charge Energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_last_charge_energy"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        if not charger or not charger.latest_charge:
            return None
        return charger.latest_charge.energy_total


class PodHomeLastChargeCostSensor(PodHomeEntity, SensorEntity):
    """Cost of the most recent charge session. cost.amount is in minor units (pence for GBP),
    matching the old API's convention - divided down to whole currency units here.

    Session snapshot, NOT dashboard-safe - see PodHomeLastChargeEnergySensor's docstring. Use
    Cost This Month for the Energy Dashboard.
    """

    _attr_translation_key = "last_charge_cost"
    _attr_name = "Last Charge Cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:cash"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_last_charge_cost"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        if not charger or not charger.latest_charge or charger.latest_charge.cost_amount is None:
            return None
        return charger.latest_charge.cost_amount / 100

    @property
    def native_unit_of_measurement(self) -> str | None:
        charger = self.charger
        if not charger or not charger.latest_charge:
            return None
        return charger.latest_charge.cost_currency


class PodHomeEnergyMonthSensor(PodHomeEntity, SensorEntity):
    """Energy delivered so far this calendar month (charger's local time), for the Energy
    Dashboard. state_class=TOTAL_INCREASING: resets naturally on the 1st of each month, which
    HA's recorder/statistics treat as an expected reset, building the real long-term total from
    this sensor's own recorded history rather than the API providing one directly - it doesn't
    expose a true lifetime total, only date-range aggregation.
    """

    _attr_translation_key = "energy_month"
    _attr_name = "Energy This Month"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:lightning-bolt"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_energy_month"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        return charger.month_energy_kwh if charger else None


class PodHomeCostMonthSensor(PodHomeEntity, SensorEntity):
    """Cost so far this calendar month - see PodHomeEnergyMonthSensor's docstring for the
    TOTAL_INCREASING/reset reasoning. Currency comes from the coordinator's account-level
    balance.currency (fetched once via GET /users, not derivable from charge-statistics itself
    - see coordinator.py's _async_fetch_currency)."""

    _attr_translation_key = "cost_month"
    _attr_name = "Cost This Month"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:cash-multiple"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_cost_month"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        if not charger or charger.month_cost_amount is None:
            return None
        return charger.month_cost_amount / 100

    @property
    def native_unit_of_measurement(self) -> str:
        # coordinator.currency stays None until GET /users actually succeeds (see
        # coordinator.py's _async_fetch_currency - it retries every poll rather than locking
        # in a guess) - fall back to a display-only default in the meantime rather than an
        # unlabelled monetary value.
        return self.coordinator.currency or DEFAULT_CURRENCY
