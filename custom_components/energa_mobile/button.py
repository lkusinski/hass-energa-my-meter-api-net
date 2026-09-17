"""Button platform for Energa My Meter integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_ENABLE_SYNTHETIC_STORAGE,
    CONF_INVERTER_ENERGY_ENTITY,
    CONF_PROSUMER_COEFFICIENT,
    CONF_VERIFY_PERIOD_END,
    CONF_VERIFY_PERIOD_START,
    DEFAULT_ENABLE_SYNTHETIC_STORAGE,
    DEFAULT_PROSUMER_COEFFICIENT,
    DOMAIN,
    SIGNAL_PERIOD_OPTIONS_UPDATED,
)
from .core.verification import format_period_date
from .dashboard_generator import (
    DEFAULT_ICON,
    DEFAULT_TITLE,
    DEFAULT_URL_PATH,
    async_provision_dashboard,
)
from .settlement import is_export_prosumer

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Energa button entities."""
    api = hass.data[DOMAIN][entry.entry_id]["api"]
    try:
        meters_list = await api.async_get_data(force_refresh=False)
    except Exception as err:
        _LOGGER.error("Energa: Failed to fetch meters for button setup: %s", err)
        meters_list = []

    active_meters = [
        m
        for m in meters_list
        if m.get("total_plus") and float(m.get("total_plus", 0)) > 0
    ]

    buttons = []
    for meter in active_meters:
        buttons.append(
            EnergaCreateDashboardButton(
                hass=hass,
                entry=entry,
                meter=meter,
                all_meters=active_meters,
            )
        )
        buttons.append(
            EnergaConfigureEnergyDashboardButton(
                hass=hass,
                entry=entry,
                meter=meter,
            )
        )
        buttons.append(
            EnergaVerifyPeriodButton(
                hass=hass,
                entry=entry,
                meter=meter,
            )
        )


    if buttons:
        async_add_entities(buttons)


class EnergaCreateDashboardButton(ButtonEntity):
    """Button to generate or refresh the Energa Lovelace dashboard."""

    _attr_has_entity_name = True
    _attr_name = "Utwórz Pulpit Rozliczeń"
    _attr_icon = "mdi:view-dashboard-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: dict[str, Any],
        all_meters: list[dict[str, Any]],
    ) -> None:
        """Initialize the button."""
        self.hass = hass
        self._entry = entry
        self._meter = meter
        self._all_meters = all_meters

        meter_id = meter["meter_point_id"]
        serial = str(meter.get("meter_serial", meter_id))
        ppe = meter.get("ppe", meter_id)

        self._serial = serial
        self._attr_unique_id = f"energa_{serial}_create_dashboard"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Energa {serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        coeff = float(
            self._entry.options.get(
                CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT
            )
        )
        _LOGGER.info(
            "Energa button pressed: provisioning dashboard for %d meters",
            len(self._all_meters),
        )
        success = await async_provision_dashboard(
            self.hass,
            self._all_meters,
            url_path=DEFAULT_URL_PATH,
            title=DEFAULT_TITLE,
            icon=DEFAULT_ICON,
            coeff=coeff,
        )
        if success:
            _LOGGER.info(
                "Energa dashboard /%s successfully created/updated", DEFAULT_URL_PATH
            )


class EnergaConfigureEnergyDashboardButton(ButtonEntity):
    """Button to automatically configure Home Assistant Energy Dashboard for Energa."""

    _attr_has_entity_name = True
    _attr_name = "Skonfiguruj Panel Energia"
    _attr_icon = "mdi:solar-power-variant-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        self.hass = hass
        self._entry = entry
        self._meter = meter

        meter_id = meter["meter_point_id"]
        serial = str(meter.get("meter_serial", meter_id))
        ppe = meter.get("ppe", meter_id)

        self._serial = serial
        self._attr_unique_id = f"energa_{serial}_configure_energy_dashboard"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(serial))},
            name=f"Energa {serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    async def async_press(self) -> None:
        """Handle the button press to configure Home Assistant Energy Dashboard."""
        try:
            from homeassistant.components.energy.data import async_get_manager
            manager = await async_get_manager(self.hass)
        except Exception as err:
            _LOGGER.error("Energy component or manager not available: %s", err)
            return

        if not manager:
            _LOGGER.error("Energy manager is None")
            return

        prefs = manager.data or manager.default_preferences()
        new_prefs = dict(prefs) if prefs else {}

        enable_synth = self._entry.options.get(
            CONF_ENABLE_SYNTHETIC_STORAGE, DEFAULT_ENABLE_SYNTHETIC_STORAGE
        )
        has_zones = self._meter.get("zone_count", 1) > 1
        serial = self._serial
        s_slug = str(serial).lower()

        inverter_entity = self._entry.options.get(
            CONF_INVERTER_ENERGY_ENTITY,
            self._entry.options.get(f"meter_{serial}_{CONF_INVERTER_ENERGY_ENTITY}", "")
        )

        is_producer = is_export_prosumer(self._meter) or bool(self._meter.get("obis_minus"))
        grid_sources = []
        battery_sources = []

        try:
            coeff = float(self._entry.options.get(CONF_PROSUMER_COEFFICIENT, DEFAULT_PROSUMER_COEFFICIENT))
        except (ValueError, TypeError):
            coeff = DEFAULT_PROSUMER_COEFFICIENT

        if coeff >= 0.7 and enable_synth:
            fee_pct = int(round((1.0 - coeff) * 100))
            if has_zones:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczna_siec_pobor_strefa_1",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczna_siec_oddanie_strefa_1",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": 0.0,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 1 (Dzień / Prowizja {fee_pct}%)",
                    },
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczna_siec_pobor_strefa_2",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczna_siec_oddanie_strefa_2",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": 0.0,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 2 (Noc / Prowizja {fee_pct}%)",
                    },
                ]
                battery_sources = [
                    {
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l1_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l1_ladowanie",
                        "name": f"Wirtualny Magazyn Energa {serial} - Strefa 1 (Opust {coeff})",
                    },
                    {
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l2_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczny_magazyn_l2_ladowanie",
                        "name": f"Wirtualny Magazyn Energa {serial} - Strefa 2 (Opust {coeff})",
                    },
                ]
            else:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczna_siec_pobor",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczna_siec_oddanie",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": 0.0,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} (Prowizja {fee_pct}%)",
                    },
                ]
                battery_sources = [
                    {
                        "stat_energy_from": f"sensor.energa_{s_slug}_syntetyczny_magazyn_rozladowanie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_syntetyczny_magazyn_ladowanie",
                        "name": f"Wirtualny Magazyn Energa {serial} (Opust {coeff})",
                    },
                ]
        elif is_producer and coeff < 0.7:
            if has_zones:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_1",
                        "stat_energy_to": f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_1",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                        "entity_energy_price_export": f"sensor.energa_{s_slug}_cena_oddania",
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 1",
                    },
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_2",
                        "stat_energy_to": f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_2",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                        "entity_energy_price_export": f"sensor.energa_{s_slug}_cena_oddania",
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 2",
                    },
                ]
            else:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_zuzycie",
                        "stat_energy_to": f"sensor.energa_{s_slug}_panel_energia_produkcja",
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru",
                        "number_energy_price": None,
                        "entity_energy_price_export": f"sensor.energa_{s_slug}_cena_oddania",
                        "number_energy_price_export": None,
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial}",
                    },
                ]
            battery_sources = []
        else:
            if has_zones:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_1",
                        "stat_energy_to": (f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_1" if is_producer else None),
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_1",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": (0.0 if is_producer else None),
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 1",
                    },
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_strefa_2",
                        "stat_energy_to": (f"sensor.energa_{s_slug}_panel_energia_produkcja_strefa_2" if is_producer else None),
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru_strefa_2",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": (0.0 if is_producer else None),
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial} - Strefa 2",
                    },
                ]
            else:
                grid_sources = [
                    {
                        "type": "grid",
                        "stat_energy_from": f"sensor.energa_{s_slug}_panel_energia_zuzycie",
                        "stat_energy_to": (f"sensor.energa_{s_slug}_panel_energia_produkcja" if is_producer else None),
                        "stat_cost": None,
                        "stat_compensation": None,
                        "entity_energy_price": f"sensor.energa_{s_slug}_cena_poboru",
                        "number_energy_price": None,
                        "entity_energy_price_export": None,
                        "number_energy_price_export": (0.0 if is_producer else None),
                        "cost_adjustment_day": 0.0,
                        "name": f"Sieć Energa {serial}",
                    },
                ]
            battery_sources = []

        existing_sources = list(new_prefs.get("energy_sources", []))
        kept_sources = [
            s for s in existing_sources
            if s.get("type") not in ("grid", "battery")
        ]
        kept_sources.extend(grid_sources)
        for b in battery_sources:
            kept_sources.append({"type": "battery", **b})

        if inverter_entity:
            has_solar = any(
                s.get("type") == "solar" and s.get("stat_energy_from") == inverter_entity
                for s in kept_sources
            )
            if not has_solar:
                kept_sources.append({
                    "type": "solar",
                    "stat_energy_from": inverter_entity,
                    "config_entry_solar_forecast": None,
                })

        new_prefs["energy_sources"] = kept_sources
        await manager.async_update(new_prefs)

        if enable_synth:
            try:
                from .synthetic_storage import async_synthesize_storage_from_recorder
                await async_synthesize_storage_from_recorder(self.hass, self._entry, self._meter)
            except Exception as synth_err:
                _LOGGER.debug("Immediate synthetic backfill during button press skipped: %s", synth_err)

        try:
            from homeassistant.components import persistent_notification
            mode_name = (
                "Wirtualny Magazyn Energii (Net-metering)"
                if enable_synth
                else "Licznik Fizyczny"
            )
            persistent_notification.async_create(
                self.hass,
                f"Panel Energia został pomyślnie zaktualizowany o konfigurację dla licznika {serial}!\n\n"
                f"Tryb: **{mode_name}**.\n"
                f"Wykresy są dostępne w zakładce [Panel Energia](/energy).",
                title="Energa: Panel Energia Skonfigurowany",
                notification_id=f"energa_energy_config_{serial}",
            )
        except Exception:
            pass


VERIFY_NOTIFICATION_TITLE = "Energa: weryfikacja rachunku"
VERIFY_RESULT_DISMISS_DELAY_S = 180.0


class EnergaVerifyPeriodButton(ButtonEntity):
    """Recompute the invoice for the dates chosen on the period date entities.

    Runs in the background (tens of seconds of API calls must not block the
    UI). Immediate feedback: the result sensor gets ``status="calculating"``
    plus the period bounds and a persistent notification is posted; on
    completion the sensor holds the full result (state = ``do_zaplaty``) and
    the notification switches to the payable summary, then auto-dismisses.
    Only one verification per meter runs at a time.
    """

    _attr_has_entity_name = True
    _attr_name = "Przelicz okres rozliczeniowy"
    _attr_translation_key = "verify_period"
    _attr_icon = "mdi:calculator-variant-outline"
    # Config entity: shown under "Configuration", not under "Controls".
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        meter: dict[str, Any],
    ) -> None:
        """Initialize the button."""
        self.hass = hass
        self._entry = entry
        self._meter = meter
        self._meter_id = str(meter.get("meter_point_id", ""))
        self._serial = str(meter.get("meter_serial", self._meter_id))
        ppe = meter.get("ppe", self._meter_id)
        self._verify_running = False
        self._period: dict[str, str | None] = {}
        self._attr_unique_id = f"energa_{self._meter_id}_verify_period"
        self.entity_id = f"button.energa_{self._serial}_przelicz_okres".lower()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=f"Energa {self._serial}",
            manufacturer="Energa-Operator",
            model=f"PPE: {ppe}",
            configuration_url="https://mojlicznik.energa-operator.pl",
        )

    async def async_added_to_hass(self) -> None:
        """Refresh availability when period dates change without a reload."""
        await super().async_added_to_hass()
        try:
            from homeassistant.helpers.dispatcher import async_dispatcher_connect

            self.async_on_remove(
                async_dispatcher_connect(
                    self.hass,
                    SIGNAL_PERIOD_OPTIONS_UPDATED,
                    self._handle_period_options_updated,
                )
            )
        except Exception as err:  # noqa: BLE001 - subscription is best effort
            _LOGGER.debug("Energa: period update subscription skipped: %s", err)

    def _handle_period_options_updated(self, entry_id: str) -> None:
        if entry_id == self._entry.entry_id:
            self.async_write_ha_state()

    def _period_bounds(self) -> tuple[str | None, str | None]:
        opts = self._entry.options or {}
        return (
            format_period_date(opts.get(CONF_VERIFY_PERIOD_START)),
            format_period_date(opts.get(CONF_VERIFY_PERIOD_END)),
        )

    @property
    def available(self) -> bool:
        """Both period dates must be set before the button can be pressed."""
        start, end = self._period_bounds()
        return bool(start and end)

    def _notification_id(self) -> str:
        return f"energa_verify_period_{self._meter_id}"

    def _coordinator(self):
        domain_data = self.hass.data.get(DOMAIN, {})
        entry_data = (
            domain_data.get(self._entry.entry_id)
            if isinstance(domain_data, dict)
            else None
        )
        if isinstance(entry_data, dict):
            return entry_data.get("coordinator")
        return None

    def _store_result(self, payload: dict) -> None:
        """Publish a result snapshot on the coordinator and refresh entities."""
        coordinator = self._coordinator()
        if coordinator is None:
            return
        store = getattr(coordinator, "_verify_result", None)
        if not isinstance(store, dict):
            store = {}
            try:
                coordinator._verify_result = store
            except Exception:  # noqa: BLE001 - never break the task on cache set
                return
        store[self._meter_id] = payload
        try:
            coordinator.async_update_listeners()
        except Exception as err:  # noqa: BLE001 - refresh is best effort
            _LOGGER.debug("Energa: verify_period listener update failed: %s", err)

    def _post_notification(self, message: str) -> None:
        try:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                message,
                title=VERIFY_NOTIFICATION_TITLE,
                notification_id=self._notification_id(),
            )
        except Exception as err:  # noqa: BLE001 - notification is best effort
            _LOGGER.debug("Energa: verify_period notification skipped: %s", err)

    def _schedule_dismiss(self) -> None:
        try:
            from .services import _schedule_notification_dismiss

            _schedule_notification_dismiss(
                self.hass,
                self._notification_id(),
                delay=VERIFY_RESULT_DISMISS_DELAY_S,
            )
        except Exception as err:  # noqa: BLE001 - cleanup is best effort
            _LOGGER.debug("Energa: verify_period dismiss scheduling skipped: %s", err)

    def _mark_calculating(self, start, end) -> None:
        """Immediate feedback: sensor attribute + 'calculating' notification."""
        self._period = {"period_start": start, "period_end": end}
        self._store_result({**self._period, "status": "calculating", "empty": False})
        self._post_notification(
            f"Liczę rachunek za okres {start} – {end}…\n\n"
            "Pobieranie danych (API/recorder) może potrwać kilkadziesiąt sekund."
        )

    async def async_press(self) -> None:
        """Start the verification in the background (one per meter at a time)."""
        start, end = self._period_bounds()
        if not start or not end:
            _LOGGER.debug(
                "Energa: verify_period button pressed without both dates set"
            )
            return
        if self._verify_running:
            _LOGGER.debug(
                "Energa: verify_period already running for meter %s — ignoring",
                self._meter_id,
            )
            self._mark_calculating(start, end)
            return
        self._verify_running = True
        # Publish the "calculating" state before scheduling so the UI reacts
        # immediately, not only when the background task first runs.
        self._mark_calculating(start, end)
        data = {
            "start": start,
            "end": end,
            "entry_id": self._entry.entry_id,
            "meter_id": self._meter_id,
        }
        coro = self._run_verification(data)
        try:
            if hasattr(self._entry, "async_create_background_task"):
                self._entry.async_create_background_task(
                    self.hass, coro, name=f"energa_verify_period_{self._meter_id}"
                )
            else:
                self.hass.async_create_task(coro)
        except Exception as err:  # noqa: BLE001 - scheduling must never raise
            self._verify_running = False
            coro.close()
            _LOGGER.error("Energa: could not schedule verify_period task: %s", err)

    async def _run_verification(self, data: dict) -> None:
        """Run the service in the background, cache the result, notify."""
        from .services import async_verify_period_data

        start = data.get("start")
        end = data.get("end")
        self._verify_running = True
        self._mark_calculating(start, end)
        try:
            result = await async_verify_period_data(self.hass, data)
        except Exception as err:  # noqa: BLE001 - background task must not explode
            _LOGGER.warning(
                "Energa: verify_period failed for meter %s: %s", self._meter_id, err
            )
            self._finish_failure(
                f"Nie udało się przeliczyć rachunku dla licznika "
                f"{self._serial}: {err}"
            )
            return
        finally:
            self._verify_running = False

        if not isinstance(result, dict):
            _LOGGER.warning(
                "Energa: verify_period returned no valid result for meter %s",
                self._meter_id,
            )
            self._finish_failure(
                f"Nie udało się przeliczyć rachunku dla licznika {self._serial} "
                "(nieprawidłowa odpowiedź usługi)."
            )
            return
        self._finish_result(result)

    def _finish_result(self, result: dict) -> None:
        """Publish a successful or empty result and update the notification."""
        if result.get("empty"):
            error = result.get("error")
            status = (
                "error" if error in ("invalid_period", "no_entry") else "empty"
            )
            payload = {**self._period, **result, "status": status}
            if status == "error":
                _LOGGER.warning(
                    "Energa: verify_period error for meter %s: %s",
                    self._meter_id,
                    error,
                )
            else:
                _LOGGER.info(
                    "Energa: verify_period empty for meter %s: %s",
                    self._meter_id,
                    error,
                )
        else:
            payload = {**result, "status": "ok"}
            _LOGGER.info(
                "Energa: verify_period done for meter %s: do zapłaty %s",
                self._meter_id,
                payload.get("do_zaplaty"),
            )
        self._store_result(payload)
        self._post_notification(self._result_message(payload))
        self._schedule_dismiss()

    def _finish_failure(self, message: str) -> None:
        """Publish an error status and notify without raising."""
        payload = {
            **self._period,
            "status": "error",
            "empty": True,
            "error": "exception",
        }
        self._store_result(payload)
        self._post_notification(message)
        self._schedule_dismiss()

    def _result_message(self, result: dict) -> str:
        if result.get("empty"):
            return (
                f"Nie znaleziono danych dla licznika {self._serial} "
                f"w okresie {result.get('period_start') or '?'} – "
                f"{result.get('period_end') or '?'}."
            )
        return (
            f"Okres: {result.get('period_start', '?')} – "
            f"{result.get('period_end', '?')}\n\n"
            f"- Netto: **{_pln(result.get('netto'))}**\n"
            f"- Brutto: **{_pln(result.get('brutto'))}**\n"
            f"- Do zapłaty: **{_pln(result.get('do_zaplaty'))}**\n\n"
            f"Źródło danych: {result.get('source', '?')}."
        )


def _pln(value) -> str:
    """Format a PLN amount defensively for the notification."""
    try:
        return f"{float(value):.2f} PLN"
    except (ValueError, TypeError):
        return "brak danych"

