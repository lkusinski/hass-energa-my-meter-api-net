"""Tests for get_price_for_key — pricing logic."""


from custom_components.energa_mobile.const import (
    CONF_IMPORT_PRICE,
    CONF_IMPORT_PRICE_1,
    CONF_IMPORT_PRICE_2,
    DEFAULT_EXPORT_PRICE,
    DEFAULT_IMPORT_PRICE,
    DEFAULT_IMPORT_PRICE_1,
    DEFAULT_IMPORT_PRICE_2,
    get_price_for_key,
)


class TestGetPriceForKey:
    """Tests for get_price_for_key()."""

    def test_default_import_price(self):
        """Returns default import price when no options set."""
        result = get_price_for_key({}, "import")
        assert result == DEFAULT_IMPORT_PRICE

    def test_default_zone_prices(self):
        """Returns default zone prices."""
        assert get_price_for_key({}, "import_1") == DEFAULT_IMPORT_PRICE_1
        assert get_price_for_key({}, "import_2") == DEFAULT_IMPORT_PRICE_2

    def test_default_export_price(self):
        """Returns default export price."""
        assert get_price_for_key({}, "export") == DEFAULT_EXPORT_PRICE

    def test_custom_import_price(self):
        """Returns custom price from options."""
        options = {CONF_IMPORT_PRICE: 1.50}
        assert get_price_for_key(options, "import") == 1.50

    def test_custom_zone_prices(self):
        """Returns custom zone prices."""
        options = {CONF_IMPORT_PRICE_1: 1.30, CONF_IMPORT_PRICE_2: 0.60}
        assert get_price_for_key(options, "import_1") == 1.30
        assert get_price_for_key(options, "import_2") == 0.60

    def test_per_meter_override(self):
        """Per-meter price takes precedence over global price."""
        options = {
            CONF_IMPORT_PRICE: 1.00,
            "meter_30132815_import_price": 2.00,
        }
        # With meter_id => per-meter
        assert get_price_for_key(options, "import", meter_id="30132815") == 2.00
        # Without meter_id => global
        assert get_price_for_key(options, "import") == 1.00

    def test_per_meter_fallback_to_global(self):
        """Falls back to global when no per-meter key exists."""
        options = {CONF_IMPORT_PRICE: 1.50}
        result = get_price_for_key(options, "import", meter_id="99999999")
        assert result == 1.50

    def test_unknown_data_key_defaults_to_import(self):
        """Unknown data_key falls back to import price."""
        result = get_price_for_key({}, "unknown_key")
        assert result == DEFAULT_IMPORT_PRICE

    def test_string_price_converted_to_float(self):
        """Prices stored as strings are converted to float."""
        options = {CONF_IMPORT_PRICE: "1.234"}
        assert get_price_for_key(options, "import") == 1.234


class TestGetProsumerCoefficient:
    """Tests for get_prosumer_coefficient()."""

    def test_default_coefficient(self):
        from custom_components.energa_mobile.const import (
            DEFAULT_PROSUMER_COEFFICIENT,
            get_prosumer_coefficient,
        )
        assert get_prosumer_coefficient({}) == DEFAULT_PROSUMER_COEFFICIENT

    def test_global_coefficient(self):
        from custom_components.energa_mobile.const import (
            CONF_PROSUMER_COEFFICIENT,
            get_prosumer_coefficient,
        )
        assert get_prosumer_coefficient({CONF_PROSUMER_COEFFICIENT: 0.7}) == 0.7

    def test_per_meter_override(self):
        from custom_components.energa_mobile.const import (
            CONF_PROSUMER_COEFFICIENT,
            get_prosumer_coefficient,
        )
        options = {
            CONF_PROSUMER_COEFFICIENT: 0.8,
            "meter_11685328_prosumer_coefficient": 0.0,
        }
        assert get_prosumer_coefficient(options, "11685328") == 0.0
        assert get_prosumer_coefficient(options, "372197", serial="11685328") == 0.0
        assert get_prosumer_coefficient(options, "99999999") == 0.8


class TestGetMeterBaseline:
    """Tests for get_meter_baseline()."""

    def test_baselines(self):
        from custom_components.energa_mobile.const import (
            CONF_BALANCE_BASELINE_IMPORT,
            get_meter_baseline,
        )
        options = {
            CONF_BALANCE_BASELINE_IMPORT: 100.0,
            "meter_11685328_balance_baseline_import_1": 1932.634,
            "meter_11685328_balance_baseline_import_2": 2423.794,
            "balance_baseline_export_1": 50.0,
        }
        # Serial lookup
        assert get_meter_baseline(options, "import_1", meter_id="372197", serial="11685328") == 1932.634
        # Meter id lookup
        assert get_meter_baseline(options, "import_1", meter_id="11685328") == 1932.634
        # Global per-zone fallback
        assert get_meter_baseline(options, "export_1", meter_id="372197") == 50.0
        # Generic prefix fallback
        assert get_meter_baseline(options, "import", meter_id="372197") == 100.0
        assert get_meter_baseline(options, "export", meter_id="372197", default=0.0) == 0.0


class TestGetMeterInitialBank:
    """Tests for get_meter_initial_bank()."""

    def test_initial_bank(self):
        from custom_components.energa_mobile.const import get_meter_initial_bank
        options = {
            "bank_initial_kwh": 500.0,
            "meter_11685328_bank_initial_pln": 150.0,
        }
        assert get_meter_initial_bank(options, "pln", meter_id="372197", serial="11685328") == 150.0
        assert get_meter_initial_bank(options, "kwh", meter_id="372197", serial="11685328") == 500.0
        assert get_meter_initial_bank(options, "kwh_l1", meter_id="372197", serial="11685328", default=0.0) == 0.0
