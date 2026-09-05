"""Tests for Energa dashboard generator and button platform."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.energa_mobile.dashboard_generator import (
    build_meter_view,
    build_energa_dashboard,
    async_provision_dashboard,
    DEFAULT_URL_PATH,
    DEFAULT_TITLE,
)
from custom_components.energa_mobile.button import EnergaCreateDashboardButton


@pytest.fixture
def mock_meter_net_metering():
    return {
        "meter_point_id": "00069839",
        "meter_serial": "00069839",
        "address": "87-100 Toruń, Wiśniowa 9",
        "tariff": "G12w",
        "zone_count": 2,
        "is_prosumer": True,
        "has_export": True,
        "ppe": "590243891023106980",
        "total_plus": 46796.65,
    }


@pytest.fixture
def mock_meter_net_billing():
    return {
        "meter_point_id": "11685328",
        "meter_serial": "11685328",
        "address": "87-148 Łysomice, Agrestowa 4",
        "tariff": "G12w",
        "zone_count": 2,
        "is_prosumer": True,
        "has_export": True,
        "ppe": "590243891022973835",
        "total_plus": 5202.755,
    }


@pytest.fixture
def mock_meter_pure_consumer():
    return {
        "meter_point_id": "30910550",
        "meter_serial": "30910550",
        "address": "87-148 Łysomice, Warzywna 2",
        "tariff": "G11",
        "zone_count": 1,
        "is_prosumer": False,
        "has_export": False,
        "ppe": "590243891022987654",
        "total_plus": 1823.4,
    }


def test_build_meter_view_net_metering(mock_meter_net_metering):
    view = build_meter_view(mock_meter_net_metering, coeff=0.8)
    assert view["title"] == "Wiśniowa 9"
    assert view["path"] == "licznik-00069839"
    assert view["icon"] == "mdi:battery-charging-high"

    # Badges
    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.energa_00069839_bank_wirtualny_kwh" in badge_entities
    assert "sensor.energa_00069839_magazyn_poziom" in badge_entities
    assert "sensor.energa_00069839_dotychczasowy_rachunek" in badge_entities
    assert "sensor.energa_00069839_prognoza_rachunku" in badge_entities

    # Cards
    card_titles = [c["title"] for c in view["cards"]]
    assert any("Net-Metering" in t for t in card_titles)
    assert any("Rozliczenie Finansowe" in t for t in card_titles)
    assert any("Taryfa G12w" in t for t in card_titles)
    assert any("Rejestry Licznika Fizycznego" in t for t in card_titles)

    # Specific entities in storage card
    storage_card = next(c for c in view["cards"] if "Net-Metering" in c["title"])
    storage_entities = [e["entity"] for e in storage_card["entities"]]
    assert "sensor.energa_00069839_pokrycie_z_magazynu_dzien_mtd" in storage_entities
    assert "sensor.energa_00069839_pokrycie_z_magazynu_noc_mtd" in storage_entities


def test_build_meter_view_net_billing(mock_meter_net_billing):
    view = build_meter_view(mock_meter_net_billing, coeff=0.0)
    assert view["title"] == "Agrestowa 4"
    assert view["path"] == "licznik-11685328"
    assert view["icon"] == "mdi:home-lightning-bolt"

    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.energa_11685328_bank_wirtualny_pln" in badge_entities
    assert "sensor.energa_11685328_cena_oddania" in badge_entities

    card_titles = [c["title"] for c in view["cards"]]
    assert any("Depozyt Prosumencki" in t for t in card_titles)

    storage_card = next(c for c in view["cards"] if "Depozyt Prosumencki" in c["title"])
    storage_entities = [e["entity"] for e in storage_card["entities"]]
    assert "sensor.energa_11685328_odzyskano_z_depozytu_mtd" in storage_entities
    assert "sensor.energa_11685328_rcem_auto" in storage_entities


def test_build_meter_view_pure_consumer(mock_meter_pure_consumer):
    view = build_meter_view(mock_meter_pure_consumer, coeff=0.0)
    assert view["title"] == "Warzywna 2"
    assert view["icon"] == "mdi:transmission-tower"

    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.energa_30910550_taryfa" in badge_entities

    card_titles = [c["title"] for c in view["cards"]]
    # Should NOT have prosumer storage card
    assert not any("Wirtualny Magazyn" in t for t in card_titles)
    assert any("Rozliczenie Finansowe" in t for t in card_titles)


def test_build_energa_dashboard_multi_meter(mock_meter_net_metering, mock_meter_net_billing):
    dash = build_energa_dashboard([mock_meter_net_metering, mock_meter_net_billing], coeff=0.8)
    assert dash["title"] == DEFAULT_TITLE
    assert len(dash["views"]) == 2
    assert dash["views"][0]["path"] == "glowny"
    assert dash["views"][1]["path"] == "licznik-11685328"


def test_button_entity_properties(mock_meter_net_metering):
    mock_entry = MagicMock()
    mock_entry.options = {}
    mock_hass = MagicMock()

    btn = EnergaCreateDashboardButton(
        hass=mock_hass,
        entry=mock_entry,
        meter=mock_meter_net_metering,
        all_meters=[mock_meter_net_metering],
    )

    assert btn._attr_has_entity_name is True
    assert btn._attr_name == "Utwórz Pulpit Rozliczeń"
    assert btn._attr_icon == "mdi:view-dashboard-outline"
    assert btn._attr_unique_id == "energa_00069839_create_dashboard"
    assert btn._attr_device_info is not None


@pytest.mark.asyncio
async def test_button_async_press(mock_meter_net_metering):
    mock_entry = MagicMock()
    mock_entry.options = {"prosumer_coefficient": 0.8}
    mock_hass = MagicMock()

    btn = EnergaCreateDashboardButton(
        hass=mock_hass,
        entry=mock_entry,
        meter=mock_meter_net_metering,
        all_meters=[mock_meter_net_metering],
    )

    with patch(
        "custom_components.energa_mobile.button.async_provision_dashboard",
        new=AsyncMock(return_value=True),
    ) as mock_prov:
        await btn.async_press()
        mock_prov.assert_called_once_with(
            mock_hass,
            [mock_meter_net_metering],
            url_path=DEFAULT_URL_PATH,
            title=DEFAULT_TITLE,
            icon="mdi:currency-pln",
            coeff=0.8,
        )


@pytest.mark.asyncio
async def test_async_provision_dashboard_storage(mock_meter_net_metering):
    mock_hass = MagicMock()
    mock_hass.config.components = ["frontend"]
    mock_hass.data = {}

    with patch("custom_components.energa_mobile.dashboard_generator.storage.Store") as mock_store_cls, \
         patch("homeassistant.components.frontend.async_panel_exists", return_value=False), \
         patch("homeassistant.components.frontend.async_register_built_in_panel") as mock_reg_panel:
        
        mock_store_inst = MagicMock()
        mock_store_inst.async_load = AsyncMock(return_value={"items": []})
        mock_store_inst.async_save = AsyncMock()
        mock_store_cls.return_value = mock_store_inst

        success = await async_provision_dashboard(
            mock_hass,
            [mock_meter_net_metering],
            url_path="energa-rachunek",
            title="Energa Rozliczenia",
            coeff=0.8,
        )

        assert success is True
        # Check storage save called
        assert mock_store_inst.async_save.call_count >= 2
        mock_reg_panel.assert_called_once()
