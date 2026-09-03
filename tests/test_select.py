"""Select entity tests, using pytest-homeassistant-custom-component's real `hass` fixture and
the shared factories in tests/_fixtures.py."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import custom_components.pod_home.select as select
from custom_components.pod_home.const import CHARGE_PRIORITY_COMPLETE_CHARGE, CHARGE_PRIORITY_LOWEST_COST
from tests._fixtures import make_charger, make_coordinator, make_tariff_window

PPID = "PSL-000001"
pytestmark = pytest.mark.asyncio


def _windows():
    return [make_tariff_window(price=0.10), make_tariff_window(price=0.30)]


async def test_current_option_reads_from_max_price(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass, {PPID: make_charger(max_price=0.10, tariff_windows=_windows())}
    )
    entity = select.PodHomeChargingStrategySelect(coordinator, PPID)
    assert entity.current_option == CHARGE_PRIORITY_LOWEST_COST


async def test_select_option_writes_max_price_and_refreshes(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(
        hass, {PPID: make_charger(max_price=None, tariff_windows=_windows())}
    )
    coordinator.async_request_refresh = AsyncMock()
    entity = select.PodHomeChargingStrategySelect(coordinator, PPID)

    await entity.async_select_option(CHARGE_PRIORITY_COMPLETE_CHARGE)

    coordinator.api.async_set_charge_priority_max_price.assert_called_once_with(PPID, 0.30)
    coordinator.async_request_refresh.assert_called_once()


async def test_select_option_without_tariff_data_raises(hass: HomeAssistant) -> None:
    coordinator = make_coordinator(hass, {PPID: make_charger(max_price=None, tariff_windows=None)})
    entity = select.PodHomeChargingStrategySelect(coordinator, PPID)
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option(CHARGE_PRIORITY_LOWEST_COST)
