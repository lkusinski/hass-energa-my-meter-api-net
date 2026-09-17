"""Tests for Energa dashboard generator and button platform."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.energa_mobile.button import EnergaCreateDashboardButton
from custom_components.energa_mobile.dashboard_generator import (
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
    build_energa_dashboard,
    build_meter_view,
    resolve_entity,
)


@pytest.fixture
def mock_meter_net_metering():
    return {
        "meter_point_id": "10000002",
        "meter_serial": "10000002",
        "address": "Przykładowa 1",
        "tariff": "G12w",
        "zone_count": 2,
        "is_prosumer": True,
        "has_export": True,
        "ppe": "590000000000000001",
        "total_plus": 46796.65,
    }


@pytest.fixture
def mock_meter_net_billing():
    return {
        "meter_point_id": "10000001",
        "meter_serial": "10000001",
        "address": "Przykładowa 2",
        "tariff": "G12w",
        "zone_count": 2,
        "is_prosumer": True,
        "has_export": True,
        "ppe": "590000000000000002",
        "total_plus": 5202.755,
    }


@pytest.fixture
def mock_meter_pure_consumer():
    return {
        "meter_point_id": "10000004",
        "meter_serial": "10000004",
        "address": "Przykładowa 3",
        "tariff": "G11",
        "zone_count": 1,
        "is_prosumer": False,
        "has_export": False,
        "ppe": "590000000000000003",
        "total_plus": 1823.4,
    }


def test_build_meter_view_net_metering(mock_meter_net_metering):
    view = build_meter_view(mock_meter_net_metering, coeff=0.8)
    assert view["title"] == "G12w — Wirtualny Magazyn (Net-metering)"
    assert view["path"] == "licznik-10000002"
    assert view["icon"] == "mdi:battery-charging-high"

    # Badges
    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.energa_10000002_bank_wirtualny_kwh" in badge_entities
    assert "sensor.energa_10000002_magazyn_poziom" in badge_entities
    assert "sensor.energa_10000002_dotychczasowy_rachunek" in badge_entities
    assert "sensor.energa_10000002_prognoza_rachunku" in badge_entities

    # Cards
    card_titles = [c["title"] for c in view["cards"] if "title" in c]
    assert any("Net-Metering" in t for t in card_titles)
    assert any("Rozliczenie Finansowe" in t for t in card_titles)
    assert any("Taryfa G12w" in t for t in card_titles)
    assert any("Rejestry Licznika" in t for t in card_titles)

    # Specific entities in storage card
    storage_card = next(c for c in view["cards"] if "Net-Metering" in c.get("title", ""))
    storage_entities = [e["entity"] for e in storage_card["entities"]]
    assert "sensor.energa_10000002_bank_wirtualny_l1_dzien_kwh" in storage_entities
    assert "sensor.energa_10000002_bank_wirtualny_l2_noc_kwh" in storage_entities
    assert "sensor.energa_10000002_pokrycie_z_magazynu_dzien_mtd" in storage_entities
    assert "sensor.energa_10000002_pokrycie_z_magazynu_noc_mtd" in storage_entities
    assert not any("bank_ladowanie" in e for e in storage_entities)
    assert not any("bank_rozladowanie" in e for e in storage_entities)

    # Card 4: Dedicated MTD volume sensors (no panel_energia_* entities)
    tariff_card = next(c for c in view["cards"] if "Taryfa G12w" in c.get("title", ""))
    tariff_entities = [e["entity"] for e in tariff_card["entities"]]
    assert "sensor.energa_10000002_pobor_energii_strefa_1_mtd" in tariff_entities
    assert "sensor.energa_10000002_pobor_energii_strefa_2_mtd" in tariff_entities
    assert "sensor.energa_10000002_oddanie_energii_strefa_1_mtd" in tariff_entities
    assert "sensor.energa_10000002_oddanie_energii_strefa_2_mtd" in tariff_entities
    assert not any("panel_energia" in e for e in tariff_entities)


def test_build_meter_view_net_billing(mock_meter_net_billing):
    view = build_meter_view(mock_meter_net_billing, coeff=0.0)
    assert view["title"] == "G12w — Depozyt Prosumencki (Net-billing)"
    assert view["path"] == "licznik-10000001"
    assert view["icon"] == "mdi:home-lightning-bolt"

    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.energa_10000001_bank_wirtualny_pln" in badge_entities
    assert "sensor.energa_10000001_cena_oddania" in badge_entities

    card_titles = [c["title"] for c in view["cards"] if "title" in c]
    assert any("Depozyt Prosumencki" in t for t in card_titles)

    storage_card = next(c for c in view["cards"] if "Depozyt Prosumencki" in c.get("title", ""))
    storage_entities = [e["entity"] for e in storage_card["entities"]]
    assert "sensor.energa_10000001_odzyskano_z_depozytu_mtd" in storage_entities
    assert "sensor.energa_10000001_rcem_auto" in storage_entities


def test_build_meter_view_pure_consumer(mock_meter_pure_consumer):
    view = build_meter_view(mock_meter_pure_consumer, coeff=0.0)
    assert view["title"] == "G11 — Profil Konsumencki"
    assert view["icon"] == "mdi:transmission-tower"

    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.energa_10000004_taryfa" in badge_entities

    card_titles = [c["title"] for c in view["cards"] if "title" in c]
    # Should NOT have prosumer storage card
    assert not any("Wirtualny Magazyn" in t for t in card_titles)
    assert any("Rozliczenie Finansowe" in t for t in card_titles)
    tariff_card = next(c for c in view["cards"] if "Taryfa G11" in c.get("title", ""))
    tariff_entities = [e["entity"] for e in tariff_card["entities"]]
    assert "sensor.energa_10000004_pobor_energii_mtd" in tariff_entities
    assert not any("panel_energia" in e for e in tariff_entities)



def test_build_meter_view_net_billing_g11():
    meter = {
        "meter_point_id": "10000003",
        "meter_serial": "10000003",
        "tariff": "G11",
        "zone_count": 1,
        "is_prosumer": True,
        "has_export": True,
        "ppe": "590000000000000004",
    }
    view = build_meter_view(meter, coeff=0.0)
    assert view["title"] == "G11 — Depozyt Prosumencki (Net-billing)"
    assert view["icon"] == "mdi:home-lightning-bolt"


def test_build_energa_dashboard_multi_meter(mock_meter_net_metering, mock_meter_net_billing):
    dash = build_energa_dashboard([mock_meter_net_metering, mock_meter_net_billing], coeff=0.8)
    assert dash["title"] == DEFAULT_TITLE
    assert len(dash["views"]) == 2
    assert dash["views"][0]["path"] == "glowny"
    assert dash["views"][1]["path"] == "licznik-10000001"


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
    assert btn._attr_unique_id == "energa_10000002_create_dashboard"
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
            icon="mdi:lightning-bolt-circle",
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


def test_agrestowa_style_dashboard_structure(mock_meter_net_billing):
    mock_meter_net_billing["customer_label"] = "Agrestowa 4"
    view = build_meter_view(mock_meter_net_billing, coeff=0.0)

    # 1. Header card (no duplicate title, clean H3 markdown)
    header_card = view["cards"][0]
    assert header_card["type"] == "markdown"
    assert "title" not in header_card
    assert "Agrestowa 4" in header_card["content"]
    assert "Net-billing" in header_card["content"]
    assert "10000001" in header_card["content"]

    # 2. Badges in Agrestowa style
    badge_names = {b["name"]: b["entity"] for b in view["badges"]}
    assert "Dotychczas brutto" in badge_names
    assert "Prognoza brutto" in badge_names
    assert "Magazyn/Depozyt" in badge_names

    # 3. Financial card title (clean, non-duplicated)
    financial_card = view["cards"][1]
    assert financial_card["title"] == "⚡ Rozliczenie Finansowe"

    # 4. Dynamic RCE & Arbitrage card
    rce_card = view["cards"][-1]
    assert "Ceny Dynamiczne RCE" in rce_card["title"]


def test_verification_section_added_in_order(mock_meter_net_billing):
    verification_ids = {
        "date.energa_10000001_okres_start",
        "date.energa_10000001_okres_koniec",
        "button.energa_10000001_przelicz_okres",
        "sensor.energa_10000001_weryfikacja_rachunku",
    }
    mock_hass = MagicMock()
    mock_hass.states.get.side_effect = (
        lambda eid: MagicMock() if eid in verification_ids else None
    )

    view = build_meter_view(mock_meter_net_billing, coeff=0.0, hass=mock_hass)
    section = next(
        c for c in view["cards"] if c.get("title") == "Sprawdzenie rachunku"
    )
    assert section["type"] == "entities"
    entities = section["entities"]
    assert [e["entity"] for e in entities] == [
        "date.energa_10000001_okres_start",
        "date.energa_10000001_okres_koniec",
        "button.energa_10000001_przelicz_okres",
        "sensor.energa_10000001_weryfikacja_rachunku",
    ]
    assert [e["name"] for e in entities] == [
        "Okres Start",
        "Okres Koniec",
        "Przelicz okres rozliczeniowy",
        "Okres: rozliczenie",
    ]


def test_verification_section_skipped_without_entities(mock_meter_pure_consumer):
    mock_hass = MagicMock()
    mock_hass.states.get.return_value = None

    view = build_meter_view(mock_meter_pure_consumer, coeff=0.0, hass=mock_hass)
    titles = [c.get("title") for c in view["cards"]]
    assert "Sprawdzenie rachunku" not in titles


def test_verification_section_skipped_when_hass_is_none(mock_meter_net_billing):
    view = build_meter_view(mock_meter_net_billing, coeff=0.0, hass=None)
    titles = [c.get("title") for c in view["cards"]]
    assert "Sprawdzenie rachunku" not in titles


def test_resolve_entity():
    # When hass is None, returns primary
    assert resolve_entity(None, "sensor.energa_123_test", ["sensor.licznik_123_test"]) == "sensor.energa_123_test"

    # When hass is present with states
    mock_hass = MagicMock()
    mock_hass.states.get.side_effect = lambda eid: MagicMock() if eid == "sensor.licznik_123_test" else None

    resolved = resolve_entity(mock_hass, "sensor.energa_123_test", ["sensor.licznik_123_test"])
    assert resolved == "sensor.licznik_123_test"


def test_meter_view_dynamic_resolution_with_hass(mock_meter_net_billing):
    mock_hass = MagicMock()
    # Simulate legacy or Licznik naming present in hass.states
    def _mock_states_get(eid):
        if "licznik_10000001" in eid:
            return MagicMock()
        return None

    mock_hass.states.get.side_effect = _mock_states_get

    view = build_meter_view(mock_meter_net_billing, coeff=0.0, hass=mock_hass)
    badge_entities = [b["entity"] for b in view["badges"]]
    assert "sensor.licznik_10000001_bank_wirtualny_pln" in badge_entities
    assert "sensor.licznik_10000001_dotychczasowy_rachunek" in badge_entities


def test_autoconsumption_not_added_without_inverter_or_state(mock_meter_net_billing):
    # Pure net billing without inverter entity
    view = build_meter_view(mock_meter_net_billing, coeff=0.0, hass=None)
    tariff_card = next(c for c in view["cards"] if "Taryfa" in c.get("title", ""))
    tariff_entities = [e["entity"] for e in tariff_card["entities"]]
    assert not any("autokonsumpcja" in e for e in tariff_entities)


def test_meter_view_with_custom_device_name_and_area():
    """Test resolution when user puts meter in Area 'wejscie' and renames device to 'licznik energa' (Issue #2)."""
    meter = {
        "meter_point_id": "55555555",
        "meter_serial": "55555555",
        "address": "ul. Testowa 1",
        "tariff": "G12w",
        "zone_count": 2,
        "is_prosumer": True,
        "has_export": True,
        "ppe": "590000000000000099",
        "total_plus": 1000.0,
    }

    class MockEntry:
        def __init__(self, entity_id, unique_id="", original_name="", domain="sensor"):
            self.entity_id = entity_id
            self.unique_id = unique_id
            self.original_name = original_name
            self.domain = domain
            self.platform = "energa_mobile"

    device_id = "mock_device_55555555"
    mock_device = MagicMock()
    mock_device.id = device_id

    mock_entries = [
        MockEntry(
            "sensor.wejscie_licznik_energa_numer_licznika",
            "energa_55555555_numer_licznika_info",
            "Numer Licznika",
        ),
        MockEntry(
            "sensor.wejscie_licznik_energa_dotychczasowy_rachunek",
            "energa_55555555_bill_current",
            "Dotychczasowy Rachunek",
        ),
        MockEntry(
            "sensor.wejscie_licznik_energa_prognoza_rachunku",
            "energa_55555555_bill_forecast",
            "Prognoza Rachunku",
        ),
        MockEntry(
            "sensor.wejscie_licznik_energa_stan_licznika_import",
            "energa_55555555_total_plus_live",
            "Stan Licznika Import",
        ),
        MockEntry(
            "sensor.wejscie_licznik_energa_bank_wirtualny_kwh",
            "energa_55555555_bank_kwh",
            "Bank Wirtualny kWh",
        ),
        MockEntry(
            "sensor.wejscie_licznik_energa_bank_wirtualny_pln",
            "energa_55555555_bank_pln",
            "Bank Wirtualny PLN",
        ),
        MockEntry(
            "sensor.wejscie_licznik_energa_magazyn_poziom",
            "energa_55555555_bank_level",
            "Magazyn Poziom",
        ),
        MockEntry(
            "binary_sensor.wejscie_licznik_energa_okno_ladowania_bess_arbitraz_rce",
            "energa_55555555_bess_charge_window",
            "Okno ładowania BESS (Arbitraż RCE)",
            domain="binary_sensor",
        ),
    ]

    import sys

    dr_mod = sys.modules["homeassistant.helpers"].device_registry
    er_mod = sys.modules["homeassistant.helpers"].entity_registry

    mock_dev_reg = MagicMock()
    mock_dev_reg.async_get_devices.return_value = [mock_device]
    mock_dev_reg.async_get_device.return_value = mock_device

    mock_ent_reg = MagicMock()

    mock_hass = MagicMock()
    with patch.object(dr_mod, "async_get", return_value=mock_dev_reg), \
         patch.object(er_mod, "async_get", return_value=mock_ent_reg), \
         patch.object(er_mod, "async_entries_for_device", return_value=mock_entries):

        view = build_meter_view(meter, coeff=0.8, hass=mock_hass)

        # Badges should resolve to sensor.wejscie_licznik_energa_*
        badge_entities = [b["entity"] for b in view["badges"]]
        assert "sensor.wejscie_licznik_energa_dotychczasowy_rachunek" in badge_entities
        assert "sensor.wejscie_licznik_energa_prognoza_rachunku" in badge_entities
        assert "sensor.wejscie_licznik_energa_bank_wirtualny_kwh" in badge_entities
        assert "sensor.wejscie_licznik_energa_magazyn_poziom" in badge_entities

        # Rejestry licznika card
        counter_card = next(c for c in view["cards"] if "Rejestry Licznika" in c.get("title", ""))
        counter_entities = [e["entity"] for e in counter_card["entities"]]
        assert "sensor.wejscie_licznik_energa_numer_licznika" in counter_entities
        assert "sensor.wejscie_licznik_energa_stan_licznika_import" in counter_entities


def test_resolve_entity_with_entity_map():
    entity_map = {
        ("sensor", "numer_licznika"): "sensor.wejscie_licznik_energa_numer_licznika",
        "numer_licznika": "sensor.wejscie_licznik_energa_numer_licznika",
    }
    # Direct lookup with entity_map
    res = resolve_entity(
        None,
        "sensor.energa_55555555_numer_licznika",
        ["sensor.licznik_55555555_numer_licznika"],
        entity_map=entity_map,
        name_suffix="numer_licznika",
    )
    assert res == "sensor.wejscie_licznik_energa_numer_licznika"


def test_resolve_entity_suffix_fallback_states():
    mock_hass = MagicMock()
    mock_hass.states.get.return_value = None
    mock_st = MagicMock()
    mock_st.entity_id = "sensor.kotlownia_moj_licznik_numer_licznika"
    mock_hass.states.async_all.return_value = [mock_st]

    res = resolve_entity(
        mock_hass,
        "sensor.energa_999_numer_licznika",
        ["sensor.licznik_999_numer_licznika"],
        name_suffix="numer_licznika",
    )
    assert res == "sensor.kotlownia_moj_licznik_numer_licznika"


def test_meter_view_with_custom_device_name_and_area_net_billing():
    """Test net-billing with binary sensors under custom device and area."""
    import sys

    meter = {
        "meter_point_id": "55555555",
        "meter_serial": "55555555",
        "tariff": "G12w",
        "zone_count": 2,
        "is_prosumer": True,
        "has_export": True,
        "prosumer_coefficient": 0.0,
    }

    class MockEntry:
        def __init__(self, entity_id, unique_id="", original_name="", domain="sensor"):
            self.entity_id = entity_id
            self.unique_id = unique_id
            self.original_name = original_name
            self.domain = domain
            self.platform = "energa_mobile"

    mock_entries = [
        MockEntry("sensor.wejscie_licznik_energa_bank_wirtualny_pln", "energa_55555555_bank_pln", "Bank Wirtualny PLN"),
        MockEntry("sensor.wejscie_licznik_energa_cena_oddania", "energa_55555555_feed_in_price", "Cena Oddania"),
        MockEntry("sensor.wejscie_licznik_energa_rcem_auto", "energa_55555555_rcem_auto", "RCEM Auto"),
        MockEntry("sensor.wejscie_licznik_energa_dynamiczna_cena_energii_rce", "energa_55555555_rce_dynamic_price", "Dynamiczna Cena Energii RCE"),
        MockEntry("sensor.wejscie_licznik_energa_spread_arbitrazowy_bess_rce", "energa_55555555_bess_arbitrage_spread", "Spread arbitrażowy BESS (RCE)"),
        MockEntry("binary_sensor.wejscie_licznik_energa_okno_ladowania_bess_arbitraz_rce", "energa_55555555_bess_charge_window", "Okno ładowania BESS (Arbitraż RCE)", domain="binary_sensor"),
        MockEntry("binary_sensor.wejscie_licznik_energa_okno_rozladowania_bess_szczyt_rce", "energa_55555555_bess_discharge_window", "Okno rozładowania BESS (Szczyt RCE)", domain="binary_sensor"),
        MockEntry("binary_sensor.wejscie_licznik_energa_cena_ujemna_rce_zagrozenie_eksportu", "energa_55555555_rce_negative_price", "Cena ujemna RCE (Zagrożenie eksportu)", domain="binary_sensor"),
    ]

    mock_device = MagicMock()
    mock_device.id = "mock_device_55555555"

    dr_mod = sys.modules["homeassistant.helpers"].device_registry
    er_mod = sys.modules["homeassistant.helpers"].entity_registry

    mock_dev_reg = MagicMock()
    mock_dev_reg.async_get_devices.return_value = [mock_device]
    mock_dev_reg.async_get_device.return_value = mock_device
    mock_ent_reg = MagicMock()

    mock_hass = MagicMock()
    with patch.object(dr_mod, "async_get", return_value=mock_dev_reg), \
         patch.object(er_mod, "async_get", return_value=mock_ent_reg), \
         patch.object(er_mod, "async_entries_for_device", return_value=mock_entries):

        view = build_meter_view(meter, coeff=0.0, hass=mock_hass)

        # RCE & Arbitrage card
        rce_card = next(c for c in view["cards"] if "Ceny Dynamiczne RCE" in c.get("title", ""))
        rce_entities = [e["entity"] for e in rce_card["entities"]]
        assert "binary_sensor.wejscie_licznik_energa_okno_ladowania_bess_arbitraz_rce" in rce_entities
        assert "binary_sensor.wejscie_licznik_energa_okno_rozladowania_bess_szczyt_rce" in rce_entities
        assert "binary_sensor.wejscie_licznik_energa_cena_ujemna_rce_zagrozenie_eksportu" in rce_entities
        assert "sensor.wejscie_licznik_energa_spread_arbitrazowy_bess_rce" in rce_entities


def test_multi_meter_registry_isolation():
    """Ensure two meters with custom device names do not mix up entities."""
    import sys

    meter1 = {"meter_point_id": "11111111", "meter_serial": "11111111", "tariff": "G11", "zone_count": 1}
    meter2 = {"meter_point_id": "22222222", "meter_serial": "22222222", "tariff": "G11", "zone_count": 1}

    class MockEntry:
        def __init__(self, entity_id, unique_id="", original_name="", domain="sensor"):
            self.entity_id = entity_id
            self.unique_id = unique_id
            self.original_name = original_name
            self.domain = domain
            self.platform = "energa_mobile"

    entries_meter1 = [
        MockEntry("sensor.kuchnia_licznik_kuchnia_numer_licznika", "energa_11111111_numer_licznika_info", "Numer Licznika"),
        MockEntry("sensor.kuchnia_licznik_kuchnia_taryfa", "energa_11111111_taryfa_info", "Taryfa"),
    ]
    entries_meter2 = [
        MockEntry("sensor.garaz_licznik_garaz_numer_licznika", "energa_22222222_numer_licznika_info", "Numer Licznika"),
        MockEntry("sensor.garaz_licznik_garaz_taryfa", "energa_22222222_taryfa_info", "Taryfa"),
    ]

    dev1 = MagicMock(id="dev_11111111")
    dev2 = MagicMock(id="dev_22222222")

    dr_mod = sys.modules["homeassistant.helpers"].device_registry
    er_mod = sys.modules["homeassistant.helpers"].entity_registry

    mock_dev_reg = MagicMock()
    mock_dev_reg.async_get_devices.side_effect = (
        lambda identifiers: [dev1] if ("energa_mobile", "11111111") in identifiers else [dev2]
    )
    mock_dev_reg.async_get_device.side_effect = lambda identifiers: dev1 if ("energa_mobile", "11111111") in identifiers else dev2

    mock_ent_reg = MagicMock()
    def _mock_entries_for_device(reg, device_id):
        if device_id == "dev_11111111":
            return entries_meter1
        return entries_meter2

    mock_hass = MagicMock()
    with patch.object(dr_mod, "async_get", return_value=mock_dev_reg), \
         patch.object(er_mod, "async_get", return_value=mock_ent_reg), \
         patch.object(er_mod, "async_entries_for_device", side_effect=_mock_entries_for_device):

        view1 = build_meter_view(meter1, hass=mock_hass)
        view2 = build_meter_view(meter2, hass=mock_hass)

        view1_badges = [b["entity"] for b in view1["badges"]]
        view2_badges = [b["entity"] for b in view2["badges"]]

        assert "sensor.kuchnia_licznik_kuchnia_taryfa" in view1_badges
        assert "sensor.garaz_licznik_garaz_taryfa" in view2_badges
        assert "sensor.garaz_licznik_garaz_taryfa" not in view1_badges
        assert "sensor.kuchnia_licznik_kuchnia_taryfa" not in view2_badges




