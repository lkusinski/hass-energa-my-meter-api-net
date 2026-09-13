# PLAN DZIAŁAŃ — Krok po Kroku (2026-09-13)

> **DLA AGENTA AI:** Ten dokument to szczegółowa instrukcja. Każde zadanie ma podane:
> konkretne pliki do edycji, co dokładnie zrobić, jak zweryfikować.
> Wykonuj zadania w kolejności. Po każdym uruchom testy: `python -m pytest tests/ -v --tb=short`

---

## KONTEKST PROJEKTU

- **Co to jest:** Integracja Home Assistant dla polskich prosumentów Energa Operator
- **Repozytorium:** `/home/ubuntu/Desktop/AI-workspace/GithubEnerga/repo`
- **Kod integracji:** `repo/custom_components/energa_mobile/`
- **Testy:** `repo/tests/`
- **Wersja aktualna:** `1.7.2` (w `manifest.json`)
- **Język:** Python 3.12+, framework Home Assistant
- **Baza danych:** SQLite WAL (`storage/sqlite/database.py`)
- **Zasada:** ZERO zależności zewnętrznych (tylko stdlib + HA core)

### Kluczowe pliki (od największych):
| Plik | Linie | Rola |
|------|-------|------|
| `sensor.py` | 4249 | Coordinator + 19 klas sensorów |
| `__init__.py` | 1439 | Setup + serwisy + backfill |
| `database.py` | 1023 | SQLite storage |
| `config_flow.py` | 932 | Kreator konfiguracji |
| `api.py` | 908 | Klient API Energa Operator |
| `settlement.py` | 780 | Logika rozliczeń prosumenckich |
| `synthetic_storage.py` | 533 | Syntetyczny magazyn w Panel Energia |

---

## FAZA 1 — SPRZĄTANIE KODU (priorytet najwyższy)

### Zadanie 1.1: Uzupełnij tłumaczenie angielskie `en.json`

**Cel:** Plik `en.json` ma 126 linii, `pl.json` ma 176 linii. Brakujące klucze powodują, że użytkownicy anglojęzyczni widzą surowe identyfikatory zamiast etykiet.

**Pliki:**
- ŹRÓDŁO: `custom_components/energa_mobile/translations/pl.json`
- CEL: `custom_components/energa_mobile/translations/en.json`
- REFERENCJA: `custom_components/energa_mobile/strings.json`

**Kroki:**
1. Otwórz `pl.json` i `en.json` obok siebie
2. Znajdź klucze obecne w `pl.json`, ale nieobecne w `en.json`
3. Przetłumacz brakujące wartości z polskiego na angielski
4. Upewnij się, że `en.json` ma identyczną strukturę kluczy jak `pl.json`
5. Upewnij się, że `strings.json` też ma wszystkie klucze (to jest plik bazowy)

**Weryfikacja:**
```bash
python3 -c "
import json
with open('custom_components/energa_mobile/translations/pl.json') as f: pl = json.load(f)
with open('custom_components/energa_mobile/translations/en.json') as f: en = json.load(f)
def compare(d1, d2, path=''):
    for k in d1:
        if k not in d2:
            print(f'BRAK w en.json: {path}.{k}')
        elif isinstance(d1[k], dict):
            compare(d1[k], d2.get(k, {}), f'{path}.{k}')
compare(pl, en)
"
```

**Oczekiwany wynik:** Brak komunikatów = pełna zgodność.

---

### Zadanie 1.2: Dodaj `requirements_test.txt`

**Cel:** Zależności testowe są hardcoded w `.github/workflows/tests.yml`. Stwórz standardowy plik.

**Utwórz plik:** `repo/requirements_test.txt`

**Zawartość:**
```
pytest>=7.0
pytest-asyncio>=0.21
pytest-cov>=4.0
aiohttp>=3.9
voluptuous>=0.13
ruff>=0.4
```

**Następnie edytuj:** `.github/workflows/tests.yml`
- Zamień linię `pip install pytest pytest-asyncio aiohttp voluptuous`
- Na: `pip install -r requirements_test.txt`

**Weryfikacja:** `pip install -r requirements_test.txt && python -m pytest tests/ -v`

---

### Zadanie 1.3: Dodaj coverage do CI

**Cel:** Dodaj mierzenie pokrycia kodu testami.

**Edytuj:** `.github/workflows/tests.yml`

**Zmień linię:**
```yaml
          python -m pytest tests/ -v --tb=short
```
**Na:**
```yaml
          python -m pytest tests/ -v --tb=short --cov=custom_components/energa_mobile --cov-report=term-missing --cov-fail-under=60
```

**Weryfikacja:** Uruchom lokalnie:
```bash
pip install pytest-cov
python -m pytest tests/ -v --cov=custom_components/energa_mobile --cov-report=term-missing
```

---

### Zadanie 1.4: Utwórz GitHub Issue Templates

**Cel:** Ułatwić użytkownikom zgłaszanie błędów i propozycji.

**Utwórz plik:** `repo/.github/ISSUE_TEMPLATE/bug_report.yml`

**Zawartość:**
```yaml
name: "🐛 Zgłoszenie błędu"
description: "Zgłoś błąd w integracji Energa My Meter"
labels: ["bug"]
body:
  - type: textarea
    id: description
    attributes:
      label: "Opis błędu"
      description: "Opisz co się wydarzyło i czego oczekiwałeś."
    validations:
      required: true
  - type: input
    id: version
    attributes:
      label: "Wersja integracji"
      placeholder: "np. 1.7.2"
    validations:
      required: true
  - type: input
    id: ha_version
    attributes:
      label: "Wersja Home Assistant"
      placeholder: "np. 2026.9.0"
    validations:
      required: true
  - type: dropdown
    id: tariff
    attributes:
      label: "Taryfa"
      options:
        - G11
        - G12
        - G12W
        - G13
        - Inna
    validations:
      required: true
  - type: dropdown
    id: system
    attributes:
      label: "System rozliczeniowy"
      options:
        - Net-metering (opusty 0.8)
        - Net-metering (opusty 0.7)
        - Net-billing (RCEm)
        - Konsument (bez PV)
    validations:
      required: true
  - type: textarea
    id: logs
    attributes:
      label: "Logi"
      description: "Wklej logi z Ustawienia → System → Logi (filtruj: energa_mobile)"
      render: text
```

**Utwórz plik:** `repo/.github/ISSUE_TEMPLATE/feature_request.yml`

**Zawartość:**
```yaml
name: "💡 Propozycja funkcji"
description: "Zaproponuj nową funkcję lub ulepszenie"
labels: ["enhancement"]
body:
  - type: textarea
    id: description
    attributes:
      label: "Opis propozycji"
      description: "Opisz funkcję, którą chciałbyś zobaczyć."
    validations:
      required: true
  - type: textarea
    id: use_case
    attributes:
      label: "Przypadek użycia"
      description: "Jak ta funkcja pomoże Ci w codziennym użytkowaniu?"
```

---

### Zadanie 1.5: Utwórz CODEOWNERS

**Utwórz plik:** `repo/.github/CODEOWNERS`

**Zawartość:**
```
# Cały kod integracji
/custom_components/energa_mobile/ @lkusinski

# Testy
/tests/ @lkusinski

# Dokumentacja
/docs/ @lkusinski
```

---

## FAZA 2 — REFAKTOR sensor.py (priorytet wysoki)

> **UWAGA DLA AI:** To jest najtrudniejsze zadanie. `sensor.py` ma 4249 linii.
> Rozbijaj ostrożnie. Po KAŻDYM kroku uruchamiaj testy.
> NIE zmieniaj logiki — tylko przenoś kod między plikami.

### Zadanie 2.1: Wydziel `coordinator.py`

**Cel:** Przenieś klasę `EnergaCoordinator` z `sensor.py` do nowego pliku `coordinator.py`.

**Kroki:**
1. Otwórz `custom_components/energa_mobile/sensor.py`
2. Znajdź klasę `EnergaCoordinator` (szukaj `class EnergaCoordinator`)
3. Zanotuj numer linii początkowej i końcowej klasy
4. Zanotuj WSZYSTKIE importy z których korzysta ta klasa
5. Utwórz plik `custom_components/energa_mobile/coordinator.py`
6. Przenieś klasę do nowego pliku z wymaganymi importami
7. W `sensor.py` dodaj: `from .coordinator import EnergaCoordinator`
8. Sprawdź czy `__init__.py` też importuje `EnergaCoordinator` — jeśli tak, zaktualizuj import

**Weryfikacja:**
```bash
python -m pytest tests/ -v --tb=short
```
**Oczekiwany wynik:** Wszystkie 358 testów PASS (zero failures).

**WAŻNE ZASADY:**
- NIE zmieniaj nazw klas ani metod
- NIE zmieniaj logiki — tylko przenoś
- Zachowaj WSZYSTKIE komentarze i docstringi
- Jeśli klasa korzysta z helpera zdefiniowanego w `sensor.py`, ten helper też przenieś lub zaimportuj

---

### Zadanie 2.2: Utwórz katalog `sensors/` i przenieś klasy sensorów

**Cel:** Pogrupuj 19 klas sensorów w logiczne moduły.

**Kroki:**
1. Utwórz katalog: `custom_components/energa_mobile/sensors/`
2. Utwórz `custom_components/energa_mobile/sensors/__init__.py` (pusty lub z re-eksportami)
3. Zidentyfikuj klasy sensorów w `sensor.py` (szukaj `class Energa*Sensor`)
4. Pogrupuj je tak:

**Plik `sensors/bank.py`** — sensory banku energii:
- `EnergaBankKwhSensor`
- `EnergaBankZoneSensor`
- `EnergaBankPlnSensor`
- `EnergaBankLevelSensor`
- `EnergaBankFlowSensor`

**Plik `sensors/bill.py`** — sensory rachunku:
- `EnergaBillForecastSensor`
- `EnergaBillCurrentSensor`
- `EnergaBillComponentSensor`

**Plik `sensors/live.py`** — sensory odczytów bieżących:
- `EnergaLiveSensor`
- `EnergaProsumerBalanceSensor`
- `EnergaStatisticsSensor`
- `EnergaSyntheticStatisticsSensor`
- `EnergaAutoconsumptionSensor`

**Plik `sensors/price.py`** — sensory cenowe:
- `EnergaPriceSensor`
- `EnergaRceSensor`

5. Przenieś każdą grupę do odpowiedniego pliku z importami
6. W oryginalnym `sensor.py` zastąp klasy importami:
   ```python
   from .sensors.bank import (
       EnergaBankKwhSensor, EnergaBankZoneSensor,
       EnergaBankPlnSensor, EnergaBankLevelSensor,
       EnergaBankFlowSensor,
   )
   from .sensors.bill import (
       EnergaBillForecastSensor, EnergaBillCurrentSensor,
       EnergaBillComponentSensor,
   )
   # itd.
   ```
7. Zachowaj funkcję `async_setup_entry` i factory w `sensor.py`

**Weryfikacja:**
```bash
python -m pytest tests/ -v --tb=short
```
**Oczekiwany wynik:** 358 testów PASS.

---

### Zadanie 2.3: Zaktualizuj `manifest.json` wersję

**Po zakończeniu refaktoru:**

**Edytuj:** `custom_components/energa_mobile/manifest.json`
- Zmień `"version": "1.7.2"` na `"version": "1.8.0"`

**Edytuj:** `CHANGELOG.md`
- Dodaj na początku pliku:
```markdown
## v1.8.0 (2026-09-XX)

### Changed
- **Refaktor:** Rozbito monolityczny `sensor.py` (4249 L) na modułową strukturę:
  - `coordinator.py` — `EnergaCoordinator`
  - `sensors/bank.py` — sensory banku kWh/PLN/Level/Flow
  - `sensors/bill.py` — prognoza rachunku i komponenty
  - `sensors/live.py` — odczyty bieżące i statystyki
  - `sensors/price.py` — sensory cenowe RCE/RCEm
- Uzupełniono tłumaczenie angielskie (`en.json`)
- Dodano coverage report do CI pipeline
- Dodano GitHub Issue Templates i CODEOWNERS
```

---

## FAZA 3 — DUALNY MAGAZYN L1/L2 (v1.9.0)

> **UWAGA DLA AI:** Ta faza wymaga zrozumienia logiki biznesowej rozliczeń prosumenckich.
> Przeczytaj najpierw: `docs/BANK.md` i `PLAN.md` sekcja 5.

### Zadanie 3.1: Rozszerz model danych settlement

**Pliki do edycji:**
- `custom_components/energa_mobile/core/settlement/models.py`
- `custom_components/energa_mobile/core/settlement/fifo_net_metering.py`

**Cel:** Rozdziel kolejkę FIFO na dwie niezależne kolejki: L1 (strefa dzienna) i L2 (strefa nocna).

**Kontekst biznesowy:**
- Energa Operator bilansuje magazyn prosumencki w dwóch oddzielnych strefach
- Przykład z faktury Wiśniowa: L1 początek 752 kWh → koniec 1518 kWh, L2 początek 606 kWh → koniec 938 kWh
- Łącznie: 1358 → 2456 kWh (to jest zweryfikowane na prawdziwej fakturze)
- W modelu FIFO: wkłady do L1 nie mogą pokryć zużycia z L2 i odwrotnie

**Kroki:**
1. Przeczytaj `core/settlement/models.py` — zrozum obecne struktury danych
2. Przeczytaj `core/settlement/fifo_net_metering.py` — zrozum obecną kolejkę FIFO
3. Dodaj pola `zone` (L1/L2) do modelu wkładu (`Lot` lub odpowiedni dataclass)
4. W `fifo_net_metering.py` rozdziel przetwarzanie na dwie niezależne kolejki
5. Upewnij się, że 12-miesięczne przedawnienie działa niezależnie per strefa

**Weryfikacja — napisz test:**
```python
# W tests/test_settlement.py dodaj:
def test_dual_zone_warehouse_wisniowa_invoice():
    """Weryfikacja przejścia stanu magazynu Wiśniowej 1358 → 2456 kWh.

    Dane z faktury FES/00042 (01.07-31.08.2026):
    L1: 752 kWh → -83 (zużycie) + 1061*0.8 (oddanie) = 1518 kWh
    L2: 606 kWh → -342 (zużycie) + 843*0.8 (oddanie) = 938 kWh
    Suma: 1518 + 938 = 2456 kWh
    """
    # ... implementacja testu
    assert bank_l1 == 1518
    assert bank_l2 == 938
    assert bank_l1 + bank_l2 == 2456
```

---

### Zadanie 3.2: Dodaj sensory strefowe

**Pliki do edycji:**
- `sensors/bank.py` (po refaktorze z Fazy 2) lub `sensor.py`

**Cel:** Dodaj dwa nowe sensory:
- `sensor.energa_<nr>_bank_kwh_l1` — stan magazynu strefa dzienna
- `sensor.energa_<nr>_bank_kwh_l2` — stan magazynu strefa nocna

**Kroki:**
1. Skopiuj wzorzec z istniejącego `EnergaBankKwhSensor`
2. Utwórz `EnergaBankZoneL1Sensor` i `EnergaBankZoneL2Sensor`
3. Dodaj atrybuty `bank_l1_share_pct` i `bank_l2_share_pct` do głównego sensora banku
4. Zarejestruj nowe sensory w `async_setup_entry`

---

### Zadanie 3.3: Rozszerz Options Flow

**Plik do edycji:** `custom_components/energa_mobile/config_flow.py`

**Cel:** Umożliwić wprowadzenie stanów początkowych per strefa.

**Dodaj pola w OptionsFlow:**
- `bank_initial_kwh_l1` (np. 752 kWh)
- `bank_initial_kwh_l2` (np. 606 kWh)
- Automatyczne sumowanie: `bank_initial_kwh = l1 + l2`

**Dodaj klucze tłumaczeń w `pl.json` i `en.json`:**
```json
"bank_initial_kwh_l1": "Stan początkowy magazynu strefa 1 (kWh)",
"bank_initial_kwh_l2": "Stan początkowy magazynu strefa 2 (kWh)"
```

---

## FAZA 4 — DEVOPS (priorytet niski, kiedy będzie czas)

### Zadanie 4.1: Automatyczny release pipeline

**Utwórz:** `.github/workflows/release.yml`

```yaml
name: Release

on:
  push:
    tags:
      - 'v*'

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Run tests
        run: |
          pip install pytest pytest-asyncio aiohttp voluptuous
          python -m pytest tests/ -v --tb=short

      - name: Create Release
        uses: softprops/action-gh-release@v2
        with:
          generate_release_notes: true
```

### Zadanie 4.2: Przenieś `test_api_response.py` do `scripts/`

**Kroki:**
1. Przenieś `repo/test_api_response.py` → `repo/scripts/test_api_response.py`
2. Sprawdź czy `.gitignore` nie blokuje (linia `/test_*.py` dotyczy roota)

---

## WERYFIKACJA KOŃCOWA

Po zakończeniu KAŻDEJ fazy uruchom pełny zestaw sprawdzeń:

```bash
# 1. Testy jednostkowe
python -m pytest tests/ -v --tb=short

# 2. Linting
pip install ruff
ruff check custom_components/ tests/ --select E,F,I --ignore E501,E402

# 3. Sprawdź manifest
python3 -c "import json; m=json.load(open('custom_components/energa_mobile/manifest.json')); print(f'Wersja: {m[\"version\"]}, Domena: {m[\"domain\"]}')"

# 4. Sprawdź tłumaczenia
python3 -c "
import json
for f in ['translations/pl.json', 'translations/en.json', 'strings.json']:
    with open(f'custom_components/energa_mobile/{f}') as fh:
        d = json.load(fh)
        print(f'{f}: OK ({len(str(d))} znaków)')
"
```

**Oczekiwany wynik:** Wszystko zielone, zero błędów.

---

## SŁOWNIK TERMINÓW (dla AI)

| Termin | Znaczenie |
|--------|-----------|
| **Net-metering** | Stary system rozliczeń prosumentów PV (opust 0.8 lub 0.7 z kWh) |
| **Net-billing** | Nowy system rozliczeń (depozyt w PLN po cenie RCEm × 1.23) |
| **RCEm** | Rynkowa cena energii miesięczna (publikowana przez PSE/OIRE) |
| **FIFO** | First In First Out — kolejka wkładów do magazynu (12-miesięczne przedawnienie) |
| **G12W** | Taryfa dwustrefowa weekendowa (L1=dzień roboczy, L2=noc+weekend) |
| **G11** | Taryfa jednostrefowa (jedna cena całą dobę) |
| **L1 / L2** | Strefa 1 (szczyt/dzień) / Strefa 2 (pozaszczyt/noc) |
| **PPE** | Punkt Poboru Energii (identyfikator przyłącza, 18 cyfr) |
| **OSD** | Operator Sieci Dystrybucyjnej (Energa Operator) |
| **HACS** | Home Assistant Community Store (sklep z integracjami) |
| **Panel Energia** | Natywny dashboard HA do śledzenia zużycia energii |
| **OBIS** | Kody identyfikujące pomiary na liczniku (np. 1.8.0 = pobór, 2.8.0 = oddanie) |
| **Coordinator** | Klasa HA odpowiedzialna za cykliczne odpytywanie API |
| **Config Flow** | Kreator konfiguracji integracji w HA (GUI) |
| **Options Flow** | Edycja ustawień integracji po instalacji (GUI) |
| **Recorder** | Baza danych HA przechowująca historię stanów encji |
| **Spike guard** | Ochrona przed anomalnymi skokami w danych statystycznych |
