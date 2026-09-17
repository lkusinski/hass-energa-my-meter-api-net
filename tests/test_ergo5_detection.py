"""Unit tests for base ergo5 install detection (pure helpers, no HA)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.energa_mobile.settlement import (
    ergo5_marker_from_manifest,
    scan_for_ergo5,
)


def _write_manifest(folder, payload, raw=None):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "manifest.json"
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestErgo5Marker:
    def test_marker_by_codeowner(self):
        assert ergo5_marker_from_manifest({"codeowners": ["@ergo5"]}) is True

    def test_marker_by_codeowner_case_and_spacing(self):
        assert ergo5_marker_from_manifest({"codeowners": [" @Ergo5 "]}) is True

    def test_marker_by_documentation_url(self):
        assert (
            ergo5_marker_from_manifest(
                {"documentation": "https://github.com/ergo5/hass-energa-my-meter-api"}
            )
            is True
        )

    def test_marker_by_issue_tracker_url(self):
        assert (
            ergo5_marker_from_manifest(
                {
                    "issue_tracker": (
                        "https://github.com/ergo5/hass-energa-my-meter-api/issues"
                    )
                }
            )
            is True
        )

    def test_own_manifest_is_not_a_hit(self):
        assert (
            ergo5_marker_from_manifest(
                {
                    "domain": "energa_mobile",
                    "codeowners": ["@lkusinski"],
                    "documentation": (
                        "https://github.com/lkusinski/hass-energa-my-meter-api-net"
                    ),
                    "issue_tracker": (
                        "https://github.com/lkusinski/hass-energa-my-meter-api-net/issues"
                    ),
                }
            )
            is False
        )

    def test_empty_and_non_dict_are_false(self):
        assert ergo5_marker_from_manifest({}) is False
        assert ergo5_marker_from_manifest(None) is False
        assert ergo5_marker_from_manifest("ergo5") is False

    def test_odd_types_do_not_raise(self):
        assert (
            ergo5_marker_from_manifest({"codeowners": 5, "documentation": 7}) is False
        )
        assert ergo5_marker_from_manifest({"codeowners": ["@other", None]}) is False


class TestScanForErgo5:
    def test_finds_ergo5_by_codeowner(self, tmp_path):
        _write_manifest(
            tmp_path / "ergo5_copy",
            {
                "domain": "energa_mobile",
                "name": "Energa My Meter API",
                "version": "4.16.0",
                "codeowners": ["@ergo5"],
            },
        )
        hits = scan_for_ergo5(str(tmp_path))
        assert len(hits) == 1
        assert hits[0]["domain"] == "energa_mobile"
        assert hits[0]["name"] == "Energa My Meter API"
        assert hits[0]["version"] == "4.16.0"
        assert hits[0]["path"].endswith("ergo5_copy")

    def test_finds_ergo5_by_url(self, tmp_path):
        _write_manifest(
            tmp_path / "renamed_folder",
            {
                "domain": "energa_mobile_copy",
                "name": "Renamed",
                "documentation": "https://github.com/ergo5/hass-energa-my-meter-api",
            },
        )
        hits = scan_for_ergo5(str(tmp_path))
        assert len(hits) == 1
        assert hits[0]["path"].endswith("renamed_folder")

    def test_ignores_our_own_manifest(self, tmp_path):
        _write_manifest(
            tmp_path / "energa_mobile",
            {
                "domain": "energa_mobile",
                "name": "Energa My Meter API (Mój Licznik) PRO",
                "codeowners": ["@lkusinski"],
                "documentation": (
                    "https://github.com/lkusinski/hass-energa-my-meter-api-net"
                ),
            },
        )
        assert scan_for_ergo5(str(tmp_path)) == []

    def test_missing_manifest_is_skipped(self, tmp_path):
        (tmp_path / "empty_component").mkdir()
        (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
        assert scan_for_ergo5(str(tmp_path)) == []

    def test_broken_json_is_skipped(self, tmp_path):
        _write_manifest(tmp_path / "broken", None, raw="{not valid json")
        assert scan_for_ergo5(str(tmp_path)) == []

    def test_missing_directory_returns_empty(self, tmp_path):
        assert scan_for_ergo5(str(tmp_path / "does_not_exist")) == []

    def test_mixed_components_reports_only_hits(self, tmp_path):
        _write_manifest(tmp_path / "ergo5_a", {"codeowners": ["@ergo5"]})
        _write_manifest(
            tmp_path / "ergo5_b",
            {"documentation": "https://github.com/ergo5/hass-energa-my-meter-api"},
        )
        _write_manifest(tmp_path / "ours", {"codeowners": ["@lkusinski"]})
        _write_manifest(tmp_path / "broken", None, raw="[")
        hits = scan_for_ergo5(str(tmp_path))
        assert {h["path"].rsplit("/", 1)[-1] for h in hits} == {"ergo5_a", "ergo5_b"}


class TestErgo5NotificationDismiss:
    """The API-less cleanup path must drop the stale persistent notification."""

    @pytest.mark.asyncio
    async def test_no_hits_deletes_issue_and_dismisses_notification(self):
        import custom_components.energa_mobile as mod

        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=[])

        with patch.object(mod.ir, "async_delete_issue") as del_issue, patch.object(
            mod.persistent_notification, "async_dismiss"
        ) as dismiss:
            await mod._async_detect_ergo5(hass)

        del_issue.assert_called_once_with(hass, mod.DOMAIN, mod.ERGO5_ISSUE_ID)
        dismiss.assert_called_once_with(hass, mod.ERGO5_ISSUE_ID)

    @pytest.mark.asyncio
    async def test_dismiss_failure_never_breaks_setup(self):
        import custom_components.energa_mobile as mod

        hass = MagicMock()
        hass.async_add_executor_job = AsyncMock(return_value=[])

        with patch.object(mod.ir, "async_delete_issue"), patch.object(
            mod.persistent_notification,
            "async_dismiss",
            side_effect=RuntimeError("no notification backend"),
        ):
            await mod._async_detect_ergo5(hass)  # must not raise
