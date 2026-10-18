"""Diagnostics tests - the temporary month_total_check section's month bucketing."""
from __future__ import annotations

import datetime

import pytest
from freezegun import freeze_time
from homeassistant.core import HomeAssistant

from custom_components.pod_home.diagnostics import async_get_config_entry_diagnostics
from tests._fixtures import make_charger, make_coordinator

pytestmark = pytest.mark.asyncio

PPID = "PSL-000001"


@freeze_time("2026-10-18T12:00:00Z")
async def test_month_total_check_buckets_sessions_by_local_start(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(timezone="Europe/London")})
    coordinator.api.async_charges.return_value = {
        "data": {
            "charges": [
                # 00:30 BST on 1 October - still 30 September in UTC.
                {"startedAt": "2026-09-30T23:30:00Z", "endedAt": "2026-10-01T04:00:00Z",
                 "energyTotal": 10, "cost": {"amount": 100}, "charger": {"id": PPID}},
                {"startedAt": "2026-09-23T19:25:34Z", "endedAt": "2026-09-24T05:17:35Z",
                 "energyTotal": 64, "cost": {"amount": 1173}, "charger": {"id": PPID}},
                {"startedAt": "2026-08-31T19:50:00Z", "endedAt": "2026-09-01T05:35:00Z",
                 "energyTotal": 8.1, "cost": {"amount": 71}, "charger": {"id": PPID}},
                {"startedAt": "2026-09-10T20:00:00Z", "endedAt": "2026-09-11T05:00:00Z",
                 "energyTotal": 5, "cost": {"amount": 50}, "charger": {"id": "PSL-OTHER"}},
            ]
        }
    }
    coordinator.api.async_charge_statistics.return_value = {
        "energy": {"totalUsage": 1.0, "cost": 2}
    }
    coordinator.config_entry.runtime_data = coordinator

    diagnostics = await async_get_config_entry_diagnostics(hass, coordinator.config_entry)

    months = diagnostics["month_total_check"][PPID]
    assert list(months) == ["2026-10", "2026-09", "2026-08"]
    assert months["2026-10"]["summed_from_charges"] == {"energy_kwh": 10, "cost": 100}
    assert months["2026-09"]["summed_from_charges"] == {"energy_kwh": 64, "cost": 1173}
    assert months["2026-08"]["summed_from_charges"] == {"energy_kwh": 8.1, "cost": 71}
    assert months["2026-10"]["charge_statistics"] == {"energy_kwh": 1.0, "cost": 2}
    coordinator.api.async_charges.assert_awaited_once_with(
        datetime.date(2026, 7, 31), datetime.date(2026, 10, 18)
    )
    statistics_ranges = [c.args[1:] for c in coordinator.api.async_charge_statistics.await_args_list]
    assert statistics_ranges == [
        (datetime.date(2026, 10, 1), datetime.date(2026, 10, 18)),
        (datetime.date(2026, 9, 1), datetime.date(2026, 9, 30)),
        (datetime.date(2026, 8, 1), datetime.date(2026, 8, 31)),
    ]
