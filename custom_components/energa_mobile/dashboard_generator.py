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

    label = (
        str(meter.get("customer_label", "")).strip()
        or str(meter.get("address", "")).strip()
        or str(meter.get("custom_title", "")).strip()
        or f"Licznik {serial}"
    )

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

    if is_net_metering:
        system_desc = f"Net-metering (Opust {coeff} — Magazyn kWh)"
    elif is_net_billing:
        system_desc = "Net-billing (Depozyt Prosumencki PLN)"
    elif is_prosumer:
        system_desc = "Fotowoltaika (Prosumencki)"
    else:
        system_desc = "Standardowy (Konsument)"

    view_icon = (
        "mdi:battery-charging-high"
        if is_net_metering
        else "mdi:home-lightning-bolt"
        if is_net_billing
        else "mdi:transmission-tower"
    )

    # 1. Badges (Agrestowa 4 style)
    badges = [
        {
            "entity": f"sensor.energa_{serial}_dotychczasowy_rachunek",
            "name": "Dotychczas brutto",
        },
        {
            "entity": f"sensor.energa_{serial}_prognoza_rachunku",
            "name": "Prognoza brutto",
        },
    ]

    if is_net_metering:
        badges.insert(
            0,
            {
                "entity": f"sensor.energa_{serial}_bank_wirtualny_kwh",
                "name": "Magazyn kWh",
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
                "name": "Magazyn/Depozyt",
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

    # 2. Card: Header Card (Markdown, Agrestowa 4 style)
    cards.append(
        {
            "type": "markdown",
            "title": f"🏡 {label} — Centrum Rozliczeń",
            "content": (
                f"## 🏡 {label} — Centrum Rozliczeń\n"
                f"**Taryfa:** {tariff} | **System:** {system_desc} | **Licznik:** `{serial}`"
            ),
        }
    )

    # 3. Card: Billing breakdown (MTD + Forecast)
    bill_entities = [
        {"entity": f"sensor.energa_{serial}_dotychczasowy_rachunek", "name": "Dotychczasowy rachunek (brutto)"},
        {"entity": f"sensor.energa_{serial}_prognoza_rachunku", "name": "Prognoza na koniec miesiąca (brutto)"},
        {"entity": f"sensor.energa_{serial}_koszt_brutto_mtd", "name": "Całkowity koszt energii i dystrybucji brutto"},
        {"entity": f"sensor.energa_{serial}_koszt_energii_czynnej_mtd", "name": "Energia czynna MTD (brutto)"},
        {"entity": f"sensor.energa_{serial}_koszt_dystrybucji_mtd", "name": "Dystrybucja MTD (brutto)"},
    ]
    if is_net_billing:
        bill_entities.append(
            {"entity": f"sensor.energa_{serial}_bank_wirtualny_pln", "name": "Stan konta wirtualnego (Depozyt PLN)"}
        )
        bill_entities.append(
            {"entity": f"sensor.energa_{serial}_cena_oddania", "name": "Wycena zasilenia depozytu brutto"}
        )
        bill_entities.append(
            {"entity": f"sensor.energa_{serial}_odzyskano_z_depozytu_mtd", "name": "Potrącenie z depozytu prosumenckiego"}
        )
    elif is_net_metering:
        bill_entities.append(
            {"entity": f"sensor.energa_{serial}_bank_wirtualny_kwh", "name": "Stan magazynu wirtualnego (kWh)"}
        )

    cards.append(
        {
            "type": "entities",
            "title": f"⚡ Rozliczenie Finansowe Energa ({label})",
            "icon": "mdi:receipt-text-outline",
            "entities": bill_entities,
        }
    )

    # 4. Card: Storage / Deposit (if prosumer)
    if is_net_metering:
        storage_entities = [
            {"entity": f"sensor.energa_{serial}_magazyn_poziom", "name": "Poziom napełnienia magazynu"},
            {"entity": f"sensor.energa_{serial}_bank_wirtualny_kwh", "name": "Dostępne saldo w magazynie (Łącznie)"},
        ]
        if has_zones:
            storage_entities.append(
                {"entity": f"sensor.energa_{serial}_bank_wirtualny_l1_dzien_kwh", "name": "Magazyn Strefa 1 / Dzień (T1)"}
            )
            storage_entities.append(
                {"entity": f"sensor.energa_{serial}_bank_wirtualny_l2_noc_kwh", "name": "Magazyn Strefa 2 / Noc (T2)"}
            )
            storage_entities.append(
                {"entity": f"sensor.energa_{serial}_pokrycie_z_magazynu_dzien_mtd", "name": "Pobranie z magazynu (Dzień T1 MTD)"}
            )
            storage_entities.append(
                {"entity": f"sensor.energa_{serial}_pokrycie_z_magazynu_noc_mtd", "name": "Pobranie z magazynu (Noc T2 MTD)"}
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

    # 5. Card: Tariffs and Energy Volumes
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

    if is_prosumer:
        tariff_entities.append(
            {"entity": f"sensor.energa_{serial}_autokonsumpcja_mtd", "name": "Autokonsumpcja MTD"}
        )
        tariff_entities.append(
            {"entity": f"sensor.energa_{serial}_stopien_autokonsumpcji_mtd", "name": "Stopień autokonsumpcji"}
        )

    cards.append(
        {
            "type": "entities",
            "title": f"⚡ Taryfa {tariff} — Koszt i Wolumeny Energii",
            "icon": "mdi:transmission-tower",
            "entities": tariff_entities,
        }
    )

    # 6. Card: Physical meter registers
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
        "meter_serial": str(serial),
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
        existing_data = await store_view.async_load()
        if existing_data and isinstance(existing_data, dict):
            existing_views = existing_data.get("config", {}).get("views", [])
            new_views = ui_config.get("views", [])

            def _extract_serial(v: dict[str, Any]) -> str | None:
                if "meter_serial" in v:
                    return str(v["meter_serial"])
                for b in v.get("badges", []):
                    ent = b.get("entity", "") if isinstance(b, dict) else str(b)
                    if "sensor.energa_" in ent:
                        parts = ent.replace("sensor.energa_", "").split("_")
                        if parts:
                            return parts[0]
                for c in v.get("cards", []):
                    for ent_item in c.get("entities", []):
                        ent = ent_item.get("entity", "") if isinstance(ent_item, dict) else str(ent_item)
                        if "sensor.energa_" in ent:
                            parts = ent.replace("sensor.energa_", "").split("_")
                            if parts:
                                return parts[0]
                return None

            new_serials = {_extract_serial(v) for v in new_views}
            new_serials.discard(None)

            # Keep existing views from other meters
            merged_views = [ev for ev in existing_views if _extract_serial(ev) not in new_serials]
            merged_views.extend(new_views)

            # Ensure the first view has path 'glowny', subsequent views have unique paths
            seen_paths = set()
            for idx, v in enumerate(merged_views):
                ser = _extract_serial(v)
                if idx == 0:
                    v["path"] = "glowny"
                    seen_paths.add("glowny")
                else:
                    pref = f"licznik-{ser}" if ser else f"widok-{idx+1}"
                    if pref in seen_paths:
                        pref = f"{pref}-{idx+1}"
                    v["path"] = pref
                    seen_paths.add(pref)
            ui_config["views"] = merged_views

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
