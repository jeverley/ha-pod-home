"""Sensor platform for pod_home."""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfLength,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    CHARGER_STATUS_AVAILABLE,
    CHARGER_STATUS_CHARGING,
    CHARGER_STATUS_FAULT,
    CHARGER_STATUS_FINISHED,
    CHARGER_STATUS_FINISHING,
    CHARGER_STATUS_OPTIONS,
    CHARGER_STATUS_PAUSED,
    CHARGER_STATUS_PREPARING,
    CHARGER_STATUS_RESERVED,
    CHARGER_STATUS_UNAVAILABLE,
    CHARGING_STATE_OPTIONS,
    DEFAULT_CURRENCY,
    DOMAIN,
    POWER_DELIVERY_STATE_OPTIONS,
    SCHEDULE_MODE_OPTIONS,
)
from .coordinator import PodHomeTariffWindow
from .entity import (
    PodHomeAccountEntity,
    PodHomeEntity,
    PodHomeVehicleEntity,
    async_setup_dynamic_chargers,
    async_setup_dynamic_vehicles,
)
from .helpers import (
    charger_status,
    currency_icon,
    known_or_none,
    parse_time_of_day,
    resolve_timezone,
    schedule_mode,
    select_last_charge,
    smart_mode_available,
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
        [
            PodHomeChargingStateSensor,
            PodHomeChargingModeSensor,
            PodHomeStatusSensor,
            PodHomeLastChargeDurationSensor,
            PodHomeLastChargeEnergySensor,
            PodHomeLastChargeCostSensor,
            PodHomeMonthEnergySensor,
            PodHomeMonthCostSensor,
            PodHomeTotalEnergySensor,
            PodHomeElectricityRateSensor,
            PodHomeBoostEndTimeSensor,
        ],
    )
    async_setup_dynamic_vehicles(
        entry,
        entry.runtime_data,
        async_add_entities,
        [
            PodHomeVehicleBatteryLevelSensor,
            PodHomeVehicleRangeSensor,
            PodHomeVehicleOdometerSensor,
            PodHomeVehicleExpectedChargeSensor,
            PodHomeVehiclePowerDeliveryStateSensor,
            PodHomeVehicleChargeRateSensor,
            PodHomeVehicleMaxCurrentSensor,
            PodHomeVehicleChargeTimeRemainingSensor,
        ],
    )
    # Account-wide, not per-charger/vehicle - a single static entity, not run through either
    # dynamic-discovery helper above.
    async_add_entities([PodHomeRewardsBalanceSensor(entry.runtime_data)])


class PodHomeChargingStateSensor(PodHomeEntity, SensorEntity):
    """Raw chargingState passthrough (e.g. Available/Charging), unfiltered. Diagnostic sibling
    to Status (helpers.charger_status()), which derives a smaller, user-meaningful value."""

    _attr_translation_key = "charging_state"
    _attr_name = "Charging state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CHARGING_STATE_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_charging_state"

    @property
    def native_value(self) -> str | None:
        charger = self.charger
        if not charger:
            return None
        return known_or_none(charger.charging_state, self._attr_options)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        if not charger or charger.charging_state is None:
            return None
        if charger.charging_state in self._attr_options:
            return None
        return {"raw_charging_state": charger.charging_state}


class PodHomeChargingModeSensor(PodHomeEntity, SensorEntity):
    """Whether Smart Charging or Basic Charging is active, from delegatedControl.status. Shown
    as "Smart"/"Basic" (see schedule_mode() in helpers.py). status_effective_from costs an extra
    call, refreshed on the same 6h cadence as firmware/tariffs. smart_charging_supported reflects
    tariff compatibility (Smart Charging needs a single- or two-rate tariff)."""

    _attr_translation_key = "charging_mode"
    _attr_name = "Charging mode"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SCHEDULE_MODE_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_charging_mode"

    @property
    def native_value(self) -> str | None:
        charger = self.charger
        if not charger:
            return None
        return schedule_mode(charger.delegated_control_status)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        if not charger or charger.delegated_control_status is None:
            return None
        effective_from = charger.delegated_control_status_effective_from
        return {
            "raw_delegated_control_status": charger.delegated_control_status,
            "status_effective_from": effective_from.isoformat() if effective_from else None,
            "smart_charging_supported": charger.smart_charging_supported,
        }


class PodHomeStatusSensor(PodHomeEntity, SensorEntity):
    """Small, user-meaningful status derived from chargingState plus sticky timestamps (see
    charger_status() in helpers.py). No Unplugged value - Cable Status (binary_sensor.py) covers
    that. Charging State above still exposes the raw wire value."""

    _attr_translation_key = "status"
    _attr_name = "Status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = CHARGER_STATUS_OPTIONS

    # HA's icon system maps per numeric range, not per enum value, so per-status icons need
    # doing manually here (same pattern as PodHomeConnectivitySensor.icon, binary_sensor.py).
    _STATUS_ICONS = {
        CHARGER_STATUS_CHARGING: "mdi:ev-station",
        CHARGER_STATUS_PAUSED: "mdi:pause-circle-outline",
        CHARGER_STATUS_AVAILABLE: "mdi:power-plug-off",
        CHARGER_STATUS_PREPARING: "mdi:timer-sand",
        CHARGER_STATUS_FINISHING: "mdi:progress-check",
        CHARGER_STATUS_RESERVED: "mdi:calendar-clock",
        CHARGER_STATUS_UNAVAILABLE: "mdi:alert-circle-outline",
        CHARGER_STATUS_FINISHED: "mdi:check-circle",
        CHARGER_STATUS_FAULT: "mdi:alert-circle",
    }

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_status"

    @property
    def native_value(self) -> str | None:
        charger = self.charger
        if not charger:
            return None
        return charger_status(charger, dt_util.utcnow())

    @property
    def icon(self) -> str:
        return self._STATUS_ICONS.get(self.native_value, "mdi:ev-station")


class PodHomeLastChargeDurationSensor(PodHomeEntity, SensorEntity):
    """Duration of the most recent charge session - live (current_charge) if in progress, else
    the last finished one (latest_charge). While live in Smart Charging mode this is cumulative
    charging time, not time-since-plug-in (see cumulative_charging_seconds(), helpers.py); Basic
    Charging mode has no schedule to refine against, so it stays time-since-plug-in. Native unit
    stays seconds for full recorder precision; suggested_unit_of_measurement only changes the
    default display to hours."""

    _attr_translation_key = "last_charge_duration"
    _attr_name = "Last charge duration"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_suggested_unit_of_measurement = UnitOfTime.HOURS
    _attr_icon = "mdi:timer"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_last_charge_duration"

    @property
    def native_value(self) -> int | None:
        charge = self.last_charge
        return charge.duration if charge else None


class PodHomeLastChargeEnergySensor(PodHomeEntity, SensorEntity):
    """Energy delivered in the most recent charge session - live if in progress (current_charge),
    else the last finished one (latest_charge). A session snapshot, not dashboard-safe - use
    Month Energy for the Energy Dashboard instead."""

    _attr_translation_key = "last_charge_energy"
    _attr_name = "Last charge energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_last_charge_energy"

    @property
    def native_value(self) -> float | None:
        charge = self.last_charge
        return charge.energy_total if charge else None


class PodHomeLastChargeCostSensor(PodHomeEntity, SensorEntity):
    """Cost of the most recent charge session. Unlike Duration/Energy, NOT live: cost is unknown
    while a session is in progress (current_charge.cost_amount is always 0 there, so it's left
    None rather than shown misleadingly), populating once mobile-api reports the session finished
    (latest_charge, via select_last_charge()). A session snapshot, not dashboard-safe - use Month
    Cost for the Energy Dashboard instead."""

    _attr_translation_key = "last_charge_cost"
    _attr_name = "Last charge cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_icon = "mdi:cash"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_last_charge_cost"

    @property
    def native_value(self) -> float | None:
        charge = self.last_charge
        if not charge or charge.cost_amount is None:
            return None
        return charge.cost_amount / 100

    @property
    def native_unit_of_measurement(self) -> str | None:
        charge = self.last_charge
        return charge.cost_currency if charge else None


class PodHomeMonthEnergySensor(PodHomeEntity, SensorEntity):
    """Energy delivered so far this calendar month (charger's local time), finalized charges
    only - matches the app. Deliberately excludes current_charge's live total: a session
    spanning the month boundary can't be split between months, so this lags until the session
    finalizes, same as the app. See Total Energy below for a live-inclusive running total."""

    _attr_translation_key = "month_energy"
    _attr_name = "Month energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:lightning-bolt"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_month_energy"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        return charger.month_energy_kwh if charger else None


class PodHomeMonthCostSensor(PodHomeEntity, SensorEntity):
    """Cost so far this calendar month - finalized charges only, same reasoning as Month energy
    above (no live current_charge top-up, see that sensor's docstring)."""

    _attr_translation_key = "month_cost"
    _attr_name = "Month cost"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:cash-multiple"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_month_cost"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        if not charger or charger.month_cost_amount is None:
            return None
        return charger.month_cost_amount / 100

    @property
    def native_unit_of_measurement(self) -> str:
        return self.coordinator.currency or DEFAULT_CURRENCY

    @property
    def last_reset(self) -> datetime.datetime | None:
        charger = self.charger
        if not charger:
            return None
        # Midnight on the 1st of the current calendar month, in the charger's local timezone.
        tz = resolve_timezone(charger.timezone)
        today_local = dt_util.now(tz)
        return today_local.replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )


class PodHomeTotalEnergySensor(PodHomeEntity, SensorEntity):
    """Running total energy delivered by this charger since Pod Home started tracking it - a
    monotonic counter, unlike Month energy above. Not a full account lifetime total; counts from
    whenever this charger was first seen by this install (total_started_at, exposed below).
    Unlike Month energy, a live session IS added on top: select_last_charge() (helpers.py) is
    used rather than a plain "current_charge is not None" check, since current_charge stays open
    past mobile-api's own endedAt - without that check the session would be double-counted
    between "mobile-api says finished" and "cable unplugged"."""

    _attr_translation_key = "total_energy"
    _attr_name = "Total energy"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_icon = "mdi:lightning-bolt-circle"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_total_energy"

    @property
    def native_value(self) -> float | None:
        charger = self.charger
        if not charger:
            return None
        total = charger.total_energy_kwh or 0.0
        current = charger.current_charge
        if (
            current
            and current.energy_total is not None
            and select_last_charge(current, charger.latest_charge) is current
        ):
            total += current.energy_total
        return total

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        if not charger or not charger.total_started_at:
            return None
        return {"tracking_started_at": charger.total_started_at.isoformat()}


class PodHomeElectricityRateSensor(PodHomeEntity, SensorEntity):
    """Current electricity rate, computed from the account's configured tariff windows."""

    _attr_translation_key = "electricity_rate"
    _attr_name = "Electricity rate"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_electricity_rate"

    @property
    def available(self) -> bool:
        # Smart-Charging-only - see smart_mode_available() (helpers.py) and DECISIONS.md.
        return super().available and smart_mode_available(self.charger.delegated_control_status)

    @property
    def native_value(self) -> float | None:
        window = self._current_window()
        return window.price if window else None

    @property
    def native_unit_of_measurement(self) -> str:
        return f"{self.coordinator.currency or DEFAULT_CURRENCY}/kWh"

    @property
    def icon(self) -> str:
        return currency_icon(self.coordinator.currency)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        charger = self.charger
        if not charger or not charger.tariff_windows:
            return None
        prices = [w.price for w in charger.tariff_windows if w.price is not None]
        window = self._current_window()
        return {"is_cheapest_rate": bool(window and prices and window.price == min(prices))}

    def _current_window(self) -> PodHomeTariffWindow | None:
        charger = self.charger
        if not charger or not charger.tariff_windows:
            return None
        tz = resolve_timezone(charger.timezone)
        now = dt_util.now(tz)
        today_name = now.strftime("%A").upper()
        yesterday_name = (now - datetime.timedelta(days=1)).strftime("%A").upper()
        current_time = now.time()
        for window in charger.tariff_windows:
            if not self._time_in_window(current_time, window.start, window.end):
                continue
            days = window.days or []
            # A wrapped window belongs to the day it started on: its after-midnight portion
            # (current_time < end) must match against yesterday's day, not today's, or it would
            # also wrongly match during the pre-start hours of its own start day.
            if self._wraps(window) and current_time < parse_time_of_day(window.end):
                if yesterday_name in days:
                    return window
            elif today_name in days:
                return window
        return None

    @classmethod
    def _wraps(cls, window: PodHomeTariffWindow) -> bool:
        start = parse_time_of_day(window.start)
        end = parse_time_of_day(window.end)
        return start is not None and end is not None and start > end

    @classmethod
    def _time_in_window(
        cls, current: datetime.time, start_str: str | None, end_str: str | None
    ) -> bool:
        start = parse_time_of_day(start_str)
        end = parse_time_of_day(end_str)
        if start is None or end is None:
            return False
        if start == end:
            return True  # a single window covering the whole day (e.g. a flat-rate tariff)
        if start < end:
            return start <= current < end
        return current >= start or current < end  # wraps past midnight


class PodHomeBoostEndTimeSensor(PodHomeEntity, SensorEntity):
    """When the current boost ("Charge Now" override) ends - see _current_boost_end_at()'s
    docstring (coordinator.py) for how "current" is decided. Not mode-gated; the override
    endpoint isn't tied to delegatedControl.status. Unknown (not unavailable) when no boost is
    running - native_value returning None is sufficient, no `available` override needed."""

    _attr_translation_key = "boost_end_time"
    _attr_name = "Boost end time"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:timer-plus-outline"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.ppid}_boost_end_time"

    @property
    def native_value(self) -> datetime.datetime | None:
        charger = self.charger
        return charger.boost_end_at if charger else None



class PodHomeVehicleBatteryLevelSensor(PodHomeVehicleEntity, SensorEntity):
    """Vehicle's reported battery level (via Enode, when a vehicle is linked)."""

    _attr_translation_key = "vehicle_battery_level"
    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_battery_level"

    @property
    def native_value(self) -> int | None:
        vehicle = self.vehicle
        return vehicle.battery_level_percent if vehicle else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        # When Enode last synced this vehicle's data, not when pod_home last polled it - can lag
        # behind by anywhere from ~30s to several minutes.
        vehicle = self.vehicle
        if not vehicle or not vehicle.synced_at:
            return None
        return {"synced_at": vehicle.synced_at.isoformat()}


class PodHomeVehicleRangeSensor(PodHomeVehicleEntity, SensorEntity):
    """Vehicle's estimated range (via Enode) - derived from battery level, not a live telemetry
    reading; only changes when battery level does. Native unit stays kilometres (Enode's own
    unit); suggested_unit_of_measurement switches the default display to miles per the account's
    own preferences.unitOfDistance setting."""

    _attr_translation_key = "vehicle_estimated_range"
    _attr_name = "Estimated range"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_range"

    @property
    def native_value(self) -> float | None:
        vehicle = self.vehicle
        return vehicle.range_km if vehicle else None

    @property
    def suggested_unit_of_measurement(self) -> str | None:
        return UnitOfLength.MILES if self.coordinator.unit_of_distance == "mi" else None


class PodHomeVehicleOdometerSensor(PodHomeVehicleEntity, SensorEntity):
    """Vehicle's odometer reading (via Enode, when a vehicle is linked). Same suggested-unit
    handling as Estimated range above - see that sensor's docstring."""

    _attr_translation_key = "vehicle_odometer"
    _attr_name = "Odometer"
    _attr_device_class = SensorDeviceClass.DISTANCE
    _attr_native_unit_of_measurement = UnitOfLength.KILOMETERS
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_odometer"

    @property
    def native_value(self) -> float | None:
        vehicle = self.vehicle
        return vehicle.odometer_km if vehicle else None

    @property
    def suggested_unit_of_measurement(self) -> str | None:
        return UnitOfLength.MILES if self.coordinator.unit_of_distance == "mi" else None


class PodHomeVehicleExpectedChargeSensor(PodHomeVehicleEntity, SensorEntity):
    """Smart Charging's live prediction for what % it'll actually deliver by Ready By - distinct
    from Target Charge (what you asked for); can diverge when a constraint like Charge Priority
    prevents hitting the target."""

    _attr_translation_key = "vehicle_expected_charge"
    _attr_name = "Expected charge"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_expected_charge"

    @property
    def available(self) -> bool:
        # Smart-Charging-only - see smart_mode_available() (helpers.py) and DECISIONS.md.
        charger = self._charger_for_vehicle()
        return super().available and smart_mode_available(
            charger.delegated_control_status if charger else None
        )

    @property
    def native_value(self) -> int | None:
        vehicle = self.vehicle
        return vehicle.expected_charge_percent if vehicle else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        vehicle = self.vehicle
        if not vehicle:
            return None
        return {
            "can_meet_target": vehicle.can_meet_target,
            "cannot_meet_target_reason": vehicle.cannot_meet_target_reason,
        }


class PodHomeVehiclePowerDeliveryStateSensor(PodHomeVehicleEntity, SensorEntity):
    """Raw vehicle.chargeState.powerDeliveryState - debug sensor backing Status's SuspendedEVSE
    handling (see charger_status() in helpers.py). Disabled by default."""

    _attr_translation_key = "vehicle_power_delivery_state"
    _attr_name = "Power delivery state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = POWER_DELIVERY_STATE_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_power_delivery_state"

    @property
    def native_value(self) -> str | None:
        vehicle = self.vehicle
        if not vehicle:
            return None
        return known_or_none(vehicle.power_delivery_state, self._attr_options)


class PodHomeVehicleChargeRateSensor(PodHomeVehicleEntity, SensorEntity):
    """Vehicle's charge rate (vehicle.chargeState.chargeRate). Null once charging stops."""

    _attr_translation_key = "vehicle_charge_rate"
    _attr_name = "Charge rate"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.KILO_WATT
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_charge_rate"

    @property
    def native_value(self) -> float | None:
        vehicle = self.vehicle
        return vehicle.charge_rate if vehicle else None


class PodHomeVehicleMaxCurrentSensor(PodHomeVehicleEntity, SensorEntity):
    """Raw vehicle.chargeState.maxCurrent. Unit unconfirmed (amps assumed) - always null on this
    account to date. Disabled by default."""

    _attr_translation_key = "vehicle_max_current"
    _attr_name = "Max current"
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_max_current"

    @property
    def native_value(self) -> float | None:
        vehicle = self.vehicle
        return vehicle.max_current if vehicle else None


class PodHomeVehicleChargeTimeRemainingSensor(PodHomeVehicleEntity, SensorEntity):
    """Raw vehicle.chargeState.chargeTimeRemaining. Unit unconfirmed (minutes assumed) - always
    null on this account to date. Native stays minutes; suggested display unit is hours."""

    _attr_translation_key = "vehicle_charge_time_remaining"
    _attr_name = "Charge time remaining"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_suggested_unit_of_measurement = UnitOfTime.HOURS

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.vehicle_id}_charge_time_remaining"

    @property
    def native_value(self) -> int | None:
        vehicle = self.vehicle
        return vehicle.charge_time_remaining if vehicle else None


class PodHomeRewardsBalanceSensor(PodHomeAccountEntity, SensorEntity):
    """Account-wide rewards balance, from GET /reward-wallet. Lives on its own "Pod Point"
    account device, not a charger (see PodHomeAccountEntity, entity.py). Always GBP-denominated,
    regardless of the account's billing currency - unlike the cost sensors, doesn't use
    coordinator.currency. balance_miles/balance_points and allowance/payout-threshold figures are
    exposed as attributes rather than separate entities."""

    _attr_translation_key = "rewards_balance"
    _attr_name = "Rewards balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "GBP"
    _attr_icon = "mdi:cash-plus"

    @property
    def unique_id(self) -> str:
        return f"{DOMAIN}_{self.coordinator.config_entry.entry_id}_rewards_balance"

    @property
    def available(self) -> bool:
        # coordinator.rewards is None until /reward-wallet succeeds at least once; unavailable
        # is more accurate than unknown-forever in that state.
        return super().available and self.coordinator.rewards is not None

    @property
    def native_value(self) -> float | None:
        rewards = self.coordinator.rewards
        return rewards.balance_gbp if rewards else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rewards = self.coordinator.rewards
        if not rewards:
            return None
        return {
            "balance_miles": rewards.balance_miles,
            "balance_points": rewards.balance_points,
            "allowance_balance_gbp": rewards.allowance_balance_gbp,
            "annual_allowance_gbp": rewards.annual_allowance_gbp,
            "payout_threshold_gbp": rewards.payout_threshold_gbp,
        }
