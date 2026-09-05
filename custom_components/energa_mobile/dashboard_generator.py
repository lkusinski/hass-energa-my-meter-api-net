"""Lovelace Dashboard Generator for Energa My Meter integration.

Provides on-demand provisioning of tailored Lovelace dashboards for
Energa meters (net-metering G12w, net-billing G12w/G11, pure consumer).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage

from .const import (
    CONF_PROSUMER_COEFFICIENT,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL_PATH = "energa-rachunek"
DEFAULT_TITLE = "Energa Rozliczenia"
DEFAULT_ICON = "mdi:currency-pln"


def is_export_prosumer(meter: dict[str, Any]) -> bool:
    """Check if meter has export (production/prosumer) configured."""
    return bool(meter.get("is_prosumer") or meter.get("has_export"))


def build_meter_view(meter: dict[str, Any], coeff: float = 0.8) -> dict[str, Any]:
    """Build a tailored Lovelace view for a specific Energa meter."""
    meter_id = str(meter.get("meter_point_id", ""))
    serial = str(meter.get("meter_serial", meter_id))
    tariff_raw = str(meter.get("tariff", "G11")).strip()
    if tariff_raw.upper() == "G12W":
        tariff = "G12w"
    elif tariff_raw.upper() == "G12R":
        tariff = "G12r"
    else:
        tariff = tariff_raw.upper()
    has_zones = meter.get("zone_count", 1) > 1
    is_prosumer = is_export_prosumer(meter)
    is_net_billing = is_prosumer and coeff < 0.7
    is_net_metering = is_prosumer and not is_net_billing

    if meter.get("customer_label"):
        title = str(meter["customer_label"]).strip()
    elif meter.get("custom_title"):
        title = str(meter["custom_title"]).strip()
    elif is_net_metering:
        title = f"{tariff} — Wirtualny Magazyn (Net-metering)"
    elif is_net_billing:
        title = f"{tariff} — Depozyt Prosumencki (Net-billing)"
    elif is_prosumer:
        title = f"{tariff} — Fotowoltaika"
    else:
        title = f"{tariff} — Profil Konsumencki"

    view_icon = (
        "mdi:battery-charging-high"
        if is_net_metering
        else "mdi:home-lightning-bolt"
        if is_net_billing
        else "mdi:transmission-tower"
    )

    # 1. Badges
    badges = [
        {
            "entity": f"sensor.energa_{serial}_dotychczasowy_rachunek",
            "name": "Dotychczas do zapłaty",
        },
        {
            "entity": f"sensor.energa_{serial}_prognoza_rachunku",
            "name": "Prognoza miesiąca",
        },
    ]

    if is_net_metering:
        badges.insert(
            0,
            {
                "entity": f"sensor.energa_{serial}_bank_wirtualny_kwh",
                "name": "Dostępny Magazyn",
            },
        )
        badges.insert(
            1,
            {
                "entity": f"sensor.energa_{serial}_magazyn_poziom",
                "name": "Poziom Magazynu",
            },
        )
    elif is_net_billing:
        badges.append(
            {
                "entity": f"sensor.energa_{serial}_bank_wirtualny_pln",
                "name": "Depozyt PLN",
            }
        )
        badges.append(
            {
                "entity": f"sensor.energa_{serial}_cena_oddania",
                "name": "Wycena oddania",
            }
        )
    else:
        badges.append(
            {
                "entity": f"sensor.energa_{serial}_taryfa",
                "name": "Taryfa",
            }
        )

    cards: list[dict[str, Any]] = []

    # 2. Card: Storage / Deposit (if prosumer)
    if is_net_metering:
        storage_entities = [
            {"entity": f"sensor.energa_{serial}_magazyn_poziom", "name": "Poziom napełnienia magazynu"},
            {"entity": f"sensor.energa_{serial}_bank_wirtualny_kwh", "name": "Dostępne saldo w magazynie"},
        ]
        if has_zones:
            storage_entities.append(
                {"entity": f"sensor.energa_{serial}_pokrycie_z_magazynu_dzien_mtd", "name": "Pobranie z magazynu (Dzień T1)"}
            )
            storage_entities.append(
                {"entity": f"sensor.energa_{serial}_pokrycie_z_magazynu_noc_mtd", "name": "Pobranie z magazynu (Noc T2)"}
            )
        storage_entities.append(
            {"entity": f"sensor.energa_{serial}_wspolczynnik_prosumencki", "name": "Współczynnik opustu"}
        )

        cards.append(
            {
                "type": "entities",
                "title": "🔋 Wirtualny Magazyn Energii (Net-Metering)",
                "icon": "mdi:battery-charging-high",
                "entities": storage_entities,
            }
        )
    elif is_net_billing:
        cards.append(
            {
                "type": "entities",
                "title": "🔋 Wirtualny Magazyn Energii (Depozyt Prosumencki)",
                "icon": "mdi:piggy-bank",
                "entities": [
                    {"entity": f"sensor.energa_{serial}_bank_wirtualny_pln", "name": "Dostępny stan depozytu prosumenckiego"},
                    {"entity": f"sensor.energa_{serial}_depozyt_wygenerowany_mtd", "name": "Doładowanie depozytu z PV w tym m-cu"},
                    {"entity": f"sensor.energa_{serial}_odzyskano_z_depozytu_mtd", "name": "Odzyskano z depozytu na pokrycie energii"},
                    {"entity": f"sensor.energa_{serial}_rcem_auto", "name": "Rynkowa cena energii skupu RCEm (PSE)"},
                    {"entity": f"sensor.energa_{serial}_cena_oddania", "name": "Wycena zasilenia depozytu brutto"},
                ],
            }
        )

    # 3. Card: Billing breakdown (MTD + Forecast)
    bill_entities = [
        {"entity": f"sensor.energa_{serial}_dotychczasowy_rachunek", "name": "Dotychczas do zapłaty (MTD)"},
        {"entity": f"sensor.energa_{serial}_prognoza_rachunku", "name": "Prognoza dopłaty na koniec miesiąca"},
        {"entity": f"sensor.energa_{serial}_koszt_brutto_mtd", "name": "Całkowity koszt energii i dystrybucji brutto"},
        {"entity": f"sensor.energa_{serial}_koszt_energii_czynnej_mtd", "name": "Koszt energii czynnej (sprzedaż)"},
        {"entity": f"sensor.energa_{serial}_koszt_dystrybucji_mtd", "name": "Koszt dystrybucji i opłat stałych"},
    ]
    if is_net_billing:
        bill_entities.append(
            {"entity": f"sensor.energa_{serial}_odzyskano_z_depozytu_mtd", "name": "Potrącenie z depozytu prosumenckiego"}
        )

    cards.append(
        {
            "type": "entities",
            "title": "💳 Rozliczenie Finansowe (Bieżące i Prognoza)",
            "icon": "mdi:receipt-text-outline",
            "entities": bill_entities,
        }
    )

    # 4. Card: Tariffs and Energy Volumes
    tariff_entities: list[dict[str, Any]] = [
        {"entity": f"sensor.energa_{serial}_taryfa", "name": "Aktywna taryfa OSD"},
    ]
    if has_zones:
        tariff_entities.extend(
            [
                {"entity": f"sensor.energa_{serial}_cena_poboru_strefa_1", "name": "Stawka poboru Strefa 1 (Dzień)"},
                {"entity": f"sensor.energa_{serial}_pobor_energii_strefa_1_mtd", "name": "Pobór energii Strefa 1 (MTD)"},
                {"entity": f"sensor.energa_{serial}_cena_poboru_strefa_2", "name": "Stawka poboru Strefa 2 (Noc)"},
                {"entity": f"sensor.energa_{serial}_pobor_energii_strefa_2_mtd", "name": "Pobór energii Strefa 2 (MTD)"},
            ]
        )
        if is_prosumer:
            tariff_entities.extend(
                [
                    {"entity": f"sensor.energa_{serial}_oddanie_energii_strefa_1_mtd", "name": "Oddanie energii Strefa 1 (MTD)"},
                    {"entity": f"sensor.energa_{serial}_oddanie_energii_strefa_2_mtd", "name": "Oddanie energii Strefa 2 (MTD)"},
                ]
            )
    else:
        tariff_entities.extend(
            [
                {"entity": f"sensor.energa_{serial}_cena_poboru", "name": "Stawka poboru G11"},
                {"entity": f"sensor.energa_{serial}_pobor_energii_mtd", "name": "Pobór energii G11 (MTD)"},
            ]
        )
        if is_prosumer:
            tariff_entities.append(
                {"entity": f"sensor.energa_{serial}_oddanie_energii_mtd", "name": "Oddanie energii G11 (MTD)"}
            )

    cards.append(
        {
            "type": "entities",
            "title": f"⚡ Taryfa {tariff} — Koszt i Wolumeny Energii",
            "icon": "mdi:transmission-tower",
            "entities": tariff_entities,
        }
    )

    # 5. Card: Physical meter registers
    meter_entities: list[dict[str, Any]] = [
        {"entity": f"sensor.energa_{serial}_numer_licznika", "name": "Numer seryjny licznika"},
        {"entity": f"sensor.energa_{serial}_ppe", "name": "Numer PPE"},
        {"entity": f"sensor.energa_{serial}_stan_licznika_import", "name": "Licznik poboru (1.8.0)"},
    ]
    if is_prosumer:
        meter_entities.append(
            {"entity": f"sensor.energa_{serial}_stan_licznika_export", "name": "Licznik oddania (2.8.0)"}
        )
    meter_entities.append(
        {"entity": f"sensor.energa_{serial}_zuzycie_dzis", "name": "Pobór energii dzisiaj"}
    )
    if is_prosumer:
        meter_entities.append(
            {"entity": f"sensor.energa_{serial}_produkcja_dzis", "name": "Oddanie energii dzisiaj"}
        )

    cards.append(
        {
            "type": "entities",
            "title": "🔢 Rejestry Licznika Fizycznego (OSD)",
            "icon": "mdi:counter",
            "entities": meter_entities,
        }
    )

    return {
        "title": title,
        "path": f"licznik-{serial}",
        "icon": view_icon,
        "badges": badges,
        "cards": cards,
    }


def build_energa_dashboard(meters: list[dict[str, Any]], coeff: float = 0.8) -> dict[str, Any]:
    """Build full dashboard configuration containing all meter views."""
    views = []
    for idx, meter in enumerate(meters):
        view = build_meter_view(meter, coeff=coeff)
        if idx == 0:
            view["path"] = "glowny"
        views.append(view)

    return {
        "title": DEFAULT_TITLE,
        "views": views,
    }


async def async_provision_dashboard(
    hass: HomeAssistant,
    meters: list[dict[str, Any]],
    url_path: str = DEFAULT_URL_PATH,
    title: str = DEFAULT_TITLE,
    icon: str = DEFAULT_ICON,
    coeff: float = 0.8,
) -> bool:
    """Provision or update the Energa Lovelace dashboard in Home Assistant."""
    try:
        ui_config = build_energa_dashboard(meters, coeff=coeff)
        clean_url = url_path.strip("/ ")
        storage_key_suffix = clean_url.replace("-", "_")

        # 1. Update .storage/lovelace_dashboards
        store_dashboards = storage.Store(hass, 1, "lovelace_dashboards")
        dash_data = await store_dashboards.async_load() or {"items": []}
        items = dash_data.get("items", [])

        # Auto-clean: remove any corrupted or bogus overrides of the built-in default 'lovelace' dashboard
        clean_items = [
            it for it in items
            if it.get("url_path") != "lovelace" and it.get("id") != "lovelace"
        ]
        if len(clean_items) != len(items):
            items = clean_items
            dash_data["items"] = items
            await store_dashboards.async_save(dash_data)
            _LOGGER.info("Purged bogus default lovelace dashboard override from lovelace_dashboards")

        existing_item = next((it for it in items if it.get("url_path") == clean_url), None)
        if not existing_item:
            items.append(
                {
                    "id": storage_key_suffix,
                    "url_path": clean_url,
                    "title": title,
                    "icon": icon,
                    "show_in_sidebar": True,
                    "require_admin": False,
                    "mode": "storage",
                }
            )
            dash_data["items"] = items
            await store_dashboards.async_save(dash_data)
            _LOGGER.info("Registered new dashboard %s in lovelace_dashboards", clean_url)

        # 2. Save dashboard views to .storage/lovelace.<url_path>
        store_view = storage.Store(hass, 1, f"lovelace.{storage_key_suffix}")
        await store_view.async_save({"config": ui_config})
        _LOGGER.info("Saved view configuration to lovelace.%s", storage_key_suffix)

        # 3. Register panel in Home Assistant frontend sidebar if available
        if "frontend" in hass.config.components:
            from homeassistant.components import frontend

            update_panel = frontend.async_panel_exists(hass, clean_url)
            frontend.async_register_built_in_panel(
                hass,
                "lovelace",
                frontend_url_path=clean_url,
                require_admin=False,
                show_in_sidebar=True,
                sidebar_title=title,
                sidebar_icon=icon,
                config={"mode": "storage"},
                update=update_panel,
            )
            _LOGGER.info(
                "Frontend panel %s %s",
                clean_url,
                "updated" if update_panel else "registered",
            )

        # 4. Notify active Lovelace storage collection if loaded in hass.data
        lovelace_data = hass.data.get("lovelace")
        if lovelace_data and hasattr(lovelace_data, "dashboards"):
            from homeassistant.components.lovelace import dashboard as ll_dashboard

            if clean_url not in lovelace_data.dashboards:
                item_spec = {
                    "id": storage_key_suffix,
                    "url_path": clean_url,
                    "title": title,
                    "icon": icon,
                    "show_in_sidebar": True,
                    "require_admin": False,
                    "mode": "storage",
                }
                lovelace_data.dashboards[clean_url] = ll_dashboard.LovelaceStorage(
                    hass, item_spec
                )

            dash_obj = lovelace_data.dashboards[clean_url]
            await dash_obj.async_save(ui_config)
            _LOGGER.info("Notified live Lovelace session for %s", clean_url)

        return True

    except Exception as err:
        _LOGGER.error("Failed to provision Energa dashboard %s: %s", url_path, err, exc_info=True)
        return False
