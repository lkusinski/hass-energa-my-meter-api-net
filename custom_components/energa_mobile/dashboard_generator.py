"""Lovelace Dashboard Generator for Energa My Meter integration.

Provides on-demand provisioning of tailored Lovelace dashboards for
Energa meters (net-metering G12w, net-billing G12w/G11, pure consumer).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import storage

from .const import DOMAIN
from .settlement import is_export_prosumer

_LOGGER = logging.getLogger(__name__)

DEFAULT_URL_PATH = "energa-rachunek"
DEFAULT_TITLE = "Energa Rozliczenia"
DEFAULT_ICON = "mdi:lightning-bolt-circle"

# v1.9.2 (P1.2): one definition only. The local duplicate here used
# ``is_prosumer or has_export`` (``has_export`` is never assigned anywhere),
# so a meter exporting without the seller flag was misclassified as a
# consumer. ``settlement.is_export_prosumer`` is now the single source.


def _async_lookup_device(dev_reg: Any, identifier: tuple[str, str]) -> Any:
    """Return a single device entry for ``identifier`` using public HA APIs.

    Uses ``DeviceRegistry.async_get_devices`` (HA 2026.9+), which replaces the
    deprecated ``async_get_device``; identifiers are unique per config entry,
    so the first match is sufficient. Defensive: a registry that lacks the new
    method (older HA) or raises must never break dashboard generation.
    """
    try:
        devices = dev_reg.async_get_devices(identifiers={identifier})
        if devices:
            return devices[0]
        return None
    except Exception as err:  # noqa: BLE001 - lookup must never break the dashboard
        _LOGGER.debug("Device lookup failed for %s: %s", identifier, err)
        return None


def _build_device_entity_map(
    hass: HomeAssistant | None,
    serial: str,
    meter_id: str = "",
) -> dict[Any, str]:
    """Build a mapping from metric suffix / name to actual entity_id in Home Assistant.

    Enables dynamic dashboard generation regardless of custom device names or area prefixes
    (e.g., 'sensor.wejscie_licznik_energa_numer_licznika' for Area 'wejscie' and Device 'licznik energa').
    """
    if hass is None:
        return {}

    mapping: dict[Any, str] = {}
    s_clean = str(serial).strip().lower()
    mid_clean = str(meter_id).strip().lower()

    # 1. Device Registry + Entity Registry lookup
    try:
        from homeassistant.helpers import device_registry as dr
        from homeassistant.helpers import entity_registry as er

        dev_reg = dr.async_get(hass)
        # HA 2026.8+ restricts a device to a single config entry, so the old
        # `async_get_device(identifiers=...)` lookup is deprecated. Identifiers
        # are no longer globally unique; `async_get_devices` returns the
        # (possibly empty) list of matches without ambiguity. Our identifiers
        # are unique per meter, so the first match is the right one.
        device = _async_lookup_device(dev_reg, (DOMAIN, str(serial)))
        if not device and meter_id:
            device = _async_lookup_device(dev_reg, (DOMAIN, str(meter_id)))

        ent_reg = er.async_get(hass)
        entries = []
        if device and hasattr(device, "id"):
            entries = er.async_entries_for_device(ent_reg, device.id)

        # Fallback if no entries found for device or device not found: check ent_reg for entries belonging to DOMAIN
        if not entries and hasattr(ent_reg, "entities") and isinstance(ent_reg.entities, dict):
            entries = [
                entry
                for entry in ent_reg.entities.values()
                if getattr(entry, "platform", None) == DOMAIN
                and (
                    (s_clean and s_clean in str(getattr(entry, "unique_id", "")).lower())
                    or (mid_clean and mid_clean in str(getattr(entry, "unique_id", "")).lower())
                )
            ]

        for entry in entries:
            eid = getattr(entry, "entity_id", None)
            if not eid or not isinstance(eid, str):
                continue
            domain = eid.split(".", 1)[0]
            obj_id = eid.split(".", 1)[1] if "." in eid else eid

            # Map all suffix segments separated by underscore
            parts = obj_id.split("_")
            for i in range(len(parts)):
                suffix = "_".join(parts[i:])
                if suffix:
                    mapping[(domain, suffix)] = eid
                    if suffix not in mapping:
                        mapping[suffix] = eid

            # Map original_name if present
            orig_name = getattr(entry, "original_name", None)
            if orig_name and isinstance(orig_name, str):
                slug = orig_name.lower().replace(" ", "_")
                mapping[(domain, slug)] = eid
                if slug not in mapping:
                    mapping[slug] = eid
    except Exception as ex:
        _LOGGER.debug("Failed device/entity registry lookup in _build_device_entity_map: %s", ex)

    # 2. Try hass.states lookup if mapping is empty or to complement
    try:
        if hasattr(hass, "states") and hasattr(hass.states, "async_all"):
            states = hass.states.async_all()
            if isinstance(states, list):
                for st in states:
                    eid = getattr(st, "entity_id", "")
                    if not eid or not isinstance(eid, str):
                        continue
                    domain = eid.split(".", 1)[0]
                    obj_id = eid.split(".", 1)[1] if "." in eid else eid
                    is_match = (
                        (s_clean and s_clean in obj_id.lower())
                        or (mid_clean and mid_clean in obj_id.lower())
                        or "energa" in obj_id.lower()
                    )
                    if is_match:
                        parts = obj_id.split("_")
                        for i in range(len(parts)):
                            suffix = "_".join(parts[i:])
                            if suffix:
                                if (domain, suffix) not in mapping:
                                    mapping[(domain, suffix)] = eid
                                if suffix not in mapping:
                                    mapping[suffix] = eid
    except Exception as ex:
        _LOGGER.debug("Failed hass.states scan in _build_device_entity_map: %s", ex)

    return mapping


def resolve_entity(
    hass: HomeAssistant | None,
    primary: str,
    fallbacks: list[str] | None = None,
    entity_map: dict[Any, str] | None = None,
    name_suffix: str | None = None,
) -> str:
    """Resolve the most accurate entity ID available in Home Assistant.

    Checks:
    1. Pre-computed device registry entity_map if provided.
    2. Exact matches in hass.states for primary and fallbacks.
    3. Exact matches in entity_registry for primary and fallbacks.
    4. Suffix matching in hass.states if name_suffix is provided.
    5. Falls back to primary.
    """
    domain = primary.split(".")[0] if "." in primary else "sensor"

    if entity_map and name_suffix:
        if (domain, name_suffix) in entity_map:
            return entity_map[(domain, name_suffix)]
        if name_suffix in entity_map:
            return entity_map[name_suffix]

    if hass is None:
        return primary

    candidates = [primary] + (fallbacks or [])
    for candidate in candidates:
        try:
            if hass.states.get(candidate) is not None:
                return candidate
        except Exception:
            pass

    try:
        from homeassistant.helpers import entity_registry as er

        ent_reg = er.async_get(hass)
        for candidate in candidates:
            if hasattr(ent_reg, "entities") and candidate in ent_reg.entities:
                return candidate
    except Exception:
        pass

    # Suffix fallback scan in hass.states if candidates failed but name_suffix is given
    if name_suffix and hasattr(hass, "states") and hasattr(hass.states, "async_all"):
        try:
            states = hass.states.async_all(domain)
            if isinstance(states, list):
                for st in states:
                    eid = getattr(st, "entity_id", "")
                    if eid.endswith(f"_{name_suffix}") or eid == f"{domain}.{name_suffix}":
                        return eid
        except Exception:
            pass

    return primary


def _entity_exists(hass: HomeAssistant | None, entity_id: str) -> bool:
    """Best-effort check whether ``entity_id`` exists in states or registry.

    Used to add the "Sprawdzenie rachunku" section only for installs that
    actually expose the Faza 2 verification entities; older dashboards are
    left untouched. Never raises.
    """
    if hass is None or not entity_id:
        return False
    try:
        if hass.states.get(entity_id) is not None:
            return True
    except Exception:  # noqa: BLE001 - existence probe must never break generation
        pass
    try:
        from homeassistant.helpers import entity_registry as er

        reg = er.async_get(hass)
        return entity_id in reg.entities
    except Exception:  # noqa: BLE001 - existence probe must never break generation
        return False


def build_meter_view(
    meter: dict[str, Any], coeff: float = 0.8, hass: HomeAssistant | None = None
) -> dict[str, Any]:
    """Build a tailored Lovelace view for a specific Energa meter."""
    meter_id = str(meter.get("meter_point_id", ""))
    serial = str(meter.get("meter_serial", meter_id))
    s_slug = serial.lower()
    tariff_raw = str(meter.get("tariff", "G11")).strip()
    if tariff_raw.upper() == "G12W":
        tariff = "G12w"
    elif tariff_raw.upper() == "G12R":
        tariff = "G12r"
    else:
        tariff = tariff_raw.upper()
    has_zones = meter.get("zone_count", 1) > 1
    is_prosumer = is_export_prosumer(meter)
    effective_coeff = float(meter.get("prosumer_coefficient")) if meter.get("prosumer_coefficient") is not None else coeff
    is_net_billing = is_prosumer and effective_coeff < 0.7
    is_net_metering = is_prosumer and not is_net_billing

    entity_map = _build_device_entity_map(hass, serial, meter_id)

    def _eid(name: str, domain: str = "sensor") -> str:
        primary = f"{domain}.energa_{s_slug}_{name}"
        fallbacks = [
            f"{domain}.licznik_{s_slug}_{name}",
            f"{domain}.energa_{meter_id}_{name}",
            f"{domain}.licznik_{meter_id}_{name}",
        ]
        return resolve_entity(
            hass,
            primary,
            fallbacks,
            entity_map=entity_map,
            name_suffix=name,
        )

    raw_label = (
        str(meter.get("customer_label", "")).strip()
        or str(meter.get("address", "")).strip()
        or str(meter.get("custom_title", "")).strip()
        or f"Licznik {serial}"
    )

    if meter.get("customer_label"):
        short_name = str(meter["customer_label"]).strip()
        location_sub = ""
    elif meter.get("custom_title"):
        short_name = str(meter["custom_title"]).strip()
        location_sub = ""
    elif "," in raw_label:
        parts = [p.strip() for p in raw_label.split(",")]
        short_name = parts[-1] if len(parts) >= 2 else raw_label
        location_sub = parts[0] if len(parts) >= 2 else ""
    else:
        short_name = raw_label
        location_sub = ""

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
        system_desc = f"Net-metering (Opust {effective_coeff} — Magazyn kWh)"
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
            "entity": _eid("dotychczasowy_rachunek"),
            "name": "Dotychczas brutto",
        },
        {
            "entity": _eid("prognoza_rachunku"),
            "name": "Prognoza brutto",
        },
    ]

    if is_net_metering:
        badges.insert(
            0,
            {
                "entity": _eid("bank_wirtualny_kwh"),
                "name": "Magazyn kWh",
            },
        )
        badges.insert(
            1,
            {
                "entity": _eid("magazyn_poziom"),
                "name": "Poziom Magazynu",
            },
        )
    elif is_net_billing:
        badges.append(
            {
                "entity": _eid("bank_wirtualny_pln"),
                "name": "Magazyn/Depozyt",
            }
        )
        badges.append(
            {
                "entity": _eid("cena_oddania"),
                "name": "Wycena oddania",
            }
        )
    else:
        badges.append(
            {
                "entity": _eid("taryfa"),
                "name": "Taryfa",
            }
        )

    cards: list[dict[str, Any]] = []

    # 2. Card: Header Card (Markdown, clean, non-duplicated)
    meta_parts = []
    if location_sub:
        meta_parts.append(location_sub)
    meta_parts.append(f"**Taryfa:** {tariff}")
    meta_parts.append(f"**System:** {system_desc}")
    meta_parts.append(f"**Licznik:** `{serial}`")
    meta_line = " &nbsp;•&nbsp; ".join(meta_parts)

    cards.append(
        {
            "type": "markdown",
            "content": f"### 🏡 {short_name}\n{meta_line}",
        }
    )

    # 3. Card: Billing breakdown (MTD + Forecast)
    bill_entities = [
        {"entity": _eid("dotychczasowy_rachunek"), "name": "Dotychczasowy rachunek (brutto)"},
        {"entity": _eid("prognoza_rachunku"), "name": "Prognoza na koniec miesiąca (brutto)"},
        {"entity": _eid("koszt_brutto_mtd"), "name": "Całkowity koszt energii i dystrybucji brutto"},
        {"entity": _eid("koszt_energii_czynnej_mtd"), "name": "Energia czynna MTD (brutto)"},
        {"entity": _eid("koszt_dystrybucji_mtd"), "name": "Dystrybucja MTD (brutto)"},
    ]
    if is_net_billing:
        bill_entities.append(
            {"entity": _eid("bank_wirtualny_pln"), "name": "Stan konta wirtualnego (Depozyt PLN)"}
        )
        bill_entities.append(
            {"entity": _eid("cena_oddania"), "name": "Wycena zasilenia depozytu brutto"}
        )
        bill_entities.append(
            {"entity": _eid("odzyskano_z_depozytu_mtd"), "name": "Potrącenie z depozytu prosumenckiego"}
        )
    elif is_net_metering:
        bill_entities.append(
            {"entity": _eid("bank_wirtualny_kwh"), "name": "Stan magazynu wirtualnego (kWh)"}
        )

    cards.append(
        {
            "type": "entities",
            "title": "⚡ Rozliczenie Finansowe",
            "icon": "mdi:receipt-text-outline",
            "entities": bill_entities,
        }
    )

    # 4. Card: Storage / Deposit (if prosumer)
    if is_net_metering:
        storage_entities = [
            {"entity": _eid("magazyn_poziom"), "name": "Poziom napełnienia magazynu"},
            {"entity": _eid("bank_wirtualny_kwh"), "name": "Dostępne saldo w magazynie (Łącznie)"},
        ]
        if has_zones:
            storage_entities.append(
                {"entity": _eid("bank_wirtualny_l1_dzien_kwh"), "name": "Magazyn Strefa 1 / Dzień (T1)"}
            )
            storage_entities.append(
                {"entity": _eid("bank_wirtualny_l2_noc_kwh"), "name": "Magazyn Strefa 2 / Noc (T2)"}
            )
            storage_entities.append(
                {"entity": _eid("pokrycie_z_magazynu_dzien_mtd"), "name": "Pobranie z magazynu (Dzień T1 MTD)"}
            )
            storage_entities.append(
                {"entity": _eid("pokrycie_z_magazynu_noc_mtd"), "name": "Pobranie z magazynu (Noc T2 MTD)"}
            )
        storage_entities.append(
            {"entity": _eid("wspolczynnik_prosumencki"), "name": "Współczynnik opustu"}
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
                    {"entity": _eid("bank_wirtualny_pln"), "name": "Dostępny stan depozytu prosumenckiego"},
                    {"entity": _eid("depozyt_wygenerowany_mtd"), "name": "Doładowanie depozytu z PV w tym m-cu"},
                    {"entity": _eid("odzyskano_z_depozytu_mtd"), "name": "Odzyskano z depozytu na pokrycie energii"},
                    {"entity": _eid("rcem_auto"), "name": "Rynkowa cena energii skupu RCEm (PSE)"},
                    {"entity": _eid("cena_oddania"), "name": "Wycena zasilenia depozytu brutto"},
                ],
            }
        )

    # 5. Card: Tariffs and Energy Volumes
    tariff_entities: list[dict[str, Any]] = [
        {"entity": _eid("taryfa"), "name": "Aktywna taryfa OSD"},
    ]
    if has_zones:
        tariff_entities.extend(
            [
                {"entity": _eid("cena_poboru_strefa_1"), "name": "Stawka poboru Strefa 1 (Dzień)"},
                {"entity": _eid("pobor_energii_strefa_1_mtd"), "name": "Pobór energii Strefa 1 (MTD)"},
                {"entity": _eid("cena_poboru_strefa_2"), "name": "Stawka poboru Strefa 2 (Noc)"},
                {"entity": _eid("pobor_energii_strefa_2_mtd"), "name": "Pobór energii Strefa 2 (MTD)"},
            ]
        )
        if is_prosumer:
            tariff_entities.extend(
                [
                    {"entity": _eid("oddanie_energii_strefa_1_mtd"), "name": "Oddanie energii Strefa 1 (MTD)"},
                    {"entity": _eid("oddanie_energii_strefa_2_mtd"), "name": "Oddanie energii Strefa 2 (MTD)"},
                ]
            )
    else:
        tariff_entities.extend(
            [
                {"entity": _eid("cena_poboru"), "name": "Stawka poboru G11"},
                {"entity": _eid("pobor_energii_mtd"), "name": "Pobór energii G11 (MTD)"},
            ]
        )
        if is_prosumer:
            tariff_entities.append(
                {"entity": _eid("oddanie_energii_mtd"), "name": "Oddanie energii G11 (MTD)"}
            )

    if is_prosumer:
        auto_ent = _eid("autokonsumpcja_mtd")
        stopien_ent = _eid("stopien_autokonsumpcji_mtd")
        has_auto = False
        if hass is not None:
            if hass.states.get(auto_ent) is not None:
                has_auto = True
            else:
                try:
                    from homeassistant.helpers import entity_registry as er

                    reg = er.async_get(hass)
                    has_auto = auto_ent in reg.entities
                except Exception:
                    pass
        elif meter.get("has_inverter") or meter.get("inverter_entity"):
            has_auto = True

        if has_auto:
            tariff_entities.append(
                {"entity": auto_ent, "name": "Autokonsumpcja MTD"}
            )
            tariff_entities.append(
                {"entity": stopien_ent, "name": "Stopień autokonsumpcji"}
            )

    cards.append(
        {
            "type": "entities",
            "title": f"⚡ Taryfa {tariff} — Wolumeny i Koszty",
            "icon": "mdi:transmission-tower",
            "entities": tariff_entities,
        }
    )

    # 6. Card: Physical meter registers
    meter_entities: list[dict[str, Any]] = [
        {"entity": _eid("numer_licznika"), "name": "Numer seryjny licznika"},
        {"entity": _eid("ppe"), "name": "Numer PPE"},
        {"entity": _eid("stan_licznika_import"), "name": "Licznik poboru (1.8.0)"},
    ]
    if is_prosumer:
        meter_entities.append(
            {"entity": _eid("stan_licznika_export"), "name": "Licznik oddania (2.8.0)"}
        )
    meter_entities.append(
        {"entity": _eid("zuzycie_dzis"), "name": "Pobór energii dzisiaj"}
    )
    if is_prosumer:
        meter_entities.append(
            {"entity": _eid("produkcja_dzis"), "name": "Oddanie energii dzisiaj"}
        )

    cards.append(
        {
            "type": "entities",
            "title": "🔢 Rejestry Licznika (OSD)",
            "icon": "mdi:counter",
            "entities": meter_entities,
        }
    )

    # 7. Card: Dynamic PSE RCE & Arbitrage (for net-billing or prosumers)
    if is_net_billing:
        cards.append(
            {
                "type": "entities",
                "title": "⚡ Ceny Dynamiczne RCE i Arbitraż (PSE)",
                "icon": "mdi:chart-timeline-variant",
                "entities": [
                    {"entity": _eid("rcem_auto"), "name": "Miesięczna cena referencyjna RCEm"},
                    {"entity": _eid("cena_oddania"), "name": "Wycena zasilenia depozytu brutto"},
                    {"entity": _eid("dynamiczna_cena_energii_rce"), "name": "Bieżąca cena RCE (15-min)"},
                    {"entity": _eid("spread_arbitrazowy_bess_rce"), "name": "Spread arbitrażowy brutto"},
                    {"entity": _eid("okno_ladowania_bess_arbitraz_rce", domain="binary_sensor"), "name": "Okno taniego ładowania (BESS/EV)"},
                    {"entity": _eid("okno_rozladowania_bess_szczyt_rce", domain="binary_sensor"), "name": "Okno szczytu rozładowania"},
                    {"entity": _eid("cena_ujemna_rce_zagrozenie_eksportu", domain="binary_sensor"), "name": "Ostrzeżenie: ujemna cena RCE"},
                ],
            }
        )

    # 8. Card: Invoice verification ("Sprawdzenie rachunku"). Added only when
    # the Faza 2 verification entities exist, so dashboards generated for
    # older installs keep their original layout. Order matters: start, end,
    # recalculate button, result.
    verification_entities = [
        {"entity": _eid("okres_start", "date"), "name": "Okres Start"},
        {"entity": _eid("okres_koniec", "date"), "name": "Okres Koniec"},
        {
            "entity": _eid("przelicz_okres", "button"),
            "name": "Przelicz okres rozliczeniowy",
        },
        {"entity": _eid("weryfikacja_rachunku"), "name": "Okres: rozliczenie"},
    ]
    if any(_entity_exists(hass, item["entity"]) for item in verification_entities):
        cards.append(
            {
                "type": "entities",
                "title": "Sprawdzenie rachunku",
                "icon": "mdi:receipt-text-check-outline",
                "entities": verification_entities,
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


def build_energa_dashboard(
    meters: list[dict[str, Any]],
    coeff: float = 0.8,
    hass: HomeAssistant | None = None,
) -> dict[str, Any]:
    """Build full dashboard configuration containing all meter views."""
    views = []
    for idx, meter in enumerate(meters):
        view = build_meter_view(meter, coeff=coeff, hass=hass)
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
        ui_config = build_energa_dashboard(meters, coeff=coeff, hass=hass)
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
        else:
            updated = False
            if existing_item.get("icon") != icon:
                existing_item["icon"] = icon
                updated = True
            if existing_item.get("title") != title:
                existing_item["title"] = title
                updated = True
            if not existing_item.get("show_in_sidebar"):
                existing_item["show_in_sidebar"] = True
                updated = True
            if updated:
                dash_data["items"] = items
                await store_dashboards.async_save(dash_data)
                _LOGGER.info(
                    "Updated existing dashboard %s (icon=%s, title=%s) in lovelace_dashboards",
                    clean_url,
                    icon,
                    title,
                )

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
                    for prefix in ("sensor.energa_", "sensor.licznik_"):
                        if prefix in ent:
                            parts = ent.replace(prefix, "").split("_")
                            if parts:
                                return parts[0]
                for c in v.get("cards", []):
                    for ent_item in c.get("entities", []):
                        ent = ent_item.get("entity", "") if isinstance(ent_item, dict) else str(ent_item)
                        for prefix in (
                            "sensor.energa_",
                            "sensor.licznik_",
                            "binary_sensor.energa_",
                            "binary_sensor.licznik_",
                        ):
                            if prefix in ent:
                                parts = ent.replace(prefix, "").split("_")
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
            if hasattr(dash_obj, "config") and isinstance(dash_obj.config, dict):
                dash_obj.config["icon"] = icon
                dash_obj.config["title"] = title
            await dash_obj.async_save(ui_config)
            _LOGGER.info("Notified live Lovelace session for %s", clean_url)

        return True

    except Exception as err:
        _LOGGER.error("Failed to provision Energa dashboard %s: %s", url_path, err, exc_info=True)
        return False
