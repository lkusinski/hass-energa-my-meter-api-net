"""Tests for backfill progress notifications and Polish message grammar (v1.9.0)."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import custom_components.energa_mobile.services as _services
from custom_components.energa_mobile.const import CONF_CREATE_SETTLEMENT_DASHBOARD
from custom_components.energa_mobile.services import (
    BACKFILL_DISMISS_DELAY_S,
    BACKFILL_NOTIFICATION_TITLE,
    BACKFILL_OVERVIEW_NOTIFICATION_ID,
    _backfill_overview_message,
    _BackfillProgressNotifier,
    _meter_count_phrase,
    _polish_plural,
    describe_active_meters,
)


def _prosumer(**extra):
    return {"meter_point_id": "1", "total_plus": 100.0, "is_prosumer": True, **extra}


def _consumer(**extra):
    return {"meter_point_id": "2", "total_plus": 100.0, "is_prosumer": False, **extra}


class TestPolishPlural:
    """Polish has three plural categories, including the 12-14 teens."""

    def test_singular(self):
        assert _polish_plural(1, "licznik", "liczniki", "liczników") == "licznik"

    @pytest.mark.parametrize("count", [2, 3, 4, 22, 23, 24, 102])
    def test_few(self, count):
        assert _polish_plural(count, "licznik", "liczniki", "liczników") == "liczniki"

    @pytest.mark.parametrize("count", [0, 5, 11, 12, 13, 14, 15, 21, 25, 100])
    def test_many(self, count):
        assert _polish_plural(count, "licznik", "liczniki", "liczników") == "liczników"

    def test_invalid_count_defaults_to_many(self):
        assert _polish_plural(None, "licznik", "liczniki", "liczników") == "liczników"

    def test_meter_count_phrase(self):
        assert _meter_count_phrase(1) == "1 licznik"
        assert _meter_count_phrase(2) == "2 liczniki"
        assert _meter_count_phrase(5) == "5 liczników"


class TestDescribeActiveMeters:
    """Direction (one-way vs two-way prosumer) must be stated correctly."""

    def test_single_prosumer(self):
        assert describe_active_meters([_prosumer()]) == (
            "1 licznik dwukierunkowy (prosument)"
        )

    def test_single_consumer(self):
        assert describe_active_meters([_consumer()]) == "1 licznik jednokierunkowy"

    def test_two_consumers(self):
        assert describe_active_meters([_consumer(), _consumer()]) == (
            "2 liczniki jednokierunkowe"
        )

    def test_five_prosumers(self):
        assert describe_active_meters([_prosumer()] * 5) == (
            "5 liczników dwukierunkowych (prosument)"
        )

    def test_mixed(self):
        assert describe_active_meters([_consumer(), _prosumer()]) == (
            "2 liczniki: 1 jednokierunkowy, 1 dwukierunkowy (prosument)"
        )

    def test_empty(self):
        assert describe_active_meters([]) == "0 liczników"


class TestBackfillOverviewMessage:
    """The start message must match the settlement-dashboard choice."""

    @staticmethod
    def _entry(create_dashboard):
        entry = MagicMock()
        entry.options = {CONF_CREATE_SETTLEMENT_DASHBOARD: create_dashboard}
        return entry

    def test_true_prosumer_message(self):
        msg = _backfill_overview_message([_prosumer()], self._entry(True), 730)
        assert "z ostatnich 2 lat" in msg
        assert "1 licznik dwukierunkowy (prosument)" in msg
        assert "Panel «Energa — Rozliczenia» utworzono" in msg
        assert "nie został utworzony" not in msg
        assert "Wbudowany Panel Energia nie jest zmieniany" in msg
        assert "nie modyfikuje Twoich pulpitów" not in msg

    def test_false_consumer_message(self):
        msg = _backfill_overview_message([_consumer()], self._entry(False), 730)
        assert "1 licznik jednokierunkowy" in msg
        assert "Panel «Energa — Rozliczenia» nie został utworzony" in msg
        assert "zgodnie z Twoim wyborem" in msg
        assert "utworzono" not in msg

    def test_missing_option_defaults_to_created(self):
        entry = MagicMock()
        entry.options = {}
        msg = _backfill_overview_message([_prosumer()], entry, 730)
        assert "Panel «Energa — Rozliczenia» utworzono" in msg


class TestBackfillProgressNotifier:
    """Progress notification: throttled updates, final summary, auto-dismiss."""

    def test_start_posts_zero_progress(self):
        with patch.object(_services.persistent_notification, "async_create") as create:
            notifier = _BackfillProgressNotifier(
                MagicMock(), "energa_import_1", "S1", 100, datetime(2024, 1, 1)
            )
            notifier.start()
        assert create.call_count == 1
        assert create.call_args.kwargs["notification_id"] == "energa_import_1"
        assert create.call_args.kwargs["title"] == BACKFILL_NOTIFICATION_TITLE
        assert "0 / 100" in create.call_args.args[1]
        assert "Pozostało" in create.call_args.args[1]

    def test_update_is_throttled_but_final_day_bypasses(self):
        clock = {"t": 1000.0}
        with patch.object(_services.persistent_notification, "async_create") as create:
            notifier = _BackfillProgressNotifier(
                MagicMock(),
                "id",
                "S1",
                100,
                datetime(2024, 1, 1),
                now_fn=lambda: clock["t"],
                interval=45.0,
            )
            notifier.start()
            assert create.call_count == 1

            clock["t"] += 10
            notifier.update(5)
            assert create.call_count == 1  # inside the throttle window

            clock["t"] += 40  # 50 s since start
            notifier.update(20)
            assert create.call_count == 2
            assert "20 / 100" in create.call_args.args[1]

            clock["t"] += 1  # final day posts regardless of throttle
            notifier.update(100)
            assert create.call_count == 3
            assert "100 / 100" in create.call_args.args[1]

    def test_start_update_and_finish_log_at_info(self):
        clock = {"t": 1000.0}
        with patch.object(_services._LOGGER, "info") as info, patch.object(
            _services, "async_call_later"
        ), patch.object(_services.persistent_notification, "async_create"):
            notifier = _BackfillProgressNotifier(
                MagicMock(),
                "id",
                "S1",
                100,
                datetime(2024, 1, 1),
                now_fn=lambda: clock["t"],
                interval=45.0,
            )
            notifier.start()
            clock["t"] += 50
            notifier.update(20)
            notifier.finish("Gotowe")
        joined = " ".join(str(call.args) for call in info.call_args_list)
        assert "S1" in joined
        assert joined.count("backfill") == 3  # start + update + finish
        assert "finished" in joined
        assert ("S1", 20, 100, 20) in [call.args[1:] for call in info.call_args_list]

    def test_finish_posts_summary_and_schedules_dismiss(self):
        scheduled = []
        with patch.object(_services.persistent_notification, "async_create") as create, patch.object(
            _services,
            "async_call_later",
            side_effect=lambda hass, delay, cb: scheduled.append((delay, cb)),
        ) as schedule:
            notifier = _BackfillProgressNotifier(
                MagicMock(), "id", "S1", 10, datetime(2024, 1, 1)
            )
            notifier.finish("Gotowe")
        assert create.call_count == 1
        assert create.call_args.args[1] == "Gotowe"
        assert notifier.finished is True
        schedule.assert_called_once()
        assert scheduled[0][0] == BACKFILL_DISMISS_DELAY_S

        with patch.object(_services.persistent_notification, "async_dismiss") as dismiss:
            scheduled[0][1]()
        dismiss.assert_called_once()
        assert dismiss.call_args.args[1] == "id"

    def test_finish_is_idempotent(self):
        with patch.object(_services, "async_call_later"), patch.object(
            _services.persistent_notification, "async_create"
        ) as create:
            notifier = _BackfillProgressNotifier(
                MagicMock(), "id", "S1", 10, datetime(2024, 1, 1)
            )
            notifier.finish("first")
            notifier.finish("second")
        assert create.call_count == 1
        assert create.call_args.args[1] == "first"

    def test_dismiss_now_when_unfinished(self):
        with patch.object(_services.persistent_notification, "async_dismiss") as dismiss:
            notifier = _BackfillProgressNotifier(
                MagicMock(), "id", "S1", 10, datetime(2024, 1, 1)
            )
            notifier.dismiss_now()
        dismiss.assert_called_once()
        assert notifier.finished is True

    def test_dismiss_now_after_finish_is_noop(self):
        with patch.object(_services, "async_call_later"), patch.object(
            _services.persistent_notification, "async_create"
        ), patch.object(
            _services.persistent_notification, "async_dismiss"
        ) as dismiss:
            notifier = _BackfillProgressNotifier(
                MagicMock(), "id", "S1", 10, datetime(2024, 1, 1)
            )
            notifier.finish("ok")
            notifier.dismiss_now()
        dismiss.assert_not_called()


class TestMaybeAutoBackfillNotifications:
    """The overall backfill reuses one notification id (no leaks)."""

    @pytest.mark.asyncio
    async def test_single_overview_id_with_grammar(self):
        hass = MagicMock()
        api = MagicMock()
        api.async_get_data = AsyncMock(
            return_value=[
                {
                    "total_plus": 1000.0,
                    "meter_point_id": "12345",
                    "is_prosumer": True,
                }
            ]
        )
        entry = MagicMock()
        entry.data = {"auto_history_start": "2024-01-01"}
        entry.options = {}

        with patch(
            "custom_components.energa_mobile._import_meter_history", AsyncMock()
        ) as import_mock, patch(
            "custom_components.energa_mobile._has_history_statistics",
            AsyncMock(return_value=False),
        ), patch(
            "custom_components.energa_mobile.synthetic_storage."
            "async_synthesize_storage_from_recorder",
            AsyncMock(),
        ), patch.object(
            _services.persistent_notification, "async_create"
        ) as create, patch.object(
            _services, "async_call_later"
        ):
            await _services._maybe_auto_backfill(hass, api, entry)

        ids = [call.kwargs["notification_id"] for call in create.call_args_list]
        assert ids
        assert all(notification_id == BACKFILL_OVERVIEW_NOTIFICATION_ID for notification_id in ids)
        assert create.call_count >= 2  # start + completion summary
        assert "1 licznik dwukierunkowy (prosument)" in create.call_args_list[0].args[1]
        import_mock.assert_awaited()
