"""Constants for Energa My Meter integration."""

DOMAIN = "energa_mobile"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_TOKEN = "device_token"
CONF_IMPORT_PRICE = "import_price"  # PLN/kWh for consumption (single-zone G11)
CONF_EXPORT_PRICE = "export_price"  # PLN/kWh for production compensation
CONF_IMPORT_PRICE_1 = "import_price_1"  # PLN/kWh zone 1 / peak (G12w)
CONF_IMPORT_PRICE_2 = "import_price_2"  # PLN/kWh zone 2 / off-peak (G12w)
CONF_PROSUMER_COEFFICIENT = "prosumer_coefficient"  # Net billing coefficient (0.0-1.0)
CONF_BALANCE_BASELINE_IMPORT = "balance_baseline_import"  # Meter import reading at period start (kWh)
CONF_BALANCE_BASELINE_EXPORT = "balance_baseline_export"  # Meter export reading at period start (kWh)
CONF_BANK_RCE_PRICE = "bank_rce_price"  # PLN/kWh for net-billing (RCE/RCEm, e.g. 0.26288 for 07.2026)
CONF_BANK_INITIAL_KWH = "bank_initial_kwh"  # Initial kWh bank for old prosumer (e.g. 1358 from the 06.2026 invoice)
CONF_BANK_INITIAL_PLN = "bank_initial_pln"  # Initial PLN bank for new prosumer (e.g. 0.0 on 01.08.2026 after the 07.2026 settlement)

# Default prices (PLN/kWh) - G12w tariff from 2026-01-01
DEFAULT_IMPORT_PRICE = 1.188
DEFAULT_IMPORT_PRICE_1 = 1.2453  # Zone 1 (peak)
DEFAULT_IMPORT_PRICE_2 = 0.5955  # Zone 2 (off-peak)
DEFAULT_EXPORT_PRICE = 0.95
DEFAULT_PROSUMER_COEFFICIENT = 0.8
DEFAULT_BALANCE_BASELINE = 0.0  # 0 = count from meter installation (lifetime)
DEFAULT_BANK_RCE_PRICE = 0.26288  # RCEm for 07.2026, update monthly via PSE
DEFAULT_BANK_INITIAL_KWH = 0.0
DEFAULT_BANK_INITIAL_PLN = 0.0

# Tariff fee table overrides for the full-bill forecast (v0.2.14).
# Keys match tariff.py _OPTION_KEY_MAP ("tariff_*"); defaults mirror
# tariff.G12W_DEFAULT_FEES (parity covered by tests/test_tariff.py).
CONF_TARIFF_ENERGY_DAY = "tariff_energy_day"
CONF_TARIFF_ENERGY_NIGHT = "tariff_energy_night"
CONF_TARIFF_EXCISE_MWH = "tariff_excise_mwh"
CONF_TARIFF_TRADE_FEE = "tariff_trade_fee"
CONF_TARIFF_ABONAMENT = "tariff_abonament"
CONF_TARIFF_GRID_FIXED = "tariff_grid_fixed"
CONF_TARIFF_GRID_VAR_DAY = "tariff_grid_var_day"
CONF_TARIFF_GRID_VAR_NIGHT = "tariff_grid_var_night"
CONF_TARIFF_QUALITY = "tariff_quality"
CONF_TARIFF_OZE = "tariff_oze"
CONF_TARIFF_COGEN = "tariff_cogen"
CONF_TARIFF_CAPACITY = "tariff_capacity"

DEFAULT_TARIFF_ENERGY_DAY = 0.6107
DEFAULT_TARIFF_ENERGY_NIGHT = 0.3990
DEFAULT_TARIFF_EXCISE_MWH = 5.00
DEFAULT_TARIFF_TRADE_FEE = 0.0
DEFAULT_TARIFF_ABONAMENT = 0.74
DEFAULT_TARIFF_GRID_FIXED = 20.17
DEFAULT_TARIFF_GRID_VAR_DAY = 0.4017
DEFAULT_TARIFF_GRID_VAR_NIGHT = 0.0851
DEFAULT_TARIFF_QUALITY = 0.0332
DEFAULT_TARIFF_OZE = 0.0073
DEFAULT_TARIFF_COGEN = 0.0030
DEFAULT_TARIFF_CAPACITY = 24.05

# API endpoints
BASE_URL = "https://api-mojlicznik.energa-operator.pl/dp"
LOGIN_ENDPOINT = "/apihelper/UserLogin"
SESSION_ENDPOINT = "/apihelper/SessionStatus"
DATA_ENDPOINT = "/resources/user/data"
CHART_ENDPOINT = "/resources/mchart"

# API headers (iOS app user agent)
HEADERS = {
    "User-Agent": "Energa/3.1.2 (pl.energa-operator.mojlicznik; build:1; iOS 16.6.1) Alamofire/5.6.4",
    "Accept": "application/json",
    "Accept-Language": "pl-PL;q=1.0, en-PL;q=0.9",
    "Content-Type": "application/json",
}

# Spike guard: maximum plausible hourly energy consumption in kWh
MAX_HOURLY_KWH = 100

# PSE API for RCE (Rynkowa Cena Energii)
PSE_RCE_API_URL = "https://api.raporty.pse.pl/api/rce-pln"
CONF_RCE_AUTO_FETCH = "rce_auto_fetch"  # Enable/disable auto RCE fetch
DEFAULT_RCE_AUTO_FETCH = False

# Settlement / auto-calibration (v0.2.11)
# Sources (verified 2026-09-04):
# - Old net-metering (opusty 0.8/0.7): energy valid 12 months from introduction
#   (counted from last day of introduction month), FIFO oldest-first.
#   See: energa.pl/dom/strefa-prosumenta/net-metering, enerad.pl/net-metering-system-opustow
# - New net-billing: deposit valid 12 months from assignment (assigned next calendar
#   month, x1.23), FIFO oldest-first, refund cap 20% (RCEm) / 30% (RCE since 01.02.2025).
#   See: energa.pl/dom/strefa-prosumenta/net-billing, gov.pl 27.12.2024 (Dz.U. 1847)
# NOTE: a plain calendar reset (Jan 1 / each month) would NOT comply — both systems
# are rolling 12-month FIFO windows.
CONF_SETTLEMENT_DATE = "settlement_date"  # YYYY-MM-DD: annual settlement anniversary (old system, e.g. invoice date 2026-06-30)
CONF_ENABLE_AUTO_SETTLEMENT = "enable_auto_settlement"  # Show settlement/expiry calibration attributes
CONF_USE_ROLLING_365D = "use_rolling_365d"  # Old system: compute bank from last 365d of statistics (FIFO) instead of lifetime baseline
DEFAULT_SETTLEMENT_DATE = ""  # empty = anniversary derived from baselines/invoice not set; attributes show validity note only
DEFAULT_ENABLE_AUTO_SETTLEMENT = False
DEFAULT_USE_ROLLING_365D = False
ROLLING_MIN_COVERAGE_DAYS = 300  # minimum statistics coverage to trust rolling 365d mode
FIFO_MIN_COVERAGE_MONTHS = 11  # minimum months with flows to trust FIFO 12m bank (v0.2.20)


def get_price_for_key(
    options: dict, data_key: str, meter_id: str | None = None
) -> float:
    """Get the configured price for a given data key.

    Supports per-meter pricing: if meter_id is provided, looks for
    meter-specific keys first (e.g. 'meter_30132815_import_price'),
    then falls back to global keys.
    """
    key_map = {
        "import": (CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE),
        "import_1": (CONF_IMPORT_PRICE_1, DEFAULT_IMPORT_PRICE_1),
        "import_2": (CONF_IMPORT_PRICE_2, DEFAULT_IMPORT_PRICE_2),
        "export": (CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE),
        "export_1": (CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE),
        "export_2": (CONF_EXPORT_PRICE, DEFAULT_EXPORT_PRICE),
    }

    conf_key, default_val = key_map.get(
        data_key, (CONF_IMPORT_PRICE, DEFAULT_IMPORT_PRICE)
    )

    # Per-meter override: meter_{serial}_{key}
    if meter_id:
        meter_key = f"meter_{meter_id}_{conf_key}"
        if meter_key in options:
            return float(options[meter_key])

    return float(options.get(conf_key, default_val))
