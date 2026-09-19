# Changelog

## v1.9.3 (2026-09-18) — kanoniczna baza jako źródło salda + start w środku miesiąca

- **Kanoniczna baza SQLite jako źródło salda w `verify_period` (LUKA 1).**
  Salda otwarcia (magazyn kWh i depozyt PLN) są teraz zapisywane i odczytywane
  z kanonicznego magazynu (`.storage/energa_canonical.db`, tabele
  `settlement_lot`/`market_price`). Precedencja:
  `override` (wejście) → `canonical` (snapshot w bazie) → `recorder`/`api`
  (przeliczenie z historii). Gdy bazy nie ma lotów na dany moment, wynik jest
  liczony z recordera/API i **zapisywany** (snapshot lotów per strefa +
  historyczne `RCEM` do `market_price`), więc kolejne przeliczenie tego samego
  okresu nie powtarza kosztownej rekonstrukcji. Wynik zwraca `opening_source`
  (`override`/`canonical`/`recorder`/`api`) i ostrzeżenia. Snapshot ma
  deterministyczny `lot_id` (`open_<ppe>_<unit>_<zone>_<data>`) i
  `rule_version=opening_snapshot_v1`; przed zapisem lotu zakładany jest wiersz
  `ppe` (FK).
- **Spójność z Bankiem (LUKA 1).** Wartość magazynu użyta jako saldo otwarcia
  pochodzi z tego samego silnika FIFO 12 m-cy, co sensory
  `*_bank_wirtualny_kwh`/`..._bank_kwh_l1/l2` (`fifo_kwh_bank` /
  `fifo_dual_zone_kwh_bank`); test akceptacyjny porównuje saldo `verify_period`
  z `_fifo_bank_from_monthly` dla tego samego momentu.
- **Start okresu w środku miesiąca (LUKA 2).** `verify_period` dolicza
  częściowy miesiąc startowy: przepływy od 1. dnia miesiąca startowego do
  `period_start` (dzienne/godzinowe `change` z recordera) wchodzą do FIFO kWh
  oraz do depozytu PLN (M → M+1, ważność 12 m-cy). Dzięki temu
  `bank_open_1/2` i `deposit_open` opisują stan **na moment `period_start`**,
  a nie na 1. dnia miesiąca. Gdy dostępny jest wyłącznie miesięczny cache API
  (bez podziału na dni), miesiąc startowy jest pomijany, a wynik dostaje
  czytelne ostrzeżenie (saldo na 1. dnia). Okresy pełnomiesięczne bez zmian.
- **Testy**: 684 passed, 1 skipped; `ruff` czysty.

## v1.9.2 (2026-09-18) — RCEm per miesiąc okresu + stawki produktu

- **RCEm z miesiąca oddania energii (PSE), nie z opcji.** `verify_period`
  rozwiązuje RCEm dla każdego miesiąca okresu z całej tabeli PSE
  (`async_fetch_official_rcem_map`); dla okresu wielomiesięcznego depozyt jest
  ważony per miesiąc. `bank_rce_price`/`_rce_cache` to wyłącznie fallback
  (z ostrzeżeniem), a jawne `rcem_pln`/`rcem` nadal nadpisuje wszystko.
  Wynik zwraca `rcem` (użyty) i `rcem_source` (`pse_table`/`option`/`override`).
  Naprawia Agrestową 08.2026: depozyt 157,59 i do zapłaty ≈615,53 bez podawania
  `rcem_pln` (wcześniej przestarzałe 0,26288 dawało 140,65/631,81).
- **Stawki produktu.** Dwie wariantowe tabele: `G12W_OFERTA` („Oferta
  Podstawowa”, net-metering — energia 0,7125/0,4622, handlowa 16,18,
  abonament 0,70) i `G12W_URZEDOWA` („taryfa urzędowa”, net-billing — jak
  dotychczasowe `G12W_DEFAULT_FEES`). API nie podaje nazwy produktu, więc
  produkt jest inferowany z systemu rozliczeń, gdy stawek nie ma w opcjach,
  oraz jawnie wybierany w Options (`tariff_product`). `fee_source` przyjmuje
  `options`/`product`/`partial`/`defaults`; przy `partial`/`defaults` jest
  ostrzeżenie. Naprawia Wiśniową 07–08.2026: 129,04/29,68/158,72 (excise
  0,60/1,86) bez ręcznego wpisywania stawek.
- **Options**: nowy selektor produktu w sekcji cen z domyślnymi dla dwóch
  typowych produktów (Wiśniowa vs Agrestowa) + zapis wszystkich `tariff_*`.
- **Testy**: 671 passed, 1 skipped; `ruff` czysty.

## v1.9.1 (2026-09-18) — automatyczne saldo początkowe depozytu (FIFO 12 m-cy + RCEm)

- **`verify_period` sam liczy saldo depozytu na początek okresu** z całej historii (FIFO 12 m-cy, RCEm per miesiąc), zamiast wymagać ręcznego `deposit_open_pln`; nadpisania ręczne nadal działają.
- Nowe: `opening_deposit_from_monthly_flows`, `async_fetch_official_rcem_map` (cała tabela PSE raz/dzień, cache), `deposit_open_source`/`deposit_history`/`warnings`; magistrala recorder→API YEAR.
- Magazyn net-meteringu: FIFO kWh także z cache koordynatora; 0 kWh przy historii z eksportem nie degraduje już do `coverage_unknown`.
- Nowa usługa **`clear_period`** (czyści zapisane daty okresu).
- Testy akceptacyjne: Agrestowa 08 = 628,55/144,57/773,12/157,59/615,53 (auto); Wiśniowa 07–08 = 129,04/29,68/158,72.

## v1.9.0 (2026-09-18) — Rozliczenia na godzinowych saldach i weryfikacja rachunku

- Rozliczenia na **godzinowych saldach** (net-billing i net-metering); akcyza od nakładki/brutto; całe kWh per strefa.
- **Weryfikacja rachunku**: usługa `verify_period` + UI (2 daty + „Przelicz okres rozliczeniowy" + sensor wyniku), encja kompletności okresu z blokadą przycisku, powiadomienie o liczeniu z ETA; źródło API z fallbackiem na recorder.
- **Salda początkowe**: magazyn (net-metering, FIFO 12 m-cy) i depozyt (net-billing).
- Onboarding: system „(wykryto)", rekomendacja PV, checkbox panelu „Energa — Rozliczenia", konsument `0.0` + backfill.
- Detekcja kopii bazowej integracji ergo5 i duplikatu domeny; `docs/MIGRACJA_ergo5.md`.
- Poprawki: `async_get_hourly_range`, magazyn (change vs state), szybszy start (bez „taking over 10 s"), anulowanie zadań w tle.

## v1.9.0-beta.8 (2026-09-18) — brak „taking over 10 seconds", etykieta konsumenta, detekcja duplikatu domeny

- **A) Setup platformy `sensor` bez drugiego refreshu koordynatora (defekt 2).**
  Prawdziwa przyczyna `Setup of sensor platform energa_mobile is taking over
  10 seconds` na `agrestowa` nie leżała w samym `sensor.async_setup_entry`, lecz
  w `update_before_add=True`: HA wołał dla **każdej** z 42 encji
  `CoordinatorEntity.async_update()` → `coordinator.async_request_refresh()`,
  więc debouncer uruchamiał kolejny pełny refresh API (~10 s, log
  `Finished fetching … in 10.65 seconds`), na który czekał setup platformy.
  Zmiana na `update_before_add=False` (stany początkowe nadal zapisuje
  `Entity.add_to_platform_finish`) usuwa ten refresh. Analogicznie
  `binary_sensor` i `date`.
- **A) Brak ponownego `api.async_get_data` w setupie platform.** `sensor`,
  `button` i `date` korzystają teraz z `coordinator.data` (świeżego po
  `async_config_entry_first_refresh`), a API wołają tylko jako fallback, gdy
  koordynator nie ma danych. Cache-read `force_refresh=False` wciąż brał
  `api._data_lock`, który równoległy refresh koordynatora trzyma przez cały
  fetch liczników i wykresów — to blokowało start platformy.
- **B) Konsument jednokierunkowy = `consumer` (defekt 4).** Licznik bez
  eksportu (`is_export_prosumer` false) nie jest już etykietowany
  `net_billing` z powodu `prosumer_coefficient=0.0`. Nowe czyste helpery
  `settlement_system_name`/`settlement_system_label` dają `system` =
  „konsument (jednokierunkowy)" i `settlement_type` = `consumer` w
  `build_period_invoice`, sensora wyniku okresu oraz sensorów rachunku
  (`system`/`settlement_type`). Ostrzeżenie „Brak salda początkowego depozytu
  (net-billing)" i noty depozytowe (`hourly_netting_note`) są dla konsumenta
  wyłączone — brak eksportu ⇒ brak depozytu. Kwoty bez zmian (depozyt 0,00).
  Zachowanie prosumenta net-billing/net-metering **bez zmian** (w tym
  historyczne `settlement_type=net_billing_rcem`).
- **C) Detekcja duplikatu domeny (przyczyna zawieszenia HA).** Nowy czysty
  `scan_for_domain_duplicates` znajduje **każdy inny** katalog w
  `custom_components/*/manifest.json` z `"domain": "energa_mobile"` (backup,
  kopia, fork ergo5). `_async_detect_duplicate_domain` zgłasza `Repairs`
  (`duplicate_domain_detected`) + `persistent_notification` z listą ścieżek i
  jednoznaczną instrukcją (przenieś kopie poza `custom_components`), a po
  zniknięciu kopii auto-usuwa issue i notyfikację. Uzupełnia wcześniejszy
  `scan_for_ergo5`.
- **Tłumaczenia**: nowy `issues.duplicate_domain_detected` w `strings.json`,
  `translations/pl.json`, `translations/en.json`.
- **Testy**: 639 passed, 1 skipped (nowe: etykieta/ostrzeżenia konsumenta w
  fakturze okresu i sensorach rachunku, skan duplikatów domeny + Repairs/
  notyfikacja, szybki setup platformy bez drugiego refreshu i bez wołania API
  gdy koordynator ma dane).

## v1.9.0-beta.7 (2026-09-18) — niezawodny start Core: timeouty API + cache dzienny (naprawa zawieszenia)

- **Przyczyna**: `EnergaAPI.async_get_data(force_refresh=False)` — wołane przez
  **każdą** platformę w `async_setup_entry` (`sensor`/`button`/`binary_sensor`/`date`) —
  mimo „cache" ponownie pobierało dzienne wykresy (`daily_pobor`/`daily_produkcja` oraz
  warianty strefowe). Dla licznika G12w to do 6 zapytań HTTP na platformę, seryjnie pod
  `_data_lock`, więc setup platformy przekraczał 10 s (log
  `Setup of binary_sensor platform energa_mobile is taking over 10 seconds`), a przy
  wolnym/DNS-owym Energa — blokował start Core na minuty.
- **Przyczyna (2)**: brak jawnego timeoutu na żądaniach `async_login`/`_api_get` —
  aiohttp używał domyślnego 300 s na jedno zapytanie, więc nieodpowiadający endpoint
  mógł zawiesić setup na wiele minut.
- **Naprawa (cache)**: nowy znacznik `_daily_fetched` — dzienne wykresy pobierane tylko
  przy `force_refresh=True` (cykl koordynatora co 15 min) lub dla nowego licznika;
  `force_refresh=False` to teraz tanie czytanie z cache.
- **Naprawa (timeouty)**: `aiohttp.ClientTimeout(total=30, connect=10)` przekazywany do
  wszystkich żądań logowania i `_api_get`.
- **Naprawa (fail-fast setup)**: gdy pierwszy refresh koordynatora się nie powiedzie,
  `async_setup_entry` propaguje teraz `ConfigEntryNotReady` (z osobnym bezpiecznikiem
  `asyncio.wait_for(..., timeout=90)`) zamiast połykać błąd i pozwalać każdej platformie
  powtarzać pełne, nieograniczone pobranie. HA kończy start i ponawia wpis w tle.
- **Higiena środowiska**: kopie integracji o tym samym `domain = energa_mobile`
  (`energa_mobile.preclean.bak` beta.1 na `wisniowa`, `energa_mobile.prebak` beta.2 na
  `agrestowa`) przeniesiono poza `custom_components` (loader HA wybierał moduł
  niedeterministycznie). Zostaje wyłącznie `custom_components/energa_mobile`.
- **`verify_period` aktualizuje sensor wyniku**: wynik usługi jest teraz publikowany na
  koordynatorze (pod `meter_point_id` i `meter_serial`), więc
  `sensor.*_weryfikacja_rachunku` odzwierciedla również bezpośrednie wywołanie usługi,
  nie tylko przycisk „Przelicz okres".
- **Komunikat o brakujących kluczach dla net-billingu**: `bank_open_*/bank_close_*` są
  wymagane tylko dla starego systemu (net-metering); net-billing nie ma magazynu, więc
  nie generuje już fałszywego `missing_breakdown_keys` / warningu (lab `agrestowa`).
- **Testy**: 620 passed, 1 skipped (nowe: bounded-timeouty, `_api_get`/`async_login`
  przekazują timeout, cache dzienny przy `force_refresh=False`/`True`, publikacja wyniku
  usługi, brak fałszywych brakujących kluczy dla net-billingu).

## v1.9.0-beta.6 (2026-09-18) — fee_source na sensorze wyniku + anulowanie taska profilu

- **`fee_source` w atrybutach sensora wyniku**: `sensor.energa_<serial>_weryfikacja_rachunku`
  („Okres: rozliczenie") eksponuje teraz `fee_source` (`options`/`partial`/`defaults`) —
  wcześniej `_BREAKDOWN_KEYS` go pomijał, więc atrybut = `None`, mimo że usługa zwracała
  wartość. Gwarantowany jest teraz **pełny** zestaw kluczy z `build_period_invoice`:
  `system`, `coverage_unknown`, `warnings` oraz `kwh` z `cover_1/2`, `bank_open_1/2`,
  `bank_close_1/2`. Brakujące klucze są logowane (warning) i raportowane w atrybucie
  `missing_breakdown_keys`, zamiast cicho znikać. Pusty okres zwraca `fee_source=None`.
- **Anulowanie zadania profilu przy wyładowaniu wpisu**: zadanie
  `_async_update_profile_forecasts` jest teraz śledzone (`_profile_forecast_task`) i
  uruchamiane jako cancellable task; `async_unload_entry` (unload/reload/`ha core stop`)
  anuluje je przez `coordinator.async_shutdown()` z `try/except CancelledError`. Koniec z
  `ERROR … Setup of config entry … cancelled` + tracebackiem profilu; brak
  „Task was destroyed" / „coroutine was never awaited".

## v1.9.0-beta.5 (2026-09-17) — UX kalkulatora, postęp historii, PV w onboardingu

- **Bez migania encji**: zmiana dat okresu nie przeładowuje już integracji (smart update listener).
- **Feedback kalkulatora**: natychmiastowy status „liczę…" + powiadomienie start/wynik; obsługa pustego okresu/błędu; blokada podwójnych kliknięć.
- **Powiadomienie o liczeniu z ETA i postępem**: start podaje „Przeliczam rachunek za okres … – …. Proszę czekać (szacowany czas: ~N s)" z liczbą dni (`~1,2 s/dzień`); w trakcie dzień-po-dniu wątek API raportuje postęp przez `on_progress(done, total)`, a powiadomienie odświeża się co ~5 s: „Postęp: X/Y dni (Z%), pozostało ~M s" z ETA liczonym z **zmierzonego** tempa; zakończenie/błąd/pusty okres pokazuje dotychczasowy czytelny wynik. Jedno idempotentne powiadomienie na licznik (`energa_verify_period_<mid>`), auto-dismiss.
- **Postęp pobierania historii przywrócony**: wskaźnik co ~45 s (ile/ile, %, przetwarzany dzień, **ETA**), poprawne „1 licznik / 2 liczniki", kierunek (jedno-/dwukierunkowy) i treść zgodna z wyborem panelu; auto-dismiss; logi info.
- **Onboarding**: checkbox „Utwórz panel «Energa — Rozliczenia» (zalecane)"; detekcja PV w natywnym Panelu Energia; jednokierunkowy → domyślna opcja „Tylko konsument — brak PV (wykryto)"; dwukierunkowy bez PV → rekomendacja dodania źródła PV.
- **Kompletność okresu (nowa encja)**: `sensor.energa_<serial>_okres_kompletnosc` („Okres: kompletność danych", `diagnostic`, stany `complete`/`incomplete`/`unknown`) sprawdza pokrycie dni w wybranym zakresie — najpierw statystyki długoterminowe recordera, potem API; atrybuty: `period_start`, `period_end`, `expected_days`, `available_days`, `missing_days`, `completeness_pct`, `source_checked`, `checked_at`. Weekend bez odczytu nie fałszuje „incomplete".
- **Bramka przycisku „Przelicz okres rozliczeniowy"**: `available = obie daty ustawione AND kompletność == complete`. Przy `incomplete`/`unknown` przycisk jest niedostępny, a ewentualne wywołanie usługi zwraca czytelny błąd/warning (`period_incomplete`) zamiast cichego liczenia na dziurze. Status odświeża się przy zmianie dat (smart listener — bez reloadu integracji).
- **Nota z recenzji (backfill)**: weryfikacja na labie `wisniowa` — backfill faktycznie wystartował i **zakończył się** (731 dni w statystykach LTS, 70 128 odczytów w magazynie kanonicznym, flaga `auto_backfill_completed=true`). Plik `/config/energa_canonical.db` = 0 B to osierocony artefakt (magazyn działa w `.storage/energa_canonical.db`), bez wpływu na działanie.

### 🛠️ Poprawki z testu na żywo (net-metering, lab `wisniowa`)

- **Magazyn net-meteringu z właściwej kolumny**: `_collect_monthly_flows` czyta teraz dzienny przyrost z `change` (a nie z `state`, który nie jest dobowym przyrostem) i ma fallback `reset_aware_delta` po kolumnie `sum`; rekonstrukcja banku 0/0 przy dodatnich saldach jest jawnie oznaczana jako `coverage_unknown` + `warning` (koniec cichego zawyżania). Regresja Wiśniowej: FIFO daje `bank_open` ≥ salda dodatnie, `cover_1/2 = 83/342` kWh, faktura **129,04 / 29,68 / 158,72**.
- **Stawki produktu i akcyza net-meteringu**: `verify_period` bierze stawki z **opcji wpisu** (`fees_from_options(entry.options, tariff)`), a wynik niesie `fee_source` (`options`/`partial`/`defaults`) z ostrzeżeniem, gdy brakujące klucze powodują użycie tabeli domyślnej. Dla `old_system=True` akcyza liczy się od **poboru brutto** per strefa (`add_excise=True`, `excise_*` = brutto). Test akceptacyjny bez wstrzykiwania stawek odtwarza fakturę co do grosza.
- **Kompletność okresu — thread-safety i skuteczna bramka**: zapisy stanu i tworzenie zadań odbywają się wyłącznie w pętli HA (`hass.loop.call_soon_threadsafe`), poprzednie zadanie jest anulowane przy zmianie dat (nowsze wygrywa, licznik generacji), a nieświeży/niepoliczony status jest natychmiast publikowany jako `unknown`. Bramka `button.*_przelicz_okres.available` czyta świeży status dla **bieżącego** zakresu dat; usługa odmawia (`period_incomplete`) także przy statusie `unknown`/braku werdyktu (gdy infrastruktura kompletności istnieje), a nie tylko przy `incomplete`.
- **ETA liczenia**: `VERIFY_SECONDS_PER_DAY = 3.0` (realny czas ~3 s/dzień na labie `wisniowa`) przy zachowaniu ETA z **zmierzonego** tempa; treść nadal pokazuje liczbę dni.

## v1.9.0-beta.4 (2026-09-17) — fix encji wyniku (kategoria sensora)

- `sensor.*_weryfikacja_rachunku` („Okres: rozliczenie"): `EntityCategory.DIAGNOSTIC` zamiast `CONFIG`
  — HA 2026.9 odrzucał `config` na sensorach (encja była `unavailable`). Date/button zostają `config`.
- `dashboard_generator`: usunięty deprecated fallback `device_registry.async_get_device` (warning).

## v1.9.0-beta.3 (2026-09-17) — Panel „Sprawdzenie rachunku" (UI)

- Encje kalkulatora przeniesione do grupy **Konfiguracja** (poza „Sterowanie" i „Sensory" na stronie urządzenia).
- Nazwy: **Okres Start**, **Okres Koniec**, **Przelicz okres rozliczeniowy**, **Okres: rozliczenie**.
- Generowany pulpit: sekcja **„Sprawdzenie rachunku"** z encjami w kolejności (start → koniec → przelicz → wynik).
- Wording: ergo5 to **integracja bazowa** (usunięto „obca"); usługa API zwraca dane, poprawki onboardingu konsumenta.

## v1.9.0-beta.2 (2026-09-17) — Weryfikacja rachunku (kalkulator) + detekcja ergo5

### 🧾 Weryfikacja rachunku za dowolny okres (beta)
- **Usługa `energa_mobile.verify_period`** (start, end, opcjonalnie `rcem_pln`): liczy rachunek jak sprzedawca
  i **zwraca pełne rozbicie** (netto/VAT/brutto/depozyt/do zapłaty + pozycje i salda).
- **Źródło danych**: API Energa (miesiące wstecz) z fallbackiem na statystyki recordera; `source` w odpowiedzi.
- **UI**: `date.energa_<serial>_okres_start`, `date.energa_<serial>_okres_koniec`,
  `button.energa_<serial>_przelicz_okres` oraz `sensor.energa_<serial>_weryfikacja_rachunku`
  (stan = `do zaplaty`, ~35 atrybutów z rozbiciem).
- Weryfikacja na żywo: odtwarza realną fakturę (różnica ≤ ~0,7 zł = ±1 kWh danych OSD).

### 🔎 Detekcja równoległej kopii bazowej integracji ergo5
- Skan `custom_components/*/manifest.json` + `.storage/hacs.repositories` → **Repairs** (`ergo5_detected`)
  i powiadomienie z listą ścieżek; automatyczne domknięcie, gdy kopii już nie ma.
- Kreator: krok ostrzegawczy z potwierdzeniem przed utworzeniem wpisu.
- `docs/MIGRACJA_ergo5.md`: przewodnik migracji i revertu (oparty na testach na labach).

### 🛠️ Poprawki
- Onboarding konsumenta: wpis dostaje jawnie `prosumer_coefficient = 0.0` (+ backfill istniejących wpisów).
- `async_get_hourly_range` zwracał 0 punktów (AttributeError w logu) — naprawione.
- Poprawne selectory `number` w `services.yaml`; `device_registry` bez deprecacji.

## v1.9.0 (2026-09-17) — Rozliczenie net-billing na saldach godzinowych (zgodne z fakturą)

### 🧮 Wierne odtworzenie faktury Energa (net-billing)
- **Podstawa rozliczenia = godzinowe salda (OSD „BP"/bilansowanie prosumentów).**
  Energia czynna, dystrybucja zmienna i jakościowa liczone są od sumy godzinowych
  sald **dodatnich** per strefa, a depozyt od sumy sald **ujemnych** — tak jak
  rozlicza sprzedawca. Nowe: `tariff.hourly_saldo`, `tariff.bill_saldos`,
  `tariff.mtd_invoice_bases`; coordinator liczy je z godzinowych statystyk
  recordera (`_async_compute_hourly_saldos`) i dokłada do cache MTD.
- **Akcyza jako realna pozycja netto** dla net-billingu (nakładka = pobór brutto
  − salda dodatnie, 5 PLN/MWh). Dla G11 i net-meteringu pozostaje informacyjna
  (w cenie energii). Zaokrąglanie linii faktury wg **ROUND_HALF_UP** (0,023 MWh → 0,12).
- **Depozyt** nakładany maks. do wysokości energia czynna + akcyza (brutto).
- Ilości rozliczeniowe w **całych kWh per strefa** (jak na fakturze).
- **Fallback**: brak danych godzinowych → dotychczasowy tryb brutto (bez regresji
  dla starego net-meteringu).
- Weryfikacja co do grosza na realnej fakturze Agrestowa **FES/00045**
  (VIII 2026: netto 628,55 / VAT 144,57 / brutto 773,12 / depozyt 157,59 / do zapłaty
  615,53). Testy regresyjne: `TestRealInvoiceAugust2026`, `TestHourlySaldo`,
  `TestMtdInvoiceBases`.

## Unreleased

### 🧪 Testy Jednostkowe i Kompatybilność Środowiskowa (Issue #3)
- **Kompatybilność Testów SQLite na Windowsie (`test_storage_sqlite.py`, `test_idempotent_reimport.py`):**
  - Zastąpiono `tempfile.NamedTemporaryFile` standardową fixture pytestową `tmp_path` w fixture `file_storage` oraz w teście migracji schematu `test_schema_v1_to_v2_migration`.
  - Rozwiązuje to problem blokowania uchwytu do pliku na poziomie systemu Windows i eliminuje błąd `PermissionError: [Errno 13] Permission denied` przy równoległym otwarciu pliku przez `sqlite3.connect` (podziękowania dla @ergo5 za zgłoszenie i rekomendację!).
  - Zaktualizowano test `test_idempotent_reimport.py` do używania `tmp_path` dla zachowania spójności.

## v1.8.3 (2026-09-14) — Pełna Obsługa Własnych Nazw Urządzeń i Stref (Issue #2 Follow-up)

### 🚀 Nowości i Poprawki Architektoniczne
- **Rejestro-Zależne Dopasowywanie Encji (`dashboard_generator.py`):**
  - Rozszerzono silnik generowania pulpitu o bezpośrednie odpytywanie rejestru urządzeń (`device_registry`) oraz rejestru encji (`entity_registry`) Home Assistant (`_build_device_entity_map`).
  - Identyfikacja urządzenia odbywa się na podstawie niezmiennych unikalnych identyfikatorów sprzętowych `identifiers={(DOMAIN, str(serial))}` lub `identifiers={(DOMAIN, str(meter_point_id))}`.
  - Pulpit dynamicznie mapuje aktualne identyfikatory encji niezależnie od:
    - Zmiany nazwy urządzenia przez użytkownika (np. `"licznik energa"` zamiast domyślnego `"Energa {serial}"`),
    - Przypisania urządzenia do strefy/obszaru (Area) w Home Assistant (np. strefa `"wejscie"` tworząca encje `sensor.wejscie_licznik_energa_*`),
    - Ręcznego przemianowania identyfikatorów encji w panelu ustawień Home Assistant.
  - Zapewniono pełną izolację między wieloma licznikami na jednym koncie — encje z jednego urządzenia nie kolidują z widokami drugiego licznika.
  - Zachowano wsteczną kompatybilność: fallback do sprawdzania `hass.states` oraz wartości domyślnych przy braku rejestru (np. środowiska testowe).

## v1.8.2 (2026-09-13) — Dynamiczne Rozpoznawanie Encji Pulpitu, Spójność Urządzeń i Wyciszenie Logów Monotonic Clamp

### 🐛 Rozwiązanie Zgłoszenia Issue #2 ("no entities")
- **Dynamiczne Rozpoznawanie Encji (`dashboard_generator.py`):**
  - Wprowadzono inteligentną funkcję `resolve_entity(hass, primary, fallbacks)`, która dynamicznie weryfikuje aktualne identyfikatory encji w rejestrze `hass.states` oraz `entity_registry`.
  - Pulpit automatycznie dopasowuje się do encji z prefiksem `sensor.energa_*` lub `sensor.licznik_*`, eliminując błąd "Wykryto nieznaną encję" na kartach Lovelace niezależnie od kolejności inicjalizacji platform.
  - Warunkowe generowanie kart autokonsumpcji: encje `autokonsumpcja_mtd` oraz `stopien_autokonsumpcji_mtd` są dodawane wyłącznie wtedy, gdy użytkownik skonfiguruje encję falownika (`CONF_INVERTER_ENERGY_ENTITY`) lub gdy sensory realnie istnieją w Home Assistant.
- **Ujednolicenie Rejestracji Urządzeń (`binary_sensor.py`, `sensors/price.py`):**
  - Ujednolicono identyfikator urządzenia `DeviceInfo` we wszystkich encjach binarnych (okna ładowania/rozładowania BESS, ujemne ceny RCE, tania strefa) oraz cen dynamicznych RCE na `identifiers={(DOMAIN, str(meter_serial))}` i nazwę `Energa {serial}` (zamiast `Licznik {serial}`).
  - Zapobiega to rozbieżnościom nazw w rejestrze urządzeń Home Assistant (`dr.async_get(hass)`), gdy platformy pomocnicze rejestrują się przed platformą główną `sensor`.
- **Wyciszenie Spamu w Logach (`recorder_adapter.py`):**
  - Zmieniono poziom logowania monotonicznego przycinania sum (`validate_and_clean_statistics`) z `WARNING` na `DEBUG`.
  - Zapobiega to zalewaniu dziennika zdarzeń setkami tysięcy ostrzeżeń podczas 2-letniego importu historii zużycia dla liczników ze skokami lub korektami wskazań OSD.
- **Klarowne Komunikaty Powiadomień (`services.py`):**
  - Doprecyzowano treść powiadomień po instalacji i imporcie historii, jednoznacznie informując, że wbudowany domyślny Panel Energia Home Assistant nie jest modyfikowany bez wiedzy użytkownika, a dane trafiają do długoterminowych statystyk (LTS).

## v1.8.1 (2026-09-13) — Modułowe Usługi (services.py), Sensory Decyzyjne i Poprawki CI/CD

### 🏗️ Modułowa Refaktoryzacja Usług (`services.py`)
- **Wydzielenie `services.py` (1 288 linii):**
  - Wyodrębniono rejestrację usług Home Assistant (`energa_mobile.update_data`, `energa_mobile.download_history`, `energa_mobile.import_statistics_service`), logikę synchronizacji i importu statystyk LTS do bazy Home Assistant z monolitycznego pliku `__init__.py`.
  - Odchudzono `__init__.py` z **1 440 linii** do **175 linii** (~88% redukcji).
  - Pełna zgodność wsteczna: re-eksport `import_statistics_for_range`, `async_import_statistics_for_meter`, `async_process_statistics_for_meter` oraz `_ensure_recorder_running`.

### 💡 Nowe Sensory Decyzyjne i Automatyzacyjne (ROADMAP.md)
- **Sensor Taniej Strefy (`binary_sensor.energa_tania_strefa`):**
  - Determinuje w czasie rzeczywistym tańszą strefę (np. strefa pozaszczytowa T2 dla taryf G12/G12w/G12as/G13/C12a/C12b).
  - Pełne wsparcie dla polskich świąt ustawowych (`is_polish_holiday`) i weekendów.
  - Dynamiczne atrybuty encji: `current_zone`, `active_price_pln`, `next_zone`, `next_zone_change`, `hours_until_next_zone`.
  - Automatyczne odświeżanie o każdej pełnej godzinie (`async_track_time_change`).
- **Sensor Jakości Danych OSD (`sensor.energa_jakosc_danych`):**
  - Stan główny: "OK" (opóźnienie do 2 dni), "Opóźnione" (3–5 dni), "Braki" (>5 dni) lub "Brak danych".
  - Atrybuty: `remote_reading_active`, `days_lag`, `last_reading_date`, `status_msg`, `sqlite_records_count`.
  - Rozszerzenie parsowania odpowiedzi API Energa w `api.py` o datę ostatniego pomiaru i komunikat licznika.

### 🛠️ Poprawki CI/CD i Zgodności z Hassfest
- **`manifest.json`:** Dodano `energy` oraz `lovelace` do tablicy `after_dependencies` (wymóg walidatora `hassfest`). Wersja podniesiona do `1.8.1`.
- **`.github/workflows/release.yml`:** Dodano explicit uprawnienie `permissions: contents: write` dla tokenu GitHub Actions, naprawiając błąd 403 `Resource not accessible by integration` w akcji `softprops/action-gh-release@v2`.

### 🧪 Testy Jednostkowe
- Dodano `tests/test_services.py` (testy rejestracji usług, importu statystyk, fallbacków).
- Dodano `tests/test_decision_sensors.py` (testy stref tanich, świąt, countdownu, stanów jakości danych).
- 369/370 zaliczonych testów jednostkowych, 0 błędów lintera ruff.

## v1.8.0 (2026-09-13) — Modułowa Architektura Sensorów, Coordinator i Narzędzia CI/CD

### 🏗️ Modułowa Refaktoryzacja Architektury (`custom_components/energa_mobile`)
- **Wydzielenie `coordinator.py` (925 linii):**
  - Wyodrębniono `EnergaCoordinator` z monolitycznego pliku `sensor.py` do dedykowanego modułu `coordinator.py`.
  - Zachowano 100% wstecznej kompatybilności — `EnergaCoordinator` jest re-eksportowany z `sensor.py`.
- **Modułowy podział sensorów na katalog `sensors/`:**
  - `sensors/bank.py` (760 linii): `EnergaBankKwhSensor`, `EnergaBankZoneSensor`, `EnergaBankPlnSensor`, `EnergaBankLevelSensor`, `EnergaBankFlowSensor` oraz funkcja bilansowania `_fifo_bank_from_monthly`.
  - `sensors/bill.py` (635 linii): `EnergaBillForecastSensor`, `EnergaBillCurrentSensor`, `EnergaBillComponentSensor`.
  - `sensors/price.py` (330 linii): `EnergaPriceSensor`, `EnergaRceSensor`, `PseRceDynamicPriceSensor`, `PseRceArbitrageSpreadSensor`.
  - `sensors/live.py` (740 linii): `EnergaLiveSensor`, `EnergaProsumerBalanceSensor`, `EnergaFirstDataDateSensor`, `EnergaStatisticsSensor`, `EnergaInfoSensor`, `EnergaCostStatisticsSensor`, `EnergaSyntheticStatisticsSensor`, `EnergaAutoconsumptionSensor`.
- **Odchudzenie `sensor.py`:** Zmniejszono objętość pliku z **4 249 linii** do **1 069 linii** (~75% redukcji), pozostawiając jedynie funkcję `async_setup_entry` oraz fabrykę encji.

### 🌐 Tłumaczenia i Jakość Kodu
- **Pełna synchronizacja języka angielskiego (`en.json`):**
  - Uzupełniono brakujące sekcje `net_metering_survey`, `energy_dashboard`, `generate_dashboard` oraz przycisków encji.
  - Osiągnięto 100% parytetu kluczy pomiędzy `pl.json`, `en.json` i `strings.json`.
- **Naprawa brakującego importu typowania:**
  - Dodano brakujący import `from typing import Any` w `sensor.py` dla `PseRceDynamicPriceSensor.extra_state_attributes`.

### 🛠️ Infrastruktura i CI/CD
- **`requirements_test.txt`:** Skonsolidowano wszystkie zależności testowe (`pytest`, `pytest-asyncio`, `pytest-cov`, `aiohttp`, `voluptuous`, `ruff`).
- **Raportowanie pokrycia testami (coverage):** Zintegrowano `--cov` w workflow GitHub Actions `tests.yml`.
- **Szablony GitHub Issue:** Dodano ustrukturyzowane szablony zgłaszania błędów (`bug_report.yml`) oraz propozycji funkcji (`feature_request.yml`).
- **Plik `CODEOWNERS`:** Zdefiniowano automatyczną odpowiedzialność za kod w `.github/CODEOWNERS`.

## v1.7.2 (2026-09-13) — Ergonomia Pulpitu Lovelace, Ikona Paska Bocznego i Automatyzacje Cen Dynamicznych RCE

### 📊 Ergonomia i Dopracowanie Pulpitu Lovelace (`dashboard_generator.py`)
- **Oficjalna ikona paska bocznego (`mdi:lightning-bolt-circle`):**
  - Zamieniono rzadziej wspieraną ikonę walutową na wyrazistą, uniwersalną ikonę energii `mdi:lightning-bolt-circle`.
  - Zagwarantowano dynamiczną aktualizację istniejących wpisów w `.storage/lovelace_dashboards` przy regeneracji pulpitów (ikona, tytuł i widoczność na pasku bocznym są natychmiast uaktualniane).
- **Wyeliminowanie podwójnego nagłówka na karcie tytułowej:**
  - Całkowicie usunięto atrybut `title` z karty typu `markdown`, co zlikwidowało powtarzający się nagłówek Lovelace nad treścią karty.
  - Zmniejszono rozmiar nagłówka z przeładowanego `##` do czytelnego, zwartego H3 (`### 🏡 {Nazwa punktu}`).
  - Podtytuł z metadanymi (miejscowość, taryfa, system rozliczeń, numer licznika) ujednolicono w jednej zwięzłej linii ze separatorami `•`.
- **Kompaktowe i czytelne tytuły kart:**
  - Usunięto długie adresy powtarzane w tytułach kart (np. zmiana `⚡ Rozliczenie Finansowe Energa (Wiśniowa)` na eleganckie `⚡ Rozliczenie Finansowe`), zapobiegając ucinaniu i zawijaniu tekstu na urządzeniach mobilnych.
- **Dedykowana Karta Cen Dynamicznych RCE i Arbitrażu BESS:**
  - Dla liczników z taryfami dynamicznymi / net-billingiem automatycznie generowana jest dedykowana karta prezentująca:
    - Miesięczną cenę referencyjną RCEm oraz wycenę zasilenia depozytu brutto
    - Bieżącą 15-minutową cenę RCE z rynku bilansującego PSE
    - Spread arbitrażowy brutto z uwzględnieniem sprawności magazynu
    - Sensory binarne okien ładowania (`CHARGE`), rozładowania (`DISCHARGE`) oraz alertu cen ujemnych.

### ⚡ Szablony Automatyzacji (Blueprints) dla Home Assistant
- Wprowadzono 3 gotowe szablony Blueprints (dostępne bezpośrednio w HA):
  - `energa_rce_negative_price_protection.yaml`: Natychmiastowa reakcja na ujemne ceny energii RCE (< 0 zł/kWh) — powiadomienie push oraz automatyczne załączenie odbiorników podnoszących autokonsumpcję (grzanie CWU pompą ciepła, ładowanie samochodu elektrycznego, klimatyzacja).
  - `energa_bess_arbitrage_charging.yaml`: Automatyczne ładowanie domowego magazynu energii lub samochodu EV w najtańszym oknie doby wyznaczonym przez silnik `ArbitrageEngine`.
  - `energa_bess_peak_discharge.yaml`: Optymalizacja rozładowania magazynu w popołudniowo-wieczornym szczycie najwyższych cen PSE.
- Nowa dokumentacja [`docs/AUTOMATIONS.md`](docs/AUTOMATIONS.md) ze studium przypadków dla pomp ciepła i wallboxa BMW.

### 📚 Nowe Przewodniki i Baza Wiedzy w Dokumentacji
- **Stabilność, Restarty HA i Integracje Chmurowe ([`docs/STABILITY_AND_INTEGRATIONS.md`](docs/STABILITY_AND_INTEGRATIONS.md)):**
  - Dogłębne studium kaskady restartowej na zewnętrznych chmurach API (np. SolisCloud) podczas intensywnych prac wdrożeniowych.
  - Opis architektury *Resilience-by-Design* w integracji Energa (SQLite WAL, asynchroniczność bez blokowania pętli `MainThread`, odporność offline).
  - Rekomendacje dla administratorów HA dotyczące higieny restartów oraz wdrożeń rozwiązań lokalnych (Modbus TCP/RS-485).
- **Mapa Rozwoju i Katalog Propozycji ([`docs/ROADMAP.md`](docs/ROADMAP.md)):**
  - Usystematyzowany katalog przyszłych usprawnień (zaawansowane wykresy ApexCharts, sensory binarne rekomendacji, integracja z natywnym panelem HA Energy).
  - Zaproszenie społeczności do zgłaszania uwag i propozycji w GitHub Issues.
- **Wnioski Wdrożeniowe w [`docs/DASHBOARD.md`](docs/DASHBOARD.md):**
  - Dodano sekcję podsumowującą wnioski z wdrożenia na 5 różnych środowiskach (konsument G11, prosument net-metering, prosument net-billing RCE).

---

## v1.7.1 (2026-09-13) — Autonomiczny Silnik Przyrostowej Syntezy Magazynu Syntetycznego (Self-Healing)

### 🛡️ Niezawodność i Ciągłość Danych w Panelu Energia (`synthetic_storage.py`)
- **Silnik syntezy przyrostowej (Incremental Self-Healing Engine):**
  - Wyeliminowano sztywny bezpiecznik pomijający obliczenia magazynu syntetycznego, gdy w bazie istniały jakiekolwiek wcześniejsze rekordy.
  - Wprowadzono automatyczne wykrywanie luk czasowych (`max_raw_ts > max_synth_ts`): po każdej dobie OSD brakujące godziny są automatycznie dosyntetyzowywane z zachowaniem idealnej monotoniczności sum skumulowanych (`base_sums`).
  - Każda aktualizacja integracji samoczynnie uzupełnia brakujące dni w przeszłości bez konieczności jakiejkolwiek ingerencji użytkownika.

---

## v1.7.0 (2026-09-13) — Precyzyjne Kalibracje Faktury Lipcowej i Obsługa Wielu Liczników

### 💎 Dokładność Finansowa
- Automatyczne wstrzykiwanie stanów bazowych liczników (`balance_baseline_*`) na granicy okresów rozliczeniowych (31 lipca).
- 100% zgodności co do grosza z oficjalnymi fakturami Energa Obrót.

---

### 🚀 Główne Ulepszenia i Optymalizacje Wydajności
- **Przeniesienie obliczeń profilu godzinowego (`HourlyProfileForecaster`) do wątku roboczego (`async_add_executor_job`):**
  - Wyeliminowano 3.7-sekundowe blokowanie głównej pętli zdarzeń Home Assistanta (`MainThread`).
  - Prognozy WAL są teraz obliczane asynchronicznie w tle przez koordynatora i natychmiast serwowane z pamięci podręcznej RAM (`_profile_forecast_cache`) w czasie `< 0.0001s`.
  - Całkowicie wyeliminowano ostrzeżenia HA: *"Updating sensor took longer than the scheduled update interval"* oraz *"Blocking call inside the event loop"*.
- **Inteligentne buforowanie zapytań do PSE OIRE (RCE):**
  - Wprowadzono 2-godzinny bufor zapytań do `api.raporty.pse.pl` z automatycznym odświeżaniem po godz. 14:00 (moment publikacji cen na dzień następny).
  - Skrócono timeout sieciowy do 8s (w tym `connect: 4s`).
  - Przejściowe błędy połączeń i DNS PSE zostały przeniesione do poziomu `DEBUG`, eliminując zaśmiecanie logów systemowych Home Assistant w trakcie przerw technicznych po stronie PSE.
- **Buforowanie zamkniętych miesięcy historycznych w bazie Recorder:**
  - Koordynator trzyma w pamięci RAM dane zamkniętych miesięcy z przeszłości, eliminując 13 powtarzalnych zapytań SQL do bazy SQLite w każdym 15-minutowym cyklu odpytywania.
- **Warunkowe rejestrowanie encji autokonsumpcji:**
  - 8 encji autokonsumpcji i bilansowania PV (`today_kwh`, `realne_zuzycie_domu_dzis`, `savings_mtd_pln`, etc.) jest rejestrowanych wyłącznie wtedy, gdy użytkownik skonfiguruje encję falownika (`CONF_INVERTER_ENERGY_ENTITY`) oraz licznik posiada status prosumencki (`is_export_prosumer`). Zapobiega to powstawaniu martwych encji `unknown`/`unavailable` na licznikach czysto konsumpcyjnych.
- **Odporne wykrywanie daty początkowej dla aktywacji w drugiej połowie miesiąca (`api.py`):**
  - W algorytmie `async_find_first_data_date` dodano sprawdzanie końca miesiąca (dzień 28), eliminując ryzyko pominięcia miesiąca instalacji w przypadku liczników uruchomionych po 15. dniu miesiąca.

### 🧪 Testy Jednostkowe
- 350 w pełni przechodzących testów jednostkowych (w czasie 0.23s).

---

## v1.6.8 (2026-09-12) — Normalizacja Slugów Encji (HA 2027.2 Ready) i Odporność na Puste Rejestry Eksportu

### ⚡ Nowości i Usprawnienia (Features & Improvements)
- **Rygorystyczna normalizacja slugów encji do małych liter (`s_slug`):**
  - Usunięto ostrzeżenia Home Assistant dotyczące nieprawidłowych wielkich liter w `entity_id` (np. `sensor.energa_V705048953698419_...`).
  - Wszystkie identyfikatory generowane przez przycisk autokonfiguracji Panelu Energia (`button.py`), generator kart Lovelace (`dashboard_generator.py`) oraz statystyki syntetycznego magazynu energii (`synthetic_storage.py`) stosują wymuszone małe litery, zachowując oryginalne oznaczenie seryjne w nazwach przyjaznych użytkownikowi.
  - Pełna zgodność ze standardami Home Assistant przygotowująca integrację na wymogi wydania 2027.2.
- **Zgodność z wytycznymi statystyk Home Assistant 2026.11:**
  - W `async_import_statistics` dodano wymagane parametry `mean_type = StatisticMeanType.NONE` oraz `unit_class = "energy"` w `StatisticMetaData`, eliminując ostrzeżenia z logów systemowych.
- **Odporność na brak danych eksportu u prosumentów (`total_minus: null`):**
  - Obsłużono przypadek nowo przyłączonych instalacji PV oraz demonstracyjnych kont OSD (np. oficjalne konto demo Energa Operator `amiEOP@energa-operator.pl`), gdzie rejestry eksportowe zwracają wartość pustą. Encje eksportowe i dzienne produkcje inicjalizują się bezpiecznie z wartością `0.0 kWh` zamiast błędu `unavailable`.

---

## v1.6.7 (2026-09-12) — Oficjalny Standard Pulpitu 'Centrum Rozliczeń' (Layout Agrestowa 4)

### 🎨 Nowy Wygląd i Ujednolicenie Pulpitu Lovelace (`/energa-rachunek`)
- **Wdrożenie nowoczesnego, czytelnego layoutu Centrum Rozliczeń:**
  - Przyjęto dopracowany, profesjonalny układ z instalacji Agrestowa 4 jako oficjalny standard integracji generowany automatycznie przy pierwszej instalacji oraz po kliknięciu przycisku `Wygeneruj Pulpit`.
  - Spójna organizacja kart:
    - *Nagłówek i status instalacji:* numer licznika, taryfa, typ rozliczeń (Net-metering vs Net-billing vs Konsument).
    - *Bieżące koszty i prognozy:* kafelki dotychczasowego rachunku brutto i prognozy zamknięcia miesiąca z rozbiciem na energię czynną i opłaty dystrybucyjne.
    - *Wirtualny magazyn / Depozyt prosumencki:* dedykowana sekcja w zależności od systemu rozliczeń (saldo kWh z podziałem L1/L2 lub depozyt wartościowy PLN z kursem RCEm).
    - *Autokonsumpcja PV i BESS:* wykresy bilansowania godzinowego oraz spreadów arbitrażowych baterii.
- **Wsparcie dla środowisk wielolicznikowych:**
  - Automatyczne generowanie odrębnych sekcji dla każdego licznika przypisanego do konta w portalu Mój Licznik.

---

## v1.6.6 (2026-09-12) — Odporny Parser Tabelaryczny RCEm na HTMLParser (PSE OIRE)

### 🐛 Poprawki Błędów i Odporność (Bug Fixes & Resilience)
- **Zaawansowany parser HTML dla cen RCEm:**
  - Zastąpiono uproszczony parser oparty na pojedynczym wyrażeniu regularnym dedykowaną implementacją bazującą na `html.parser.HTMLParser`.
  - Rozwiązano problem gubienia rekordów tabeli na stronach PSE OIRE (wcześniej odczytywano jedynie ~10 z ~55 pozycji).
  - Dodano pełne wsparcie dla korekt cenowych — parser poprawnie rozpoznaje etykiety `"skorygowana RCEm"` oraz `"RCEm korekta"`.
  - Wyeliminowano błąd przesunięcia wierszy (gdzie cena z jednego miesiąca była omyłkowo przypisywana do nazwy miesiąca z nagłówka tabeli).

---

## v1.6.5 (2026-09-12) — Precyzyjna Klasyfikacja Net-billing vs Net-metering i Wsparcie G11 Net-billing

### ⚡ Nowości i Poprawki (Features & Bug Fixes)
- **Bezkompromisowa klasyfikacja profili prosumenckich:**
  - Rozwiązano problem błędnego kwalifikowania prosumentów w taryfie jednotaryfowej G11 na nowych zasadach (Net-billing) jako starego Net-meteringu.
  - Precyzyjne rozróżnienie: współczynnik `>= 0.7` aktywuje mechanizmy Net-meteringu (magazyn kWh w Panelu Energia), natomiast współczynnik `0.0` aktywuje depozyt wartościowy PLN (Net-billing z RCEm).
  - Zweryfikowano zgodność formuł rozliczeniowych co do grosza na rzeczywistych fakturach OSD (m.in. instalacja Bursztynowa).

---

## v1.6.4 (2026-09-12) — Dyskretny 15-minutowy Interwał Odpytywania API

### ⚡ Usprawnienia Wydajności (Performance Improvements)
- **Dopasowanie interwału odpytywania API do cyklu OSD:**
  - Zmieniono domyślny interwał koordynatora na 15 minut, harmonizując działanie integracji z cyklem publikacji danych przez Energa Operator.
  - Zmniejszono narzut sieciowy i wyeliminowano ryzyko przekroczenia limitów zapytań (rate limiting) na serwerach Energa Mój Licznik.

---

## v1.6.3 (2026-09-12) — Stabilne Wydanie Produkcyjne (Natywny Magazyn Wirtualny, Nowy Format Panelu Energia, Ochrona Bazy Danych)

### 🌟 Oficjalne Wydanie Stabilne (Production Release)
- **Kompletna stabilizacja i weryfikacja wielośrodowiskowa:** Wydanie v1.6.3 zostało w 100% przetestowane, zweryfikowane i wdrożone zarówno na instancjach laboratoryjnych, jak i na produkcyjnym Home Assistant.
- **Pełne wsparcie dla net-meteringu (opusty 0.8 / 0.7) i net-billingu:** Wirtualny magazyn energii jako natywna bateria w oficjalnym Panelu Energia Home Assistant (`/energy`), syntetyczna sieć rozliczana zgodnie z przepisami OZE, bez podwajania kosztów poboru z opustu.
- **Automatyczna ochrona przed spadkami sum (Anti-Spike Protection):** Wbudowane klamrowanie monotoniczności w `RecorderAdapter` oraz kotwiczenie w `EnergaDataUpdater` gwarantujące brak ujemnych anomalii i pików na wykresach długoterminowych.
- **Nowy format źródeł sieciowych (GridSourceType):** Bezbłędna integracja z najnowszymi wersjami Home Assistant bez błędów "Brak sieci".
- **Zaproszenie społeczności do testów:** Dodano sekcję w dokumentacji dotyczącą testowania pozostałych taryf (G13 trójstrefowa, G12, C, instalacje > 10 kWp).

## v1.6.2 (2026-09-12) — Płaski Format Źródeł Sieci (GridSourceType), Zabezpieczenie przed Resetem Sum i Ochrona Spadków

### ⚡ Nowości i Usprawnienia (Features & Improvements)
- **Nowoczesny, płaski format źródeł sieci w Panelu Energia (`button.py`):**
  Zaktualizowano generator konfiguracji Panelu Energia z przestarzałego schematu zagnieżdżonego (`LegacyGridSourceType` z tablicami `flow_from` / `flow_to`) na obowiązujący w nowszych wersjach Home Assistant format płaski (`GridSourceType`). Każde przyłącze i strefa taryfowa (np. Strefa 1 / Dzień oraz Strefa 2 / Noc w G12/G12w) stanowi teraz samodzielny obiekt z bezpośrednio przypisanym `stat_energy_from` oraz `stat_energy_to`. Rozwiązuje to całkowicie problem komunikatu **"Brak sieci"** pojawiającego się na frontendzie `/energy`.
- **Kotwiczenie sum w `EnergaDataUpdater` (`data_updater.py`):**
  Metoda `gather_stats_for_sensor` przyjmuje teraz parametr `last_known_sum`. W przypadku braku pobranych wcześniej statystyk z bazy w pamięci podręcznej (np. przy pierwszym odświeżeniu po starcie systemu, zanim recorder zakończy inicjalizację), koordynator nie resetuje obliczeń do zera, lecz kotwiczy sumę na najwyższej znanej wartości sensora.
- **Twarda ochrona przed spadkami sum w `RecorderAdapter` (`recorder_adapter.py`):**
  Dodano bufor `_highest_sums` oraz metodę `seed_last_sum`. `RecorderAdapter` weryfikuje każdą importowaną paczkę statystyk — jeśli którakolwiek suma miałaby spaść poniżej dotychczasowej najwyższej zarejestrowanej wartości, suma jest automatycznie klamrowana w górę (`running_sum = max(running_sum, cand_sum)`). Gwarantuje to absolutną monotoniczność i uniemożliwia powstanie ujemnych pików energii (np. -13.7 MWh) na wykresach.

### 🐛 Poprawki Błędów (Bug Fixes)
- **Naprawa błędu `NameError: name 'recorder_adapter' is not defined` (`__init__.py`):**
  Zmienna `recorder_adapter` została przeniesiona do zakresu zewnętrznego funkcji `_import_meter_history`, naprawiając błąd asynchronicznego generowania historii magazynu dla niektórych liczników.
- **Pobieranie znacznika czasu w `_get_smart_start_date` (`sensor.py`):**
  W zapytaniach `get_last_statistics` dodano wymagany parametr `"start"` do zbioru `types`, co umożliwia poprawną detekcję daty ostatniego rekordu i płynne pobieranie tylko nowych godzin bez luk czasowych.
- **Aktualizacja pamięci podręcznej statystyk w sensorze (`sensor.py`):**
  Po każdym pomyślnym zaimportowaniu statystyk przez `EnergaStatisticsSensor`, słownik koordynatora `_pre_fetched_stats` oraz adapter rekordera są natychmiast uaktualniane o najnowszą wartość sumy i znacznika czasu.

### 🧪 Testy Jednostkowe
- 344 w pełni przechodzące testy jednostkowe (dodano testy kotwiczenia `last_known_sum` oraz klamrowania spadków sum w `RecorderAdapter`).


### ⚡ Nowości i Usprawnienia (Features & Improvements)
- **Błyskawiczna synteza statystyk magazynu z bazy Recorder (`synthetic_storage.py`):** Dodano funkcję `async_synthesize_storage_from_recorder`. Dla istniejących instalacji aktualizowanych do v1.6.0+ (gdzie pobieranie historii było już wcześniej oznaczone jako zakończone) integracja natychmiast generuje 730 dni historii wirtualnego magazynu bezpośrednio z lokalnej bazy danych Home Assistant w ułamku sekundy, bez konieczności wykonywania setek zapytań HTTP do API Energi.
- **Automatyczny backfill przy kliknięciu przycisku (`button.py`):** Wciśnięcie przycisku `Skonfiguruj Panel Energia` natychmiast sprawdza i dopełnia statystyki syntetycznego magazynu w bazie Recorder, gwarantując natychmiastową widoczność wykresów w `/energy`.

### 🐛 Poprawki Błędów (Bug Fixes)
- **Obsługa liczników 1-strefowych G11 (`sensor.py`):** Naprawiono krytyczny błąd `UnboundLocalError: cannot access local variable 'init_l1'` w sensorze wirtualnego magazynu (`EnergaBankKwhSensor`). W licznikach jednotaryfowych (G11, np. instalacje PV z taryfą całodobową) zmienne `init_l1` i `init_l2` były inicjalizowane wyłącznie wewnątrz bloku `if self._has_zones:`, co blokowało odświeżanie encji przez koordynatora i generowało błędy w logach.
- **Uniwersalna dostępność przycisku autokonfiguracji (`button.py`):** Przycisk `Skonfiguruj Panel Energia` (`button.energa_{serial}_skonfiguruj_panel_energia`) jest teraz rejestrowany bezwarunkowo dla każdego licznika, eliminując problem pomijania przycisku na instalacjach, gdzie flaga `is_prosumer` lub sumaryczny eksport były zerowe w trakcie początkowej synchronizacji.
- **Wzmocniona detekcja prosumencka (`__init__.py`):** Rozszerzono warunek sprawdzania statusu prosumenta dla powiadomień systemowych oraz wstecznego generowania statystyk magazynu o analizę punktów eksportowych i kodów OBIS minus.

### 🧪 Testy Jednostkowe
- 342 w pełni przechodzące testy jednostkowe (dodano testy dla syntezy z bazy Recorder oraz regresyjny dla taryf G11 bez stref).

## v1.6.0 (2026-09-12) — Natywny Wirtualny Magazyn Energii (Net-Metering 0.8/0.7), Onboarding Survey & Autokonfigurator Panelu Energia

### 🔋 Natywny Model Wirtualnego Magazynu Energii (Net-metering) w Panelu Energia (`/energy`)
- **Matematyczny silnik bilansowania (`synthetic_storage.py`):** Zgodnie z polską ustawą o OZE (art. 4 ust. 1 i 11), nadwyżki wprowadzone do sieci dzielone są precyzyjnie na:
  1. *Ładowanie Wirtualnego Magazynu:* $Export \times 0.8$ (dla instalacji $\le 10$ kW) lub $\times 0.7$ (powyżej 10 kW).
  2. *Prowizja Rzeczowa OSD:* $Export \times 0.2$ (lub $0.3$) jako bezpłatne oddanie do sieci (0 zł/kWh).
  3. *Rozładowanie Magazynu:* kompensacja poboru z dostępnego salda banku za 0 zł/kWh opłaty zmiennej.
  4. *Pobór Netto z Sieci:* wyłącznie nadwyżka poboru ponad stan magazynu, taryfikowana pełną stawką brutto.
- **Obsługa taryf jednostrefowych (G11) i dwustrefowych (G12/G12w):** W taryfach wielostrefowych zaimplementowano bilansowanie między strefą L1 (Dzień) i L2 (Noc) zgodnie z przepisami OZE (najpierw rozładowanie własnej strefy, następnie dopełnienie z drugiej strefy).
- **Wyeliminowanie przekłamań Energy Dashboard:** Rozwiązano fundamentalny problem Panelu Energia w Home Assistant, który przy net-meteringu drastycznie zaniżał wskaźniki samowystarczalności oraz zawyżał koszty poboru energii.

### 📋 Ankieta Konfiguracyjna przy Pierwszej Instalacji (Onboarding Survey)
- **Kreator dodawania integracji (`config_flow.py`):** Przy wyborze starych zasad prosumenckich (net-metering) pojawia się dedykowany krok ankiety z wyjaśnieniem zasad rozliczeń.
- **Wybór mocy mikroinstalacji:**
  - $\le 10$ kW (współczynnik 0.8: odbiór 80%, prowizja 20%).
  - $> 10$ kW (współczynnik 0.7: odbiór 70%, prowizja 30%).
- **Wybór modelu prezentacji:**
  - *Wirtualny Magazyn Energii (Rekomendowany)* — syntetyczny magazyn w Panelu Energia, prowizja jako bezpłatny eksport, pobór z magazynu za 0 zł.
  - *Model Tradycyjny* — surowy licznik fizyczny (całość importu i eksportu w sekcji Sieć).
- **Zarządzanie z poziomu Opcji:** Użytkownik w dowolnym momencie może zmienić parametry w `Opcje -> Panel Energia i Wirtualny Magazyn`.

### 🔘 Przycisk Automatycznej Konfiguracji Panelu Energia (`button.py`)
- **Przycisk `Skonfiguruj Panel Energia`:** Dostępny na karcie urządzenia licznika (`button.energa_{serial}_skonfiguruj_panel_energia`).
- **Zero-click Setup:** Jednym kliknięciem bezpośrednio konfiguruje Home Assistant Energy Preferences (`.storage/energy`) za pośrednictwem natywnego API `EnergyManager.async_update`, bez konieczności restartu Home Assistanta i bez ręcznego wklejania encji.
- **Automatyczne wiązanie encji:**
  - Baterie: `syntetyczny_magazyn_l1` / `l2` (lub `syntetyczny_magazyn`).
  - Sieć: `syntetyczna_siec_pobor` z cennikami oraz `syntetyczna_siec_oddanie` z ceną 0.0 zł.
  - Fotowoltaika: automatycznie dołącza skonfigurowany falownik (`inverter_energy_entity`).
  - Powiadomienie systemowe po pomyślnej konfiguracji.

### 📊 Wsteczny Import i Ciągłość Statystyk (`__init__.py`, `sensor.py`)
- **Automatyczny backfill 730 dni:** Historia wirtualnego magazynu generowana automatycznie podczas początkowego pobierania danych.
- **Kotwiczenie sum (Anchor-safe):** Nowe odczyty kontynuują skumulowaną sumę bez resetów do zera, chroniąc wykresy słupkowe przed wielomegowatowymi skokami.
- **Ochrona przed duplikacją:** Właściwość `native_value = None` w `EnergaSyntheticStatisticsSensor` zabezpiecza przed generowaniem konkurencyjnych statystyk przez silnik recorder HA.

### 🧪 Testy Jednostkowe
- 340 przechodzących testów jednostkowych (w tym 9 dedykowanych testów silnika bilansowania, konfiguratora panelu energia i ankiety onboardingowej).


### 💰 Transparentne Ceny Energii i Rozbicie Składników
- **Wzbogacone atrybuty cen poboru (`sensor.energa_*_cena_poboru*`):** Dodano atrybuty `stawka_calkowita_brutto`, `strefa`, `opis` oraz szczegółowe wskazówki wyjaśniające, że stawka używana w Panelu Energia powinna zawierać pełny koszt zmienny (energia czynna + opłaty dystrybucyjne zmienne + podatki/VAT 23%).
- **Doprecyzowanie formularzy konfiguracji (`strings.json`, `translations/pl.json`):** Zaktualizowano etykiety pól cenowych, eliminując wątpliwości użytkowników i zapobiegając zaniżaniu prognoz kosztów w natywnym Panelu Energia (`/energy`).

### 🔬 Eksperymentalny Model Syntetyczny Wirtualnego Magazynu
- Opracowanie i laboratoryjna weryfikacja (Lab 123 — Wiśniowa) modelu syntetycznych przepływów dla natywnego Panelu Energia, który zachowuje 100% spójności zużycia własnego budynku przy pełnej reprezentacji akumulatora (baterii).
- Zestaw 331 przechodzących testów jednostkowych.

## v1.5.0 (2026-09-11) — Automatyczny Bank Energii (Zero-Config FIFO) & Kalibracja Datą Faktury

### 🤖 Automatyczny Wirtualny Magazyn Energii z API Energa (Zero-Config Out-of-the-Box)
- **Koniec z ręczną konfiguracją banku dla nowych użytkowników:** Integracja automatycznie pobiera agregacje miesięczne (`type="YEAR"` z `/resources/mchart`) z oficjalnego API Energi dla ostatnich 12–14 miesięcy.
- **Natychmiastowe saldo magazynu:** Silnik FIFO oblicza realny stan magazynu wirtualnego oraz podział na strefy L1 (Dzień) i L2 (Noc) natychmiast po instalacji, bez konieczności wpisywania jakichkolwiek stanów początkowych czy poszukiwania faktury.
- **Priorytetyzacja źródeł:** Integracja używa następującej hierarchii:
  1. Tryb daty faktury (`settlement_date` + stan początkowy z faktury).
  2. Tryb automatyczny FIFO z API Energi (`fifo_12m_api`) – zero-config!
  3. Tryb bazowych odczytów licznika (`balance_baseline_import/export`).

### 📅 Uproszczona Kalibracja Datą Faktury (`settlement_date`)
- **Brak konieczności spisywania 5-cyfrowych stanów licznika:** Użytkownik, który chce dokładnie skalibrować magazyn pod ostatnią fakturę, podaje jedynie datę odcięcia z faktury (np. `2024-05-31`) oraz wykazany na niej stan magazynu (L1 i L2).
- **Automatyczne zbilansowanie okresu po fakturze:** Integracja sumuje pobór i oddanie wyłącznie z miesięcy następujących po dacie faktury i powiększa o nie stan z faktury.

### 📊 Dashboard Lovelace `/energa-rachunek` & Multimeter Support
- **Wsparcie dla wielu liczników:** Pulpit rozliczeniowy automatycznie łączy widoki z wielu liczników/punktów PPE w zakładkach bez wzajemnego nadpisywania plików konfiguracyjnych Lovelace.
- **Aktywne prognozy rachunków od pierwszego uruchomienia:** Domyślne włączenie flagi `enable_auto_settlement: True` zapewnia dostępność encji prognoz (`prognoza_rachunku`, `dotychczasowy_rachunek`, `koszt_brutto_mtd`) i eliminuje błędy "Nie znaleziono encji" na kafelkach dashboardu.

### 🧪 Testy Jednostkowe
- 330 przechodzących testów jednostkowych, w tym dedykowane testy silnika `bank_from_invoice_date` i fuzji dashboardów.

## v1.4.1 (2026-09-11) — Odporność Onboardingu i Auto-Backfillu (Resilient Onboarding & Provisioning)

### 🔄 Odporność na Przerwanie i Restart HA podczas 730-dniowego Auto-Backfillu (`__init__.py`)
- **Eliminacja błędu pomijania historii:** Wcześniejsza funkcja `_has_any_panel_statistics` sprawdzała jedynie ostatni punkt telemetryczny (`get_last_statistics`), który po pierwszym odświeżeniu koordynatora zawierał bieżący dzień. W efekcie każdy restart Home Assistanta w trakcie lub przed backfillem trwale blokował import 730 dni historii.
- **Weryfikacja okna historycznego `_has_history_statistics`:** Sprawdzanie obecności danych w oknie `[target_start, target_start + 60 dni]` zamiast pojedynczego najnowszego punktu.
- **Trwały znacznik `auto_backfill_completed`:** Zapis stanu w `entry.data`, zapobiegający zbędnym zapytaniom do bazy po pełnym zaimportowaniu.

### 📢 Notyfikacje Postępu i Gotowości (`__init__.py`)
- **Pasek postępu importu:** Informowanie użytkownika co 30 przetworzonych dni o postępie procentowym i szacowanym pozostałym czasie.
- **Ściągawka konfiguracji Panelu Energia:** Powiadomienie końcowe zawiera gotowe identyfikatory encji sumarycznych (`sensor.energa_[meter_id]_pobor_energia_suma_kwh`, `sensor.energa_[meter_id]_oddanie_energia_suma_kwh`) oraz bezpośredni odnośnik do pulpitu Lovelace.

### 📊 Automatyczne Dodanie Pulpitu Lovelace do Paska HA (`__init__.py`, `dashboard.py`)
- Wywołanie `async_provision_dashboard(hass, entry)` natychmiast przy konfiguracji i ładowaniu integracji, dzięki czemu dashboard `/energa-rachunek` pojawia się w menu bocznym bez konieczności ręcznych akcji użytkownika.

### 🛡️ Odporność Kreatora Konfiguracji na Timeouty API i Opcja Braku PV (`config_flow.py`, `settlement.py`, translacje)
- **Krok awaryjny `system_fallback`:** W przypadku powolnej odpowiedzi lub błędu API Energi podczas wykrywania liczników, użytkownik otrzymuje czytelny formularz awaryjny zamiast błędu konfiguracji lub nieprawidłowej domyślnej autokonfiguracji.
- **Opcja "Nie posiadam fotowoltaiki":** Dodano opcję `brak` (współczynnik `0.0`) w kreatorze, chroniąc zwykłych odbiorców przed automatycznym przypisaniem do net-meteringu (współczynnik 0.8).

### 🧪 Nowe Testy Jednostkowe (`tests/test_onboarding_resilience.py`)
- 10 nowych testów jednostkowych pokrywających współczynnik `brak`, fallback w config flow, mechanizm weryfikacji okna statystyk oraz proces wznowienia auto-backfillu.
- Łącznie 328 testów jednostkowych przechodzi pomyślnie.

## v1.4.0 (2026-09-10) — Podwójny Wirtualny Magazyn Energii L1/L2 (Dual-Zone Net-Metering FIFO)

### 🏭 Izolacja Stref Rozliczeniowych L1 (Dzień) i L2 (Noc) w Net-Meteringu (`settlement.py`, `sensor.py`)
- **Faktyczne odwzorowanie zasad OSD Energa:** Potwierdzono na podstawie rzeczywistej faktury rozliczeniowej (`FES/00042`), że w taryfach wielostrefowych (G12, G12w) depozyt energii w starym systemie (net-metering / opusty 0.8) prowadzony jest w dwóch całkowicie odrębnych magazynach:
  - **L1 (Dzień / Szczyt):** Nadwyżki z generacji dziennej zasilają wyłącznie pulę L1 i kompensują jedynie pobór w strefie dziennej.
  - **L2 (Noc / Poza szczytem):** Nadwyżki generacji poza szczytem (np. weekendy G12w) zasilają wyłącznie pulę L2 i kompensują jedynie pobór nocny/weekendowy.
  - Brak wzajemnego subsydiowania stref (brak transferu energii między L1 i L2 przy deficycie w jednej ze stref).
- **Nowy silnik rozliczeniowy `fifo_dual_zone_kwh_bank` & `run_dual_zone_fifo_net_metering`:**
  - Niezależne kolejki FIFO ze ścisłą datą ważności (12 miesięcy dla każdego depozytu strefowego).
  - Obliczanie sumarycznego salda banku oraz precyzyjnych składowych `bank_kwh_l1` i `bank_kwh_l2`.

### 📊 Dedykowane Sensory i Atrybuty Home Assistant
- **Nowe encje strefowe:**
  - `sensor.energa_[meter_id]_bank_wirtualny_l1_dzien_kwh` — saldo magazynu w strefie dziennej / szczytowej.
  - `sensor.energa_[meter_id]_bank_wirtualny_l2_noc_kwh` — saldo magazynu w strefie nocnej / pozaszczytowej.
- **Wzbogacone atrybuty encji głównej `sensor.energa_[meter_id]_bank_wirtualny_kwh`:**
  - Dodano `bank_kwh_l1`, `bank_kwh_l2`, `bank_l1_share_pct`, `bank_l2_share_pct`, informujące o strukturze zmagazynowanej energii.

### ⚙️ Konfiguracja Początkowa dla Nowych Użytkowników (`config_flow.py`, `const.py`)
- W opcjach integracji (`OptionsFlow`) dla taryf strefowych dodano dedykowane pola:
  - `bank_initial_kwh_l1` (Stan początkowy magazynu L1 z faktury)
  - `bank_initial_kwh_l2` (Stan początkowy magazynu L2 z faktury)
  - Automatyczne sumowanie do ogólnego `bank_initial_kwh` z pełną kompatybilnością wsteczną.

### 🧪 Testy Jednostkowe (`tests/test_settlement.py`, `tests/test_settlement_pure.py`)
- Test przejścia i rozliczenia zgodnego z fakturą Wiśniowa FES/00042 (1358 kWh -> 2456.2 kWh).
- Test izolacji strefowej (brak transferu nadwyżek L1 do deficytu L2).
- Test czystego silnika domenowego `run_dual_zone_fifo_net_metering`.

## v1.3.4 (2026-09-10) — Częściowe Pokrycie Historii Magazynu (Graceful FIFO Partial Coverage)

### 🔋 Poziom Napełnienia Magazynu dla Krótszej Historii (`const.py`, `sensor.py`)
- **Obniżenie progu wymaganej historii:** Zmniejszono stałą `FIFO_MIN_COVERAGE_MONTHS` z 11 do 3 miesięcy.
- **Odblokowanie kafelka `sensor.energa_[serial]_magazyn_poziom`:** Encja poziomu napełnienia magazynu (%) wylicza się i jest w pełni aktywna dla świeżych importów (np. od wiosny bieżącego roku), cesji umów oraz instalacji działających krócej niż rok, eliminując stan `unknown`.
- **Nowy atrybut telemetrii `coverage_status`:** Informuje o skali pokrycia rocznego horyzontu bilansowania (np. `partial (7/12m)` lub `full (12m)`).
- **Zaktualizowana nota `fifo_note`:** Wyjaśnia minimalny wymagany okres 3 miesięcy do wiarygodnej estymacji wkładów i salda.

### 🧪 Testy Jednostkowe (`tests/test_settlement.py`, `tests/test_sensor.py`)
- Dodano test `test_graceful_partial_coverage` sprawdzający estymację poziomu magazynu dla częściowego horyzontu (4 miesiące).
- Dodano test `test_fifo_bank_from_monthly_partial_coverage` weryfikujący próg graniczny (2 miesiące -> None, 3 miesiące -> estymacja aktywna).

## v1.3.3 (2026-09-10) — Wsparcie Wariantów Prefiksów PPE i Poprawki Autokonsumpcji

### ☀️ Odporność Identyfikatorów Odczytów (`autoconsumption.py`)
- Dodano elastyczną obsługę prefiksów PPE (`PL_...` vs numeryczne) oraz fallback do `meter_id` w silniku zapytań `get_readings`, zapobiegając pustym seriom danych autokonsumpcji przy specyficznych formatach bilingowych OSD.

## v1.3.2 (2026-09-09) — Silnik Autokonsumpcji PV i Realnego Zużycia Domu (Hour-by-Hour Alignment)

### ☀️ Asynchroniczny Bilans Autokonsumpcji i Domu (`autoconsumption.py`)
- **Dopasowanie godzinowe (Hour-by-Hour Bucket Alignment):**
  - Wyeliminowano błąd pozornej „100% autokonsumpcji” wynikający z opóźnienia OSD Mój Licznik (3–24h) względem falownika PV. Bilans liczony jest ściśle w zamkniętych interwałach godzinowych, dla których dostępne są oba źródła telemetrii.
  - Wyliczanie rzeczywistego zużycia domu: $\text{Zużycie Domu}[h] = \text{Pobór}[h] + \max(0, \text{Produkcja PV}[h] - \text{Oddanie}[h])$.
  - Wyliczanie oszczędności finansowych w kwotach **BRUTTO (z VAT 23%)** w odniesieniu do stawek zmiennych energii i dystrybucji taryf G11, G12 oraz G12w.

### 🏠 Nowe Sensory Telemetryczne Home Assistant
- `sensor.energa_[meter_id]_autokonsumpcja_dzis` (kWh z atrybutami godzinowymi synchronizacji)
- `sensor.energa_[meter_id]_autokonsumpcja_wczoraj` (kWh — pełna, ostateczna zamknięta doba)
- `sensor.energa_[meter_id]_autokonsumpcja_mtd` (kWh)
- `sensor.energa_[meter_id]_stopien_autokonsumpcji_mtd` (% wyprodukowanej energii zużytej na miejscu)
- `sensor.energa_[meter_id]_samowystarczalnosc_energetyczna_mtd` (% zapotrzebowania domu pokrytego ze słońca)
- `sensor.energa_[meter_id]_realne_zuzycie_domu_dzis` (kWh)
- `sensor.energa_[meter_id]_realne_zuzycie_domu_mtd` (kWh)
- `sensor.energa_[meter_id]_oszczednosc_autokonsumpcja_mtd` (PLN brutto zaoszczędzone dzięki autokonsumpcji)

### ⚙️ Konfiguracja i Integracja z HA Recorder
- W `OptionsFlow` dodano pole wyboru encji falownika (`inverter_energy_entity`, np. `sensor.solis_energy_total`).
- `RecorderAdapter.async_get_hourly_statistics`: odpytuje rejestrator HA Core (`statistics_during_period`) o godzinowe delty produkcji falownika.

## v1.3.1 (2026-09-09) — Ujednolicenie Brutto (z VAT 23%) i Poprawki Stabilności Recordera

### 🛡️ Poprawki Stabilności i Bezpieczeństwo
- **Bezpieczny odczyt `StatisticMetaData` TypedDict (`ha/recorder_adapter.py`):**
  - Obsługa obiektów `StatisticMetaData` będących typami słownikowymi `TypedDict` w Home Assistant Core (zapobieganie `AttributeError: 'dict' object has no attribute 'statistic_id'`).
- **Brakujący import `Decimal` (`sensor.py`):**
  - Naprawiono błąd `NameError: name 'Decimal' is not defined` przy inicjalizacji współczynnika sprawności BESS (`bess_efficiency`).

### 💰 Spójność Finansowa i Ujednolicenie Kwot BRUTTO (z VAT 23%)
- **Ujednolicenie encji składowych rachunku (`EnergaBillComponentSensor`):**
  - Wszystkie sensory wartości bilingowych (`sale_total`, `distr_total`) raportują teraz kwoty **BRUTTO (z VAT 23%)**, zapewniając pełną tożsamość: $\text{Energia Czynna Brutto} + \text{Dystrybucja Brutto} = \text{Koszt Brutto MTD}$.
  - Wartości netto oraz stawka VAT są dostępne w atrybutach encji (`netto_pln`, `vat_rate`).
- **Ceny dynamiczne RCE w brutto (`sensor.energa_[meter_id]_rce_dynamic_price`):**
  - Zgodnie z art. 4b ustawy o OZE wycena depozytu oraz stawka rynkowa prezentowana jest z mnożnikiem VAT 23%.

## v1.3.0 (2026-09-07) — Wdrożenie Etapu 5 Architektury Docelowej V1.0 (Zaawansowane Prognozowanie WAL, Ceny Dynamiczne PSE RCE i Arbitraż BESS)

### 📈 Zaawansowane Prognozowanie Godzinowe (Hourly Profile Forecaster)
- **Przejście z prostej ekstrapolacji MTD na model profilu godzinowego (`projections/forecast.py`):**
  - Wykorzystanie do 730 dni kanonicznych danych godzinowych z bazy SQLite WAL (`CanonicalStorage`).
  - Rozbicie poboru i oddania energii na profile typów dni:
    - **Profil dnia roboczego ($P_{\text{work}}$):** dekompozycja 24-godzinna dla dni roboczych (Pn–Pt) z porannym i popołudniowym szczytem.
    - **Profil weekendowy/świąteczny ($P_{\text{weekend}}$):** wypłaszczony profil dla sobót, niedziel oraz świąt.
  - **Pełna obsługa polskiego kalendarza dni wolnych od pracy:**
    - Stałe święta państwowe i kościelne (1.01, 6.01, 1.05, 3.05, 15.08, 1.11, 11.11, 25-26.12).
    - Ruchome święta wielkanocne wyliczane z algorytmu Meeusa (Wielkanoc, Poniedziałek Wielkanocny, Boże Ciało).
  - **Dekompozycja strefowa dla taryf G11, G12 oraz G12w:**
    - Automatyczne mapowanie godzin przyszłych na strefy szczytowe (T1) i pozaszczytowe (T2).
    - Uwzględnienie specyfiki G12w: 100% godzin w weekendy i polskie święta przypisywane do strefy taniej (T2).
  - **Adaptacyjny wskaźnik trendu ($k$):**
    - Skalowanie profilu na podstawie rzeczywistej intensywności poboru zrealizowanego w bieżącym miesiącu (kompensacja sezonowa pomp ciepła i klimatyzacji).
  - **Bezpieczny fallback:**
    - Przy bazie <7 dni automatyczny i płynny fallback do wygładzonej ekstrapolacji MTD.
  - **Rozszerzone atrybuty w `sensor.energa_[meter_id]_bill_forecast`:**
    - `forecast_method: hourly_profile_wal`, `profile_confidence`, `profile_history_days`, `profile_trend_factor`, `forecast_import_t1_kwh`, `forecast_import_t2_kwh`, `forecast_export_t1_kwh`, `forecast_export_t2_kwh`.

### ⚡ Dynamiczne Ceny PSE RCE i Silnik Arbitrażu BESS (`projections/arbitrage.py`, `adapters/pse/rce_client.py`)
- **Klient API PSE OIRE dla cen dynamicznych:**
  - Asynchroniczne pobieranie cen RCE (interwały 15-minutowe i 1-godzinne) dla bieżącej doby oraz publikacji D-1 na dobę kolejną (dostępnej po godz. 14:00).
  - Idempotentny zapis interwałowych cen RCE w bazie SQLite WAL (`market_price`).
- **Silnik Arbitrażu Cenowego i Optymalizacji Magazynu Energii (BESS):**
  - Wykrywanie optymalnych, najtańszych okien ładowania (`charge_windows`) dla baterii, pomp ciepła, buforów CWU i pojazdów elektrycznych.
  - Wykrywanie szczytowych okien rozładowania (`discharge_windows`) w godzinach wieczornych.
  - **Weryfikacja sprawności cyklu (Round-Trip Efficiency $\eta = 88\%$):** automatyczne blokowanie zbędnego cyklowania baterii, gdy spread cenowy nie pokrywa strat energii.
  - **Ochrona przed cenami ujemnymi (Negative Price Alert):** natychmiastowe wykrywanie interwałów z $RCE < 0$ PLN/kWh dla ochrony depozytu prosumenckiego przed stratami eksportu.

### 🏠 Nowa Platforma `binary_sensor` oraz Nowe Sensory Telemetryczne Home Assistant
- **Nowa platforma `binary_sensor.py`:**
  - `binary_sensor.energa_[meter_id]_bess_charge_window` (aktywne okno ładowania BESS)
  - `binary_sensor.energa_[meter_id]_bess_discharge_window` (aktywne okno szczytu i rozładowania BESS)
  - `binary_sensor.energa_[meter_id]_rce_negative_price` (alarm ujemnej ceny PSE)
- **Nowe sensory telemetryczne w `sensor.py`:**
  - `sensor.energa_[meter_id]_rce_dynamic_price` (bieżąca dynamiczna cena rynkowa PSE w PLN/kWh z atrybutami interwału UTC)
  - `sensor.energa_[meter_id]_bess_arbitrage_spread` (bieżący spread arbitrażowy BESS z uwzględnieniem strat sprawności)

---

## v1.2.0 (2026-09-07) — Wdrożenie Etapu 4 Architektury Docelowej V1.0 (Produkcja, Recorder Adapter, Migration Map, Diagnostyka i Alerty)


### 🏗️ Architektura Produkcyjna Home Assistant (Implementation Brief V1.0 - Etap 4)
- **Dedykowany Pakiet `ha/` i Adapter Home Assistant Recorder (`RecorderAdapter`):**
  - Wyodrębniono `ha/recorder_adapter.py` izolujący domenę integracji od bezpośrednich wywołań silnika bazy danych Home Assistant Core.
  - Wdrożono rygorystyczną walidację i sanityzację punktów `StatisticData`:
    - **Gwarancja monotoniczności:** `validate_and_clean_statistics` blokuje i koryguje jakiekolwiek ujemne uskoki sum w ramach serii, eliminując błędy resetu licznika w HA.
    - **Ochrona przed wartościami ujemnymi i skokami fizycznymi:** odrzucanie anomalii powyżej `MAX_HOURLY_KWH` (50 kWh/h) oraz ujemnych delty energii.
    - **Prawidłowe metadane:** wymuszenie `has_mean: False`, `has_sum: True`, `StatisticMeanType.NONE`, `unit_class: energy`.
- **Mapa Migracji i Wymiana Licznika (`MigrationMap`):**
  - Zapewniono obsługę scenariusza *Meter Replacement* (wymiana fizycznego licznika na danym PPE): automatyczne wyliczanie offsetu kumulacyjnego gwarantuje gładką ciągłość sum energii bez zapaści ani utraty historii w Panelu Energia.
  - Zdefiniowano most łączący dotychczasowe identyfikatory encji (`sensor.energa_{serial}_...`) ze stabilnymi logicznymi identyfikatorami PPE (`energa_mobile:{ppe_id}__...`).
- **Odporność na Restarty Offline i Awarie Sieci:**
  - Zabezpieczono wszystkie sensory (`EnergaStatisticsSensor`, `EnergaBankKwhSensor`, `EnergaBillCurrentSensor`): przy braku połączenia z API Energi podczas startu HA encje nie emitują fałszywych zer (`0.0`), zachowując poprzedni stan lub zgłaszając `unavailable` bez naruszania bazy statystyk.
- **Oficjalna Platforma Diagnostyczna Home Assistant (`diagnostics.py`):**
  - Wdrożono natywną funkcję `async_get_config_entry_diagnostics` dostępną z interfejsu HA (*Pobierz diagnostykę*).
  - Automatyczne, rekursywne maskowanie danych wrażliwych (PII: hasła, tokeny, PESEL, adresy zamieszkania, telefony, e-maile).
  - Pełny zrzut metryk kanonicznej bazy SQLite WAL: wersja schematu, liczba odczytów, zakres dat, rozmiar pliku, status koordynatora oraz aktywne alerty.
- **System Wczesnego Ostrzegania Prosumenckiego (`alerts.py`):**
  - Wykrywanie przerw w odczytach powyżej 48h (*source staleness*).
  - Monitorowanie 12-miesięcznego terminu wygasania lotów FIFO (kroczące ostrzeżenia 30- i 7-dniowe dla depozytu net-billing i energii net-metering).
  - Wykrywanie niezaakceptowanych rozbieżności faktur wymagających weryfikacji.
- **Eliminacja Ostrzeżeń Bootstrapu Home Assistant:**
  - Przeniesiono 2-letni auto-backfill z `hass.async_create_task` na `entry.async_create_background_task`, dzięki czemu start Home Assistant Core nie jest blokowany i przebiega natychmiastowo.

## v1.1.1 (2026-09-06) — Eliminacja Błędnego Kodowania Tytułu Pulpitu Lovelace (Auto-Purge PrzeglÄ d)

### 🎨 Standaryzacja Interfejsu i Poprawka Kodowania (i18n)
- **Automatyczne oczyszczanie nadpisania domyślnego pulpitu Lovelace:**
  - Wycofano i zablokowano rejestrację wpisów o identyfikatorze `lovelace` w `.storage/lovelace_dashboards`. Wbudowany w Home Assistant Core pulpit `/lovelace` zarządza własnym tytułem w oparciu o silnik lokalizacyjny (`title: None`), co wyświetla natywny polski tytuł **"Przegląd"** w pasku bocznym.
  - Dodano automatyczne wykrywanie i usuwanie błędnych wpisów ze zniekształconym kodowaniem UTF-8 (`PrzeglÃ„Â…d` / `PrzeglÄ d`) podczas startu integracji i generowania dashboardów.

## v1.1.0 (2026-09-05) — Wdrożenie Etapu 3 Architektury Docelowej V1.0 (Ceny, Faktury, Rekonsyliacja Audytowa i Storage V2)

### 📊 Architektura Docelowa V1.0 (Implementation Brief V1.0)
- **Rozszerzenie Kanonicznego Storage SQLite WAL do Schema V2:**
  - Dodano tabelę `market_price` z wersjonowaniem rewizji cen PSE (RCEm / RCE), jawną datą publikacji, rozdzielczością i źródłem (*provenance*).
  - Dodano tabele `settlement_lot` oraz `settlement_allocation` dla trwałego śledzenia lotów FIFO (`kWh` w net-meteringu, `PLN` w net-billingu) oraz alokacji z terminami ważności (*expires_at*).
  - Dodano tabele `invoice_reconciliation` oraz `invoice_reconciliation_line` do przechowywania audytowalnych raportów rekonsyliacji faktur wraz ze stanem zatwierdzenia użytkownika (*approval state*).
  - Automatyczna migracja `schema_version` z V1 do V2 z zachowaniem wszystkich dotychczasowych odczytów i checkpointów.
- **Audytowalna Rekonsyliacja Faktur i Golden Test:**
  - Wdrożono silnik rekonsyliacji per-line porównujący pozycje taryfowe z fakturami sprzedawcy (energia czynna, opłata handlowa, składnik zmienny, opłata jakościowa, OZE, kogeneracyjna, opłaty stałe, opłata mocowa, VAT 23% i depozyt).
  - Dodano test *golden fixture* w oparciu o fakturę `FES_00017.pdf` (Warzywna, G11, 2159 kWh, 2 794,24 zł brutto) wykazujący zgodność co do grosza (`MATCH`, 0,00 zł wariancji).
- **Niezmienniki Księgi (Ledger Units Invariant):**
  - Zagwarantowano ścisłą izolację księgi fizycznej energii (`kWh`) i księgi monetarnej (`PLN`) — zabezpieczono silniki FIFO i modele przed mieszaniem niespójnych jednostek.
- **Usługa Home Assistant `energa_mobile.reconcile_invoice`:**
  - Zarejestrowano usługę w HA umożliwiającą automatyczne audytowanie faktur z powiadomieniem `persistent_notification` i trwałym zapisem w bazie integracji.

## v1.0.9 (2026-09-05) — Natychmiastowa Dostępność Rachunku MTD po Nowej Instalacji (Fresh Start Fix)

### ⚡ Eliminacja Stanu `unavailable` na Sensorach Rachunku MTD i Prognozy
- **Fallback na Pamięć Podręczną Koordynatora (`_compute_period_sums_from_memory`):**
  - Przy pierwszej instalacji lub restarcie, zanim silnik HA Recorder zarejestruje encje i zaimportuje statystyki do SQLite, koordynator natychmiast wylicza sumy MTD z pobranych z API danych godzinowych (`_hourly_stats`).
  - Kafelki `Dotychczasowy Rachunek`, `Prognoza Rachunku` oraz sensory składowe MTD są aktywne w kilka sekund po instalacji, bez oczekiwania na kolejny godzinny cykl koordynatora.
- **Dedykowane Lokalne Odświeżanie Rozliczenia (`async_refresh_settlement`):**
  - Koordynator przelicza MTD, 365-dniową historię oraz sumy miesięczne lokalnie z bazy danych HA i powiadamia encje **bez** ponownego odpytywania zewnętrznego API Energi (ochrona przed rate-limit i opóźnieniami sieciowymi).
  - Wdrożono wielostopniową kalibrację startową w interwałach (2s, 8s, 20s), gwarantującą płynne przełączenie na pełne statystyki bazy SQLite zaraz po zakończeniu importu.
- **Obsługa Pojedynczego Punktu Statystyki:**
  - W `_async_compute_period_sums`, gdy w oknie zapytania dostępny jest 1 punkt statystyki (np. 1. dzień miesiąca lub świeża instalacja), delta wyliczana jest bezpośrednio z wartości punktu (`change`/`state`), zamiast pomijania licznika.

## v1.0.8 (2026-09-05) — Całkowita Eliminacja Świec (Spikes) na Panelu Energia & Ochrona Bazy Statystyk

### 🛡️ Eliminacja Świec i Zapaści Liczników w Panelu Energia (Energy Dashboard)
- **Wyciszenie `native_value` w sensorach statystyk (`EnergaStatisticsSensor`, `EnergaCostStatisticsSensor`, `EnergaBankFlowSensor`):**
  - Wycofano zwracanie wartości stanu bieżącego w sensorach statystyk panelu oraz baterii (`native_value` zwraca `None`).
  - Rozwiązano problem podwójnego zliczania: silnik kompilatora statystyk Home Assistant Core Recorder zliczał przyrosty ze stanów tabeli `states`, które kolidowały z danymi wstrzykiwanymi przez `async_import_statistics`. Przy nocnych resetach stany zerowały się lub zaczynały od zera, co przy kolejnym imporcie tworzyło wielomegowatowe świece (9–13 MWh) lub ujemne załamania sumy.
  - Dane do Panelu Energia oraz sekcji Baterii płyną teraz w 100% czysto i wyłącznie przez precyzyjny silnik importu statystyk godzinowych.
- **Korekta klas stanów (`state_class = None`) dla wskaźników okresowych i poziomów magazynu:**
  - Wskaźniki okresowe MTD (`EnergaBillComponentSensor`: koszty, potrącenia, pokrycia) oraz poziomy depozytu/magazynu (`EnergaProsumerBankSensor`, `EnergaProsumerBankPlnSensor`) nie są sumującymi się licznikami energii — posiadają teraz `state_class = None`. Zapobiega to rejestrowaniu ich przez HA Recorder jako akumulatorów całkujących i fałszywym ujemnym spadkom na przełomie miesięcy.
  - Dodano dedykowane sensory wolumenu energii MTD (`pobor_energii_*_mtd`, `oddanie_energii_*_mtd`) do zasilania kart podsumowujących.
- **Ochrona monotoniczności i deduplikacja importu historii:**
  - W `gather_stats_for_sensor` dodano bezwzględny strażnik monotoniczności (`running_sum >= last_sum`), uniemożliwiający cofnięcie się sumy statystyk.
  - W `_import_meter_history` dla sensorów `bank_ladowanie` i `bank_rozladowanie` wprowadzono sprawdzanie `get_last_statistics`, deduplikację punktów i kotwiczenie tylko nowych interwałów na `last_sum`, co zapobiega dublowaniu sumy przy ponownym pobraniu historii.
- **Narzędzia audytu i automatycznej naprawy bazy SQLite:**
  - `scripts/check_spikes.py`: skanuje bazę statystyk HA z uwzględnieniem przerw czasowych (time-gap aware), wykrywając realne zapaści sumy, nienaturalne skoki godzinowe (>25 kWh/h) i anomalie stanów.
  - `scripts/repair_spikes.py`: bezpiecznie usuwa przekłamane wiersze statystyk, wskaźniki MTD z tabeli recorder oraz odtwarza spójność sum bez utraty prawidłowej historii.

## v1.0.7 (2026-09-05) — Generator Dashboardów na Żądanie & Anonimizacja Danych

### 🚀 Nowe Możliwości & Usprawnienia
- **Dostarczanie prekonfigurowanych Dashboardów na żądanie użytkownika:**
  - Dodano usługę `energa_mobile.generate_dashboard` oraz przycisk w panelu integracji `button.energa_mobile_generate_dashboards` ("Wygeneruj Gotowe Dashboardy Energa").
  - Integracja nie narzuca już tworzenia dashboardów w ciemno — użytkownik ma pełną kontrolę i może jednym kliknięciem wygenerować nowoczesne karty dostosowane do jego typu instalacji i taryfy.
- **Anonimizacja danych i ustandaryzowane nazwy widoków:**
  - Przejrzyste, intuicyjne tytuły dashboardów: *Wirtualny Magazyn*, *Depozyt Prosumencki*, *Profil Konsumencki*.
  - Całkowita anonimizacja danych adresowych w testach, dokumentacji i domyślnych nazwach widoków.

## v1.0.6 (2026-09-05) — Standaryzacja Nazewnictwa Encji do Standardu Home Assistant (Opcja A)

### 🚀 Standaryzacja Nazw & Pełna Zgodność z HA Core
- **Spójne, kanoniczne nazewnictwo encji (`has_entity_name = True`):**
  - Wszystkie sensory integracji posiadają teraz czyste, profesjonalne nazwy w języku polskim w `_attr_name` (np. `Bank Wirtualny kWh`, `Prognoza Rachunku`, `Dotychczasowy Rachunek`, `Koszt Brutto MTD`, `Magazyn Poziom`) bez ręcznie doklejanych numerów seryjnych `({serial})`.
  - Przypisano brakujące `self._attr_device_info = device_info` w klasach sensorów bankowych i rozliczeniowych (`EnergaBankKwhSensor`, `EnergaBankPlnSensor`, `EnergaFirstDataDateSensor`, `EnergaBillForecastSensor`).
  - Home Assistant automatycznie i spójnie generuje identyfikatory encji w przestrzeni urządzenia: `sensor.energa_<serial>_<slug>` (np. `sensor.energa_10000002_bank_wirtualny_kwh`, `sensor.energa_10000001_bank_wirtualny_pln`, `sensor.energa_10000001_prognoza_rachunku`).
  - Wyeliminowano podwójne numery liczników w nazwach (np. `sensor.energa_10000002_magazyn_poziom_10000002` -> `sensor.energa_10000002_magazyn_poziom`).
- **Automatyczna migracja Entity Registry na starcie integracji:**
  - W `async_setup_entry` dodano bezobsługowy mechanizm migracji rejestru encji (`_ent_reg.async_update_entity`), który przy starcie HA natychmiastowo i bezpiecznie przemianowuje dotychczasowe encje na nowe, kanoniczne `entity_id`.
  - Pełna ochrona historii długoterminowej w HA Recorderze — unikalne identyfikatory (`_attr_unique_id`) pozostały w 100% niezmienne.

## v1.0.5 (2026-09-05) — Ujemne Odliczenie z Depozytu i Dashboard dla G11 Net-Billing

### 🚀 Nowe Możliwości & Usprawnienia UX
- **Ujemny znak dla odzyskanego depozytu prosumenckiego (`deposit_applied`):**
  - Sensor `sensor.energa_<meter>_mtd_deposit_applied` (nazwa przyjazna: `Odzyskano z Depozytu MTD`) zwraca teraz wartość ujemną (np. `-20.73 PLN`).
  - Umożliwia to w 100% intuicyjną arytmetykę na kartach rozliczenia:
    `Całkowity koszt brutto (118.90 zł) + Odzyskano z depozytu (-20.73 zł) = Dotychczas do zapłaty (98.17 zł)`.
  - W atrybutach encji zachowano wartość dodatnią `deposit_applied_positive_pln` oraz znacznik `is_deduction: true`.
- **Dedykowany panel rozliczeń i magazynu dla instalacji G11 z fotowoltaiką na nowych zasadach (Net-billing):**
  - Skonfigurowano widok `/rachunek-dom` dla taryfy jednostrefowej G11 ze statystykami depozytu, ceną RCEm oraz rozliczeniem bieżącym i prognozowanym.

## v1.0.4 (2026-09-05) — Dedykowane Encje Składowe Rozliczenia i Magazynu

### 🚀 Nowe Możliwości
- **Promocja składowych rozliczenia MTD do natywnych sensorów (`EnergaBillComponentSensor`):**
  - Wszystkie kluczowe wartości rozliczeniowe i magazynowe są teraz osobnymi, natywnymi encjami Home Assistant (dostępnymi out-of-the-box bez konieczności parsowania atrybutów):
    - `sensor.energa_<meter>_mtd_brutto` — Całkowity koszt energii i dystrybucji brutto MTD [PLN]
    - `sensor.energa_<meter>_mtd_sale_total` — Koszt zakupu energii czynnej MTD [PLN]
    - `sensor.energa_<meter>_mtd_distr_total` — Koszt dystrybucji i opłat stałych MTD [PLN]
    - Dla Net-billing:
      - `sensor.energa_<meter>_mtd_deposit` — Wartość doładowania depozytu z PV MTD [PLN]
      - `sensor.energa_<meter>_mtd_deposit_applied` — Wartość potrącenia z depozytu na energię MTD [PLN]
    - Dla Net-metering:
      - `sensor.energa_<meter>_mtd_cover_day` — Pokrycie z magazynu w Strefie 1 MTD [kWh]
      - `sensor.energa_<meter>_mtd_cover_night` — Pokrycie z magazynu w Strefie 2 MTD [kWh]
- **Dedykowany dashboard magazynu dla Net-meteringu (G12w):**
  - Skonfigurowano widok `/rachunek-dom` prezentujący stan magazynu w kWh, stopień napełnienia, bilans prosumencki oraz pokrycie energii czynnej.

### 🚀 Nowe Możliwości
- **Encja dotychczasowego rachunku (`EnergaBillCurrentSensor`):**
  - Dodano nową dedykowaną encję `sensor.energa_<meter_id>_dotychczasowy_rachunek` (np. `Dotychczasowy Rachunek (10000001)`).
  - Raportuje dokładną kwotę do zapłaty **od 1. dnia miesiąca do chwili obecnej (MTD)** na podstawie faktycznego poboru, stawek taryfy i potrącenia depozytu prosumenckiego.
  - Eliminuje niepewność użytkownika dotyczącą ekstrapolacji: użytkownik widzi jednocześnie faktyczny koszt do dziś (np. 105 zł) oraz prognozę na koniec miesiąca (np. 465 zł).
- **Zgodność z Python 3.9+ (`const.py`):**
  - Dodano `from __future__ import annotations` w `const.py`, zapobiegając błędom typowania unii `|` w starszych środowiskach.

## v1.0.2 (2026-09-05) — Stabilizacja Net-Billingu & Odporność Sieciowa

### 🐛 Bug Fixes & Usprawnienia
- **Stabilizacja depozytu prosumenckiego Net-billing (`sensor.bank_wirtualny_pln_*`):**
  - Poprawiono `native_value` z `gross_deposit - deposit_applied` na `max(0.0, gross_deposit - deposit_applied)`. Depozyt w portalu Energa Obrót jest aktywem klienta i nie może przyjmować wartości ujemnych.
  - Dodano precyzyjne atrybuty analityczne: `gross_deposit_pln`, `deposit_applied_pln`, `deposit_remaining_pln` oraz `net_financial_balance_pln`.
- **Ścisłe wygaszenie wirtualnej baterii w Net-billingu:**
  - Encje `EnergaProsumerBalanceSensor`, `EnergaBankKwhSensor`, `EnergaBankLevelSensor` oraz `EnergaBankFlowSensor` (`bank_ladowanie` / `bank_rozladowanie`) są ściśle ograniczone do instalacji ze starym Net-meteringiem (`prosumer_coefficient >= 0.7`).
  - Na instalacjach Net-billingowych (`prosumer_coefficient < 0.7`) wirtualny akumulator kWh nie jest tworzony, a stare osierocone encje są automatycznie usuwane z rejestru.
- **Wygładzanie wczesnomiesięczne prognozy faktury (`EnergaBillForecastSensor`):**
  - Dodano algorytm `smoothed_blend_7d`: w pierwszych 7 dniach miesiąca (przy dostępnej historii >= 14 dni) prognoza łączy bieżącą stawkę dobową MTD ze średnią dobową kroczącą (30d/365d), eliminując nierealistyczne prognozy (np. 8000 PLN) na początku miesiąca.
  - Zoptymalizowano `_annual_import_estimate` dla progu opłaty mocowej — ufa danym rejestratora przy pokryciu >= 30 dni.
- **Odporność na chwilowe błędy sieciowe (transient network retry):**
  - W `_api_get` dodano automatyczne ponowienie zapytania przy pierwszej próbie z 2-sekundowym backoffem w przypadku `aiohttp.ClientError`, `asyncio.TimeoutError`, `TimeoutError` lub `RuntimeError`.
- **Sensory Panelu Energia ze stanem liczbowym (`EnergaStatisticsSensor`):**
  - Zainicjalizowano `_last_sum` oraz zwracanie ostatniej zaimportowanej sumy w `native_value`. Eliminuje to stan `unknown` na liście encji i zapewnia 100% czystą walidację w `energy/validate` (zero błędów i zero ostrzeżeń).

## v1.0.1 (2026-09-04) — Szybki Start & Odporność Onboardingu

### 🐛 Bug Fixes
- **Eliminacja timeoutu 60s platformy sensorów przy czystym onboardingu:**
  - W `_get_smart_start_date` fallback przy braku wcześniejszych statystyk został skrócony z 30 dni do 1 dnia (`now - timedelta(days=1)`). Pobieranie 30 dni synchronicznie w `async_setup_entry` zajmowało ponad 75 sekund i przekraczało limit 60s watchdoga platformy Home Assistant.
  - Dodano wyszukiwanie istniejących statystyk w rejestratorze (`recorder`) po przewidywalnym `statistic_id`, zanim koordynator sięgnie po domyślny fallback.
  - Pierwsze uruchomienie po dodaniu integracji trwa teraz poniżej 3 sekund bez żadnych ostrzeżeń HA, a pełna 730-dniowa historia pobiera się asynchronicznie w tle przez `_maybe_auto_backfill`.

## v1.0.0 (2026-09-04) — Architektura Docelowa

### 🚀 Nowe Możliwości & Architektura
- **Kanonityczny Magazyn Danych (SQLite WAL):** Trwały, odporny na awarie i wymiany liczników magazyn odczytów (`energa_canonical.db`) powiązany z logicznym punktem poboru (PPE).
- **Czysty Silnik Rozliczeniowy FIFO (Decimal):**
  - **Net-Metering (kWh):** Fizyczny magazyn energii FIFO 12 miesięcy, poprawny współczynnik 0.8/0.7, wygasanie energii, natywne serie ładowania i rozładowania dla sekcji Magazynu Energii.
  - **Net-Billing (PLN):** Finansowy depozyt z dedykowaną alokacją wyłącznie do pozycji energii czynnej (zgodnie z art. 4 ust. 11 ustawy o OZE), obsługa zwrotu niewykorzystanego depozytu (20% RCEm / 30% RCE).
- **Automatyczny Parser Cen RCEm z PSE:** Pobieranie oficjalnych stawek miesięcznych RCEm z tabeli PSE z uwzględnieniem korekt i publikacji ~11. dnia miesiąca.
- **Autonomiczna Prognoza Rachunku:** Dokładna estymata faktury brutto z podziałem na taryfy G11 i G12w oraz uwzględnieniem progu opłaty mocowej.
- **Klasa `monetary` dla encji kosztowych:** Encje `_cost` posiadają klasę `monetary`, co umożliwia ich bezpośredni wybór w sekcji "Użyj encji śledzącej całkowity koszt" w Panelu Energia.
- **Spójna konfiguracja Panelu Energia:** Wyeliminowanie kolizji `_cost_2`, pełna zgodność z walidacją Home Assistant (`energy/validate` bez błędów).
- **Kompleksowa aktualizacja dokumentacji:** Nowy, przejrzysty przewodnik w `README.md` obejmujący konfigurację starego i nowego systemu oraz odbiorców taryfy G11.

## v0.3.9 (2026-09-04)

### 🐛 Bug Fixes (bateria jedno-strefowa & alokacja depozytu)
- **Eksport jednej strefy trafiał do slotu importu:** backfill
  przepływów wkładał `export` do kubełka 0 zamiast 1 — ładowanie
  stało w `0.0`, a rozładowanie rosło o import+eksport naraz.
  Niewidoczne na konsumentach (brak eksportu) i G12W (osobna
  ścieżka); wykryte na żywym G11 z PV (rozładowanie ≈ import+eksport
  co do kWh). Mapowanie wydzielone do testowalnego `bucket_flows`.
- **P0: alokacja depozytu tylko do energii czynnej:** depozyt
  net-billingowy alokowany jest wyłącznie do kwalifikowanych pozycji
  sprzedaży energii czynnej brutto (`sale_gross`, art. 4 ust. 11 ustawy
  o OZE), a nie od całego brutto faktury. Dystrybucja i opłaty stałe
  pozostają zawsze do zapłaty.

## v0.3.8 (2026-09-04)

### ✨ Nowe (wybór systemu przy dodawaniu)
- **Pytanie o system w kreatorze:** konta prosumenckie wybierają
  stare (0.8) albo nowe (0.0) zasady od razu przy logowaniu —
  API nie zdradza systemu (data aktywacji to data aplikacji),
  więc pyta człowiek raz, zamiast zgadywać. Wybór ląduje w Options
  (Ceny) i da się zmienić.
- **Sierota Magazyn Poziom sprzątana** w nowym systemie (stary sensor
  poziomu nie ma tam sensu i wisiał jako `unavailable`).

### 🧪 Testy
- Orphan-set nowego systemu zawiera poziom; ~210 testów.

## v0.3.7 (2026-09-04)

### 🔙 Revert (v0.3.6 cofnięty)
- **Auto-współczynnik wycofany:** data aktywacji to data aktywacji
  aplikacji Mój Licznik, nie data umowy prosumenta (licznik ze starą
  instalacją potrafi mieć tegoroczną datę) — zgadywanie systemu po niej
  błędnie klasyfikowało. Współczynnik wraca do jawnego ustawienia
  w Options (`0.8` stare / `0.0` nowe); nic innego z v0.3.6 nie zmienia
  zachowania.

## v0.3.6 (2026-09-04)

### ✨ Nowe (G11 z PV na nowych zasadach)
- **Auto-domyślny współczynnik z daty aktywacji:** prosument bez
  ustawionego `prosumer_coefficient` dostaje `0.0` (net-billing)
  gdy umowa startuje od 04.2022, inaczej `0.8` (opusty). Świeży
  G11 z fotowoltaiką nie wpada już w stary Bank kWh — od pierwszego
  startu ma Bank PLN + RCEm + prognozę z depozytem. Ręczne ustawienie
  w Options zawsze wygrywa; mieszane konta (starzy + nowi prosumenci
  na jednym loginie) zostają bez zmian do decyzji ręcznej.
- Jednostrefowy Bank PLN / prognoza / przepływy działają bez zmian
  (gałąź `else` dla liczników bez stref była gotowa — brakowało tylko
  właściwego współczynnika).

## v0.3.5 (2026-09-04)

### 🐛 Bug Fixes (Bank kWh skakał po częściowym reimporcie)
- **Panele też kotwiczą reimporty:** `build_statistics` startuje
  `running_sum` (i koszty) od sumy sprzed zakresu importu
  (`_stat_sum_before`, jak przepływy w v0.3.4). Częściowy reimport
  pisał wiersz graniczny z `sum 0.0`, co zatruwało cały poprzedni
  miesiąc (`last-first` rzędu −5509 kWh) i ścinało Bank FIFO o setki kWh.
- **Delty odporne na resety (`reset_aware_delta`):** sumy miesięczne/MTD/
  rolling liczą tylko dodatnie segmenty szeregu `sum` — jeden zły wiersz
  nie kasuje już całego miesiąca ani estymaty rocznej do progu mocowego.
- **Kotwica bierze MAX z 30 dni, nie ostatni wiersz** (`_stat_sum_before`).

### 🧪 Testy
- **~205 testów**: `reset_aware_delta` (monotoniczny / reset w środku /
  reset na granicy miesiąca / defensive), clamp ujemnego miesiąca w FIFO.

## v0.3.4 (2026-09-04)

### 🐛 Bug Fixes (słupki baterii znikały po backfillu — G12W nowe zasady)
- **Reimporty kontynuują sumy, nie startują od 0:** serie przepływów
  kotwiczone na sumie sprzed zakresu importu (`anchor_flow_series`);
  pełne backfille startują czysto od 0. Koniec z reset-dipem
  (5889 kWh → 0,0) odczytywanym przez recorder jako reset licznika.
- **Seed sensora z MAX 14 dni, nie z ostatniego wiersza:** po resecie
  sumy ostatni wiersz to 0,0 mimo tysięcy zaimportowanych kWh.

## v0.3.3 (2026-09-04)

### 🐛 Bug Fixes
- **Re-auth / zmiana loginu nie gubi już `auto_history_start`** (dane wpisu scalane, nie nadpisywane).

## v0.3.2 (2026-09-04)

### 🐛 Bug Fixes
- **Serwis `fetch_history` nie blokuje już wołającego:** 730-dniowy import działa w tle (zadanie + powiadomienie), odpowiedź serwisu wraca od razu.
- **`Data pierwszego odczytu` dla wszystkich liczników** (dotąd tylko prosumenci) — konsumenci też widzą początek okna historii.

## v0.3.1 (2026-09-04)

### 🐛 Bug Fixes
- **`Data pierwszego odczytu` `unknown` na starych wpisach:** legacy wpisy trio trzymały wykryte daty w `entry.data`, a sensor czytał tylko `options` — teraz czyta oba.

## v0.3.0 (2026-09-04)

### 🧭 Nowy first boot: historia sama w tle, zero blokowania
- **Auto-backfill 730 dni:** po dodaniu integracji historia z 2 lat
  pobiera się sama w tle (powiadomienie `Energa: Pobieranie danych`),
  gdy statystyki jeszcze nie istnieją. Koniec z zamrożonym UI
  (hierarchiczna detekcja potrafiła mielić minutę przy ~15 s obietnicy).
- **Usunięty overengineering:** platforma `button` (`Wykryj pierwszy
  odczyt`) i krok `detect_first` w Options wyleciały; sieroty sprzątają
  się same przy starcie. Zostały: `Ceny`, `Pobierz Historię` (ręczny
  re-import), `Wyczyść Statystyki`.
- **Uczciwe tłumaczenia:** koniec z `~15 sekund`, poprawione `początkowy`,
  uzupełniony angielski, taryfowe defaulty formularza G11/G12W.

### 🔋 Panel Energia od nowa: stary = off-grid, nowy = sprzedaż
- **Stary net-metering:** eksport NIE jest zwrotem do sieci (trafia do
  magazynu kWh). Rekomendowane wpięcie: import = sieć, eksport = ☀️
  instalacja PV, przepływy = bateria. Koszty eksportu po 0,95
  przestały powstawać (placeholder bez pokrycia).
- **Nowy net-billing:** nadprodukcja JEST sprzedawana — `Cena Oddania`
  to teraz żywa cena sprzedaży `RCEm×1.23` (cache PSE, fallback opcja);
  podepnij ją jako cenę zwrotu w Panelu Energia zamiast zamrożonej
  rekompensaty. W starym systemie cena jest `unknown` (brak sprzedaży).
- **Bilans Prosumencki = diagnostic (ukryty):** półprodukt do Banku
  (`Bank=max(0,Bilans)+initial`; np. 1128,1 vs 2486,1 różnią
  się dokładnie o `initial` 1358). W nowym systemie degeneruje się do
  `−import` (zero informacji). Patrz Bank i Magazyn Poziom.
- **Nowy sensor `Magazyn Poziom %`** (stary system, klasa `battery`):
  Bank / wkłady 12 m-cy × 100 z trybu FIFO; bez historii `unknown`
  zamiast zgadywania. Atrybuty FIFO: `fifo_deposits_kwh` i reszta.

### 🧾 Koszty jak na fakturze G11 + akcyza informacyjnie
- **Tabela G11** z faktury konsumenckiej (2159 kWh, brak PV):
  energia `0,6114`, handlowa `16,18/mies.`, abonament `0,70`, sieciowa
  stała `11,77`, zmienna `0,3485`, mocowa `24,05` → netto `2271,74`,
  brutto `2794,24` co do grosza. Prognoza konsumenta wreszcie poprawna
  (dotąd liczyła defaultami G12W z handlową `0,00`).
- **Akcyza 5 PLN/MWh już w cenie energii** (faktura G11 dowodzi:
  bez dodawania suma gra; linijka `naliczono akcyzę` to przypis).
  `compute_bill` raportuje ją jako info, nie dolicza.
- Tabela opłat dobierana po taryfie licznika (`tariff_family`);
  formularz Cen pokazuje defaulty G11 dla kont czysto-G11; migracja:
  opcje G11 z nietkniętymi defaultami G12W używają tabeli G11.

### 🧪 Testy
- **~200 testów**: faktura G11 co do grosza, `tariff_family`,
  migracja opcji, `warehouse_level_pct`, `deposits_kwh` w FIFO,
  `orphan_removed_uids`, brak kosztów eksportu.

## v0.2.23 (2026-09-04)

### 🔋 Historia także dla baterii (koniec 0/0)
- **Backfill przepływów:** Download History importuje też szeregi
  `Ładowanie/Rozładowanie` (replay semantyki live z tych samych godzin;
  encje rozwiązywane po `unique_id`, więc działa mimo suffixów w nazwach).
- **Seedowanie bez skoku:** sensory startują z ostatniej sumy statystyk
  (nie z 0), więc bateria nie robi resetu po imporcie/restarcie.
- Poziom baterii = `Ładowanie − Rozładowanie` narasta od pierwszego
  zaimportowanego dnia; stan magazynu (pełna wartość) pokazuje gauge
  `Bank kWh/PLN`.

### 🧪 Testy
- **182 testy** (było 179): serie historyczne przepływów.

## v0.2.22 (2026-09-04)
- Wyrównanie manifestu (0.2.21 miała manifest 0.2.20); kod = v0.2.21.

## v0.2.21 (2026-09-04)
- Logowanie serwisu `fetch_history` (diagnostyka importu historii).

## v0.2.20 (2026-09-04)

### 🧠 Bank liczony sam + naprawiona detekcja startu
- **FIFO 12 m-cy z historii (`fifo_kwh_bank`):** magazyn starego systemu
  odtwarzany z miesięcznych przepływów (wkłady `export×coeff` ważne do
  końca M+12, pobór zjada najstarsze) — zero przepisywania z faktur, gdy
  jest ~11 mies. historii (`settlement_mode: fifo_12m`, atrybuty
  `fifo_expired/uncovered`). Bez historii działa jak dotąd (baseline).
- **Detekcja pierwszego odczytu bez zgadywania:** skan miesięcy liniowy
  (half-probing przeskakiwał maj/czerwiec → zwracał 07-01), retry probek,
  testy regresyjne.
- Backfill: dla starego systemu potrzeba **≥365 dni** (nie 30!) —
  tyle trzyma API (730d); start `2025-07-30` w tle.

### 🧪 Testy
- **179 testów** (było 172): FIFO + detekcja.

## v0.2.19 (2026-09-03)

### 🧹 Sprzątanie banku nieaktywnego systemu
- Cleanup obejmuje też bank nieaktywnego systemu u prosumenata
  (np. `Bank kWh unavailable` po przejściu na net-billing) — reguła
  `orphan_bank_uids` w teście jednostkowym.
- Fix kolizji: prognoza konsumenta (v0.2.17) nie jest już kasowana
  przez sprzątanie.

### 🧪 Testy
- **172 testy** (było 168).

## v0.2.18 (2026-09-03)

### ⚖️ Opłata mocowa z urzędu, nie z faktury sąsiada
- **Auto-próg URE 2026** (Informacja 58/2025): ryczałt wg rocznego poboru
  (`<500` → 4.29, `500–1200` → 10.31, `1200–2800` → 17.18, `>2800` → 24.05
  netto/mies.). Roczny pobór z kroczących 365 dni statystyk albo z
  annualizacji licznika; ręczne `tariff_capacity` zawsze wygrywa.
  Stary default 24.05 to był po prostu najwyższy próg (Wasze domy biorą
  więcej) — dla kawalerki zawyżałby o ~17 zł/mies. Atrybut
  `capacity_source` mówi skąd liczba.
- Dla obcego konta z automatu działa też: RCEm official z PSE i próg
  mocowy; z faktury trzeba przepisać 3 liczby (ceny energii D/N, handlową,
  jakościową) — formularz `Options` to podpowie.

### 🧪 Testy
- **168 testów** (było 165): progi URE + wpływ na rachunek.

## v0.2.17 (2026-09-03)

### 🧾 Prognoza dla wszystkich liczników
- **`Prognoza Rachunku` dla każdego licznika** (dotąd tylko nowe
  net-billing): stary net-metering (pokrycie z magazynu, bez depozytu PLN)
  i zwykli odbiorcy (pełny rachunek z samego importu) — ta sama matematyka
  `compute_bill`, osobny sensor per licznik.
- **Fix:** stary system dostawałby fikcyjny depozyt
  (`export×RCEm×1.23`) — teraz jawne `deposit_pln=0.0`.

### 🧪 Testy
- **165 testów** (było 163): brak depozytu w starym systemie, rachunek
  konsumenta G11).

## v0.2.16 (2026-09-03)

### 🐛 Bug Fixes (wykryte na żywym lab2)
- **Flaga `is_prosumer` to był `obis_minus` w przebraniu:** `api.py`
  ustawiał ją też dla samych kodów OBIS eksportu (`or bool(mp.obis_minus
  ...)`), więc bramka `is_export_prosumer` z v0.2.15 przepuszczała liczniki
  konsumenckie (G11 bez PV nadal dostawał Bilans/Bank/przepływy).
  Flaga to już czysty `type == Wytwórca`; realny eksport wykrywają
  niezerowe liczniki `total_minus*`. Zweryfikowane na lab2: pełna
  `Prognoza Rachunku` brutto działa na żywym HA (`do_zapłaty` 100.58,
  MTD brutto 66.37, RCEm official).

## v0.2.15 (2026-09-03)

### 🧹 Czysty konsument (G11 bez PV): zero sensorów prosumenckich
- **Nowa bramka `is_export_prosumer`:** sam `obis_minus` NIE wystarcza —
  liczniki odbiorcze (np. G11 bez PV) potrafią raportować kody OBIS
  eksportu z zerowymi odczytami i dostawały bezużyteczny `Bank 0.0`,
  mylące przepływy `Ładowanie/Rozładowanie` oraz `Bilans == -import`.
  Prosument = flaga sprzedawcy (`type: Wytwórca`) LUB niezerowy licznik
  eksportu. Bez eksportu nie powstają: `Bilans`, `Bank kWh/PLN`,
  `Ładowanie/Rozładowanie`, `RCEm`, `Prognoza`, sensory eksportu ani
  `Cena Oddania/Współczynnik`.
- **Decyzja: `Bilans Prosumencki` zbędny u odbiorcy** — to algebraicznie
  `-import` (zero informacji ponad sensory importu), a nazwa wprowadza
  w błąd. Panel Energia i tak bierze sensory importu.
- **Auto-sprzątanie sierot:** przy starcie usuwane są porzucone encje
  prosumenckie liczników konsumenckich (zastępuje ręczne `jq` na
  `entity_registry` z v0.2.10).

### 🧪 Testy
- **163 testy** (było 157): +6 `is_export_prosumer` (kody OBIS to za mało,
  flaga `Wytwórca`, defensywność).

## v0.2.14 (2026-09-03)

### 🧾 Pełna prognoza rachunku brutto (faktura, nie tylko energia)
- **`Prognoza Rachunku` liczona jak faktura:** stan = prognozowana dopłata
  na koniec miesiąca (`do_zapłaty`), MTD z przepływów + liniowa ekstrapolacja
  i ponowna wycena (`tariff.compute_bill`: sprzedaż D/N + akcyza 5 PLN/MWh +
  handlowa + dystrybucja zmienna/stała/jakościowa/OZE/kogeneracyjna/mocowa +
  VAT 23% − rozliczenie prosumenta). Atrybuty: pełny rozkład MTD i prognozy
  (`mtd_brutto`, `mtd_netto`, `mtd_vat`, `mtd_deposit`, `mtd_do_zaplaty`,
  `forecast_brutto`, `forecast_do_zaplaty`) + pokrycie magazynem
  (`cover_day/night`) dla starego systemu. Stare atrybuty energetyczne
  (`mtd_net_pln`, `forecast_pln`) zostają dla kompatybilności.
- **Stary net-metering:** pobór pokryty magazynem (do wysokości Banku kWh,
  D/N proporcjonalnie) = 0 za energię i zmienną dystrybucję/jakościową;
  stałe, akcyza i OZE/kogeneracyjna zawsze płatne (jak faktura 05–06.2026:
  energia `0`, brutto `156.33` ✓).
- **Nowe opcje (`Options → Ceny`):** 12 nadpisań stawek taryfy
  (`tariff_energy_day/night`, `excise`, `trade_fee`, `abonament`,
  `grid_fixed/var_day/var_night`, `quality`, `oze`, `cogen`, `capacity`) —
  domyślne G12W z faktur; parzystość defaultów pilnowana testem.
- **Wielu liczników czytelniej:** `Bilans`, `Bank kWh/PLN`, `Ładowanie`,
  `Rozładowanie`, `Data pierwszego odczytu`, `RCEm auto` i `Prognoza`
  mają numer seryjny w nazwie (3 konta w labie miały identyczne nazwy).

### 🧪 Testy
- **157 testów** (było 145): +12 (`fees_from_options`, `split_cover`,
  parzystość defaultów `const`↔`tariff`, matematyka sensora na fakturach
  07 i 05–06.2026).

## v0.2.13 (2026-09-03)

### 🐛 Bug Fixes (zweryfikowane na labie)
- **`EnergaBankFlowSensor` bez stanu po restarcie:** brak `RestoreEntity` —
  `AttributeError: async_get_last_state` przy dodawaniu encji, sensory
  `unavailable`. Dodano dziedziczenie `RestoreEntity`.
- **Martwa linia po refaktorze:** `value = charge if ... else discharge`
  (`NameError`) — gałęzie ustawiają już `value` bezpośrednio.

## v0.2.12 (2026-09-03)

### 🔋 Natywne przepływy Banku (bateria na żywo w Panelu Energia)
- **Nowe sensory `Bank Ładowanie` / `Bank Rozładowanie`** (`total_increasing`,
  kWh, per licznik prosumencki): narastają z delty Bilansu między odczytami
  (stare: `Δ(export×coeff-import)`; nowe: wzrost exportu/importu). Pierwszy
  odczyt tylko kotwiczy bazę (bez skoku), restart odtwarza stan z HA.
  Zastępują parę template z `bank_energii.yaml` — do podepięcia jako
  `Bateria` w `Ustawienia → Pulpity → Energia`.
- **Stan Bank kWh/PLN bez zmian** (gauge w Lovelace pokazuje pełną wartość
  od razu, bateria dobudowuje historię z przepływów).

### 🐛 Bug Fixes
- **Warning `RCEm monetary+measurement`:** `RCEm (auto)` nie ma już
  `device_class monetary` (cena PLN/kWh + `measurement`); to samo dla
  `Prognozy Rachunku` (PLN + `measurement`, bez `monetary`).

### 🔒 Prywatność
- Z opisów wersji i dokumentacji usunięto numery liczników, PPE, numery
  faktur i adresy. Instalacje opisane ogólnie: `G12W stare zasady`,
  `G12W nowe zasady`, `G11 Odbiorca`; w przykładach `<nr-licznika>`.

## v0.2.11 (2026-09-04)

### ⚖️ Autokalibracja rozliczeń (FIFO 12 m-cy, nie reset kalendarzowy)
- **Weryfikacja przepisów (sprawdzone źródła):** reset „1 stycznia" (stare) i „co miesiąc"
  (nowe) byłyby NIEZGODNE z przepisami. Oba systemy to kroczące okna FIFO 12 m-cy:
  stare — odbiór w 12 m-cy od końca miesiąca wprowadzenia (`energa.pl net-metering`,
  `enerad.pl`); nowe — depozyt ważny 12 m-cy od przypisania (M+1, ×1.23), zwrot max
  20% RCEm / 30% RCE (`energa.pl net-billing`, `gov.pl` 27.12.2024 Dz.U. 1847).
- **Nowe opcje (`Options → Ceny`):** `settlement_date` (rocznica rozliczenia, np. data
  faktury), `enable_auto_settlement` (master-switch kalibracji, domyślnie OFF = jak 0.2.10),
  `use_rolling_365d` (stary system: bank z ostatnich 365 dni statystyk zamiast lifetime).
- **Bank kWh:** tryb `rolling_365d` (`max(0, export_365×coeff - import_365)`, wymaga
  `Pobierz Historię`, min. 300 dni pokrycia) + atrybuty `settlement_next`,
  `days_to_settlement`, `validity_note`, `settlement_mode`, `coverage_days`.
- **Nowy `EnergaBillForecastSensor` (`Prognoza Rachunku`, tylko nowe systemy):**
  `mtd_net/dni×dni_miesiąca` ze statystyk + atrybuty `mtd_import/export/net`,
  `forecast_pln`, `rce_source`. Depozyt pokrywa tylko energię czynną.
- **Bank PLN:** atrybuty `deposit_valid_until` (+12 m-cy), `refund_cap_note`,
  `validity_note`, `hourly_netting_note` (sprzedawca bilansuje godzinowo —
  faktura 07: 456 kWh z delty licznika 523 kWh, sensor liczy z delt — przybliżenie).

### 🐛 Bug Fixes
- **RCE auto ≠ RCEm:** prosta średnia RCE (lab: 0.59287) to NIE fakturowane RCEm
  (średnia ważona; lipiec 0.26288). `async_fetch_rcem` bierze teraz **oficjalne RCEm
  ze strony PSE** (`pse.pl/oire/rcem...`, parser w `settlement.py`), fallback: średnia
  RCE. Reguła miesiąca: przed 11. dniem → M-2, po 11. → M-1. `coordinator._rce_source`
  mówi skąd wartość (`PSE RCEm official` / `PSE RCE avg fallback` / `manual`).
   Zweryfikowano: faktura G12W-nowe 07.2026 `456×0.26288×1.23=147.44` = RCEm z tabeli PSE.
- **Weryfikacja fakturowa:** bank G12W-stare `1358+1114.18=2472.18` zgodny prod==lab;
  przybliżenie deltami vs bilans godzinowy sprzedawcy ~1% (1344 vs 1358).
  Prod atrybuty `formula` (783 / +147.44) są nieaktualne — kosmetyka po stronie prod.

### ✨ Nowe / Ulepszenia
- **Bank — łatwa widoczność:** `Bank Kwh/Pln` rozbudowane atrybuty (`formula`, `per_strefa_note`, `price_1/2`, `import_1/2` `L1/L2`) + `docs/BANK.md` z gotowym `Lovelace vertical-stack` (gauge + entities) dla G12W stare (`1358 kWh`) i G12W nowe (`0.00 PLN`). `bank_energii.yaml` do usunięcia — bank natywnie.
- **RCE auto-fetch naprawiony:** `EnergaCoordinator` cache `24h` (`_rce_cache/_rce_last_fetch` w `_async_update_data`), `EnergaRceSensor` czyta z coordinatora (`rce_source: PSE auto/manual`), `Bank PLN` używa cache gdy `rce_auto_fetch`. Brak osobnych sesji per sensor.
- **Bank tworzony tylko dla prosumenta:** `if meter.get("obis_minus")` — konsumenci bez produkcji nie dostają `Bank kWh/PLN` ani `RCEm` (na życzenie: nie zaśmieca encji).

### 🐛 Bug Fixes
- **`async_find_first_data_date` hierarchia `today-730d`:** było `2020→end_year` podwójna pętla + `07-01` probe zwracało `2025-01-01` zamiast `2024-09-02` (`api.py:293`). Teraz `window_start = today-730d` clamp `activation_date`, `start_year = window_start.year`, `~14 req` `0.7s`, probe `has_data` sprawdza `>0` per `zones`. Zgodne z `POSZLAKI_I_PLAN.md:9` (API 2 lata).
- **Bank PLN cena RCE:** preferuje `coordinator._rce_cache` gdy `auto_fetch`, fallback `bank_rce_price`.
- **`button.py` meter_id:** `Wykryj pierwszy odczyt` przekazywał `meter_serial` do `async_find_first_data_date` oczekującego `meter_point_id` — poprawiono na `point_id` (dla kont multi-meter).
- **`has_data_for_day` fałszywy pozytyw:** `bool(data["import"])` zwracało `True` dla dnia z listą `24×0.0` — teraz `any(v>0)` per `zones`.

## v0.2.9 (2026-09-03)

### 🐛 Bug Fixes
- **Poprawka z 0.2.8 jako czysty 0.2.9:** `Wykryj niedostępny` + `Bank unavailable` wymagały czystego tagu (poprzedni 0.2.8 force-push myli HACS cache). Wydano 0.2.9 bez nadpisywania tagu.

## v0.2.8 (2026-09-03)

### 🐛 Bug Fixes
- **Przycisk Wykryj pierwszy odczyt `niedostępny` + `Bank` `unavailable`:** `Button` robił `super().__init__(entry)` zamiast `coordinator` → `AttributeError: ConfigEntry has no attribute async_add_listener` + `Bank` `NameError` dla `CONF_BANK_*` w `0.2.7` na labie (cache HACS). Naprawiono `button.py` na `coordinator` i `sensor.py` importy.
- **Puste pole Wykryj w pl.json:** Już w `v0.2.7`, teraz `v0.2.8` zawiera też `Data pierwszego odczytu` widoczną.

## v0.2.7 (2026-09-03)

### 🐛 Bug Fixes
- **Bank nie działał w ogóle:** `CONF_BANK_INITIAL_KWH`, `CONF_BANK_RCE_PRICE`, `CONF_BANK_INITIAL_PLN` nie były zaimportowane w `sensor.py` → `NameError` przy każdej aktualizacji → encje banku nigdy się nie tworzyły. Dodano brakujące importy.
- **`activationDate` na złym poziomie:** API zwraca `activationDate` na poziomie `response`, nie `meterPoints`. Kod szukał `meter.get("activationDate")` → zawsze `None`. Dodano `activation_date` do `meter_obj` pobierane z `response.activationDate`.
- **Bank PLN — błędna cena eksportu:** Używał `0.95` jako ceny eksportu zamiast `get_price_for_key()` z konfiguracji. Naprawiono.

### ✨ New Features
- **Auto-detekcja stare/nowe po `prosumer_coefficient`:** `coefficient >= 0.7` = stare (net-metering, Bank kWh), `< 0.7` = nowe (net-billing, Bank PLN). Tylko jeden bank na licznik, zależnie od typu. `activationDate` nie jest wiarygodnym wskaźnikiem — to data umowy, nie data przejścia na system prosumentencki.
- **Auto-fetch RCEm z PSE API:** Nowy sensor `EnergaRceSensor` pobiera RCEm z `api.raporty.pse.pl/api/rce-pln` (opcja `rce_auto_fetch` w konfiguracji). Fallback na ręczny `bank_rce_price` jeśli pobieranie się nie powiedzie.
- **Opcje banku w konfiguracji:** `bank_rce_price`, `bank_initial_kwh`, `bank_initial_pln`, `rce_auto_fetch` dodane do formularza cen w Options → Set Energy Prices.
- **Atrybuty stanu banku:** Bank kWh i PLN pokazują teraz szczegółowe atrybuty (`net_import_kwh`, `net_export_kwh`, `coefficient`, `bilans_kwh`, `initial_kwh`, `source`).
- **`obis_balance` (BP) i `obis_yearly` (WytworzonaOddana):** Dodano wykrywanie dodatkowych kodów OBIS z API.
- **`is_prosumer` w meter_obj:** Detekcja prosumenta po `type: "Wytwórca"` w agreementPoints.

### 🔧 Changes
- `api.py`: `meter_obj` zawiera teraz `activation_date`, `is_prosumer`, `obis_balance`, `obis_yearly`
- `api.py`: `async_find_first_data_date` używa `activation_date` zamiast `activationDate`
- `sensor.py`: Dodano `EnergaRceSensor` — sensor RCEm z auto-fetch z PSE
- `config_flow.py`: Dodano opcje banku w formularzu cen (G12W i G11)
- `strings.json`, `pl.json`: Dodano tłumaczenia nowych opcji banku

## v0.2.6 (2026-09-02)

### 🐛 Bug Fixes
- **Unknown error po Wykryj → Zatwierdź:** Brak `from . import _import_meter_history` w `async_step_detect_first` → `NameError` na `Zatwierdź` po `Wykryto: 2025-01-01`. Dodano import.

## v0.2.5 (2026-09-02)

### 🐛 Bug Fixes
- **Puste pole w menu Opcji i ukryta Data:** Brak `detect_first` w `translations/pl.json` → puste pole między `Pobierz Historię` a `Wyczyść Statystyki` (zrzut 18:06). Dodano tłumaczenie + przeniesiono `Data pierwszego odczytu` z `diagnostic` na widoczne (`None`) + dopisano `Proszę czekać — aplikacja próbuje znaleźć datę pierwszego odczytu (~15 sekund).` w `strings.json`/`pl.json` dla `Podaj dane logowania` (lab: login z wielkiej litery długo myśli).

## v0.2.4 (2026-09-02)

### ✨ New Features
- **Encja Data pierwszego odczytu + przycisk Wykryj:** Dodano `sensor.energa_XXX_data_pierwszego_odczytu` (`device_class: date`) i `button.energa_XXX_wykryj_pierwszy_odczyt` (pushbutton) per licznik. Data wykrywana automatycznie podczas konfiguracji (hierarchicznie rok→pół→miesiąc→dzień, ~14 requestów, komunikat `Wykrywam... ~15s`), zapisywana w `entry.options["first_data_date"]` i proponowana jako `default` w `Pobierz Historię`. Jeśli nie pasuje — wyłącz encję, przycisk zawsze pod ręką.

## v0.2.3 (2026-09-02)

### ✨ New Features
- **Wykryj pierwszy odczyt (hierarchicznie):** Nowa opcja `Konfiguruj → Wykryj pierwszy odczyt` w menu Opcji. Zamiast liniowego `for day in range(days)` (365 requestów), sprawdza rok → półrocze → miesiąc → dzień pojedynczymi `mchart` na środku okresu (`1 request/poziom`, ~14 requestów dla 5 lat, `sleep 0.7s` dyskretnie). Dyskretne i szybkie — nie zarzuca serwera. Zwraca `05.05.2023` dla przykładu z issue.

### 🐛 Bug Fixes
- **Login case sensitivity (retry):** Poprawiono logowanie z zachowaniem oryginalnej wielkości liter — najpierw próba z wpisanym loginem, a dopiero przy `invalid_auth` druga próba z `lower()`. Dzięki temu `User@Example.com` zadziała jako `user@example.com` bez nadpisywania poprawnych kont z wielkimi literami.

## v0.2.2 (2026-09-02)

### 🐛 Bug Fixes
- **Login case sensitivity:** Emaile są case-insensitive (RFC 5321), ale `api-mojlicznik` jest case-sensitive. Dodano normalizację `strip().lower()` w `config_flow.py` i `api.py`, aby logowanie `User@Example.com` działało jako `user@example.com`.

## v0.2.1 (2026-09-02)

### 🐛 Bug Fixes
- **Login case sensitivity:** Emaile są case-insensitive (RFC 5321), ale `api-mojlicznik` jest case-sensitive. Dodano normalizację `strip().lower()` w `config_flow.py` i `api.py`, aby logowanie `User@Example.com` działało jako `user@example.com`.

## v4.15.2 (2026-07-15)

### 🐛 Bug Fixes
- **#34 — Cost always 0 PLN for G12w tariff:** The options form for configuring energy prices displayed only a single "import price" field (G11 mode) when `_has_multi_zone_meters()` returned `False` on first entry after HA restart (API data not yet loaded). This caused `import_price_1`/`import_price_2` keys to never be saved, resulting in cost statistics always being written as 0 PLN. Fixed by adding two additional detection paths: (1) a persistent `has_multi_zone` hint saved to options when prices are successfully configured, and (2) a check for the presence of an existing `import_price_1` key in options. The live API query remains as final fallback. Affects users with G12w tariff and no photovoltaics.

## v4.15.1 (2026-05-17)

### 🐛 Bug Fixes
- **#31 — `ConfigEntryNotReady` on Energa server outage:** When the Energa API returns `success=false` due to a temporary server outage (not invalid credentials), the integration now raises `ConfigEntryNotReady` (automatic retry) instead of `ConfigEntryAuthFailed` (permanent block requiring manual re-authentication). The fix checks whether the API error message contains auth-related keywords (`login`, `password`, `credentials`, `auth`). Plain `success=false` without an auth error is treated as a server issue.

## v4.13.0 (2026-03-30) - DST Hour Fix & Prosumer Balance Redesign

### 🐛 Bug Fixes
- **#26 — DST hour mapping:** On spring-forward days, Energa API returns 23 hourly points with correct Unix timestamps. Previous code used array indices as hour numbers, causing all hours after the 2→3 AM gap to shift by −1h and hour 23 data to be dropped. Fixed by using API-provided `tm` timestamps instead of index-based hour construction.

### ✨ New Features
- **#27 — Prosumer balance with configurable baselines:** Redesigned `Bilans Prosumencki` to use meter totals minus user-configured baselines instead of incomplete statistics sums. New formula: `(export − baseline_export) × coefficient − (import − baseline_import)`. New config options `Baseline Import/Export (kWh)` in Options → Prices. Default 0 = lifetime calculation (backward compatible).

### 🔧 Changes
- Removed complex `_get_stats_sums()` and entity_registry lookups from prosumer balance (~50 lines removed)
- Rich state attributes on balance sensor: full breakdown of meter, baseline, net, effective values
- Added `include_timestamps` mode to `_fetch_chart()` for timestamp-based hour mapping
- Added baseline field translations (strings.json, en.json, pl.json)

### 🧪 Tests
- **40 tests** (was 24): +16 DST tests (`test_dst.py`), +1 G11 Lab real-world test, +3 baseline tests
- New `TestDSTHourMapping` class: spring-forward (23h), fall-back (25h), normal day coverage

## v4.12.1 (2026-03-29) - Critical G12W Bug Fixes

### 🐛 Bug Fixes
- **Export price mapping (G12W):** `export_1`/`export_2` zones were incorrectly charged at **import price** (1.188 PLN/kWh) instead of export price (0.95 PLN/kWh). Fixed `get_price_for_key()` to explicitly map per-zone export keys.
- **Prosumer balance (G12W):** For multi-zone tariffs, export sum was always **0** because the code searched for a single `export` entity instead of summing `export_1` + `export_2`. Prosumer balance now correctly aggregates per-zone exports.
- **DST spring-forward crash:** During DST transition (e.g. March 29), local hours 02:00 and 03:00 both mapped to the same UTC hour after `as_utc()` conversion, causing duplicate `start_ts` entries. The recorder crashed with `StaleDataError`. Fixed by merging duplicate UTC timestamps in `build_statistics()`.
- **Token expired log noise:** Downgraded "Token expired" messages from WARNING to DEBUG across `api.py`, `sensor.py`, and `__init__.py` to reduce log clutter from normal API session rotation.

### 📝 Documentation
- Fixed cost sensor names in README: `Cost` → `Koszt` (import) / `Rekompensata` (export) to match actual code
- Fixed Troubleshooting section: removed stale `*_cost` entity_id references
- Fixed API Reference: `zones[]` was documented as a request parameter, but the integration reads per-zone data from the response array client-side
- Fixed API Reference response example: now shows multi-zone `zones` array
- Fixed CHANGELOG: corrected HACS PR reference (#5416 → #5727)

## v4.12.0 (2026-03-28) - Per-Zone Export Sensors

### ✨ New Features
- **Per-zone export sensors for G12W:** New sensors `Panel Energia Produkcja Strefa 1` and `Panel Energia Produkcja Strefa 2` for multi-zone tariffs (G12W, G12, G12AS, G12R). Export data is fetched from the chart API using the `zones[]` array, matching the import zone pattern.
- **Per-zone export statistics:** Chart data for export is now fetched per zone (zone_index=0/1), enabling proper per-zone per-hour energy tracking in HA's long-term statistics.

### 🔧 Changes
- Coordinator totals now include `export_1`/`export_2` for multi-zone meters.
- Pre-fetched statistics now cover `export_1`/`export_2` suffixes for smart fetch optimization.
- G11 (single-zone) meters continue to use a single `export` sensor (no behavior change).

### 🧪 Tests
- 53 tests (was 46). Added `TestChartZoneData` class with 6 tests based on real API data from G12W account.
- Updated sensor creation logic tests to verify per-zone export keys.


## v4.11.0 (2026-03-27) - Bug Fixes & Prosumer Balance

### 🐛 Bug Fixes
- **#25 — HTTP 403 loop:** After re-login on token expiry, retry request still used the old (expired) token because params were built before the retry loop. Fixed by moving params computation inside the loop.
- **#25 — Database executor warning:** Changed `hass.async_add_executor_job(get_last_statistics, ...)` to `recorder.get_instance(hass).async_add_executor_job()` — HA requires DB operations to go through the recorder's own executor pool.
- **#23 — "Unknown error" on login:** `AbortFlow` from `_abort_if_unique_id_configured()` was caught by the generic `except Exception` handler, showing "Unknown error" instead of "Already configured". Added explicit `AbortFlow` re-raise.
- **Prosumer balance sensor:** Removed incompatible `device_class=ENERGY` (balance can be negative, incompatible with `state_class=measurement`).
- **Duplicate attributes:** `EnergaProsumerBalanceSensor` had two `extra_state_attributes` definitions — the second (generic meter info) was overriding the first (prosumer balance breakdown).

### ✨ New Features
- **Prosumer Balance sensor:** `Bilans Prosumencki` — tracks net billing balance (export × coefficient − import) in kWh.
- **Prosumer coefficient:** Configurable via Options Flow (default 0.8 = 80%).
- **Per-meter pricing:** Support for meter-specific price overrides in Options Flow.

## v4.10.2 (2026-03-25) - Stale Device Cleanup
- Auto-remove stale devices after account change

## v4.10.1 (2026-03-25) - Meter Readings Fix
- Fix: auto-refresh meter total readings on every cycle (closes #20, #22)

## v4.10.0 (2026-03-24) - Per-Meter Pricing UI
- Per-meter pricing UI in config_flow with `_get_active_meters` helper

## v4.9.0 (2026-03-24) - Per-Meter Pricing Wiring
- Wire `meter_id` to `get_price_for_key` in all callers (3 files)

## v4.8.0 (2026-03-24) - Per-Meter Pricing Foundation
- Per-meter pricing support in `get_price_for_key` (backward compatible)

## v4.7.2 (2026-03-21) - Login Timeout
- Add 30s login timeout + session cleanup on error

## v4.7.1 (2026-03-20) - Spike Guard
- Prevent spike on partial import — extend to today for sum continuity
- Add spike guard to history import

## v4.7.0 (2026-03-20) - Options Flow Fixes
- Add `async_unload_entry` and update listener (closes #17, #19)
- API warning/error capture with persistent notifications

## v4.6.0 (2026-03-19) - Options Preservation
- Fix: Options flow now preserves prices (closes #18)

## v4.5.1 (2026-03-14) - Name Unification
- Rename integration to "Energa My Meter API (Mój Licznik API)"
- Update README for HACS Default, clean up .gitignore

## v4.5.0 (2026-03-08) - Session Resilience
- Session resilience — auto-recovery on closed session and token expiry
- Unified tariff documentation (G12/G12w/G12r)

## v4.4.1 (2026-02-21) - G12w Bugfixes & Code Cleanup

### 🐛 Bug Fixes
- **Statistics spike fix:** `_get_anchor()` was double-counting already-imported data, causing cumulative sum to grow exponentially each coordinator cycle
- **Zero-consumption hours:** `bool(0.0)` evaluates to `False` in Python — hours with 0 kWh were silently skipped. Fixed to `if hourly_value is not None and hourly_value >= 0:`
- **Negative deltas at boundary:** Backward-from-meter_total calculation created negative deltas at the boundary between `fetch_history` and coordinator data
- **Negative sums for new zones:** Backward calculation caused negative sums for newly activated tariff zones (e.g., G12w zone 2 started at -12.886 kWh)
- **Clear stats now includes costs:** `async_clear_statistics()` was missing `_cost` statistic IDs, leaving orphaned cost data

### 🔧 Code Quality
- **Forward-from-zero calculation:** Replaced backward anchor-based calculation with forward-from-zero approach — guarantees monotonically increasing, non-negative sums
- **Deduplicated price logic:** Extracted `get_price_for_key()` helper in `const.py`, replacing identical code in 3 files
- **Rate limiting:** Added 0.3s delay between API requests in coordinator path to prevent throttling
- **Spike guard constant:** Replaced hardcoded `100` with `MAX_HOURLY_KWH` constant, added warning log
- **Dead code cleanup:** Removed unused `resolve_entity_id()`, `_tz`, `UTC` constant, anchor parameters

> **Note:** Forward-from-zero produces identical Energy Dashboard results (HA uses sum differences). No user action required after update.

## v4.4.0 (2026-02-19) - G12w Multi-Zone Tariff Support

### ✨ New Features
- **G12w multi-zone tariff support:** Automatic detection and separate tracking of peak (zone 1) and off-peak (zone 2) consumption
- **Zone-specific pricing:** Configurable prices per zone via Options Flow
- **New sensors for G12w:** `Panel Energia Strefa 1`, `Panel Energia Strefa 2` with corresponding cost sensors
- **Zone-aware history import:** Downloads and imports zone-specific hourly data

### 🔧 Changes
- Options Flow dynamically shows zone-specific or single price fields based on detected meter type
- `clear_stats` extended to include zone-specific statistic IDs

## v4.3.10 (2026-02-13) - Negative Cost Fix

- Fixed: Negative cost values appearing in Energy Dashboard
- Root cause: Cost statistics not being cleared/recalculated when energy statistics were updated
- Affects: Users who previously ran history import and saw negative PLN values

## v4.3.9 (2026-02-11) - Hour Offset Fix

- Fixed: Hourly statistics were shifted +1 hour compared to Energa app
- API index 0 = 00:00-01:00, was incorrectly mapped to 01:00 (now correctly maps to 00:00)
- Affects: Energy Dashboard hourly bars, Panel Energia statistics
- After update: clear statistics and reimport history (30 days) for correct alignment

## v4.3.8 (2026-02-11) - Session Isolation Fix

- Fixed: Use dedicated HTTP session instead of shared HA session
- Prevents `cookie_jar.clear()` from affecting other integrations
- Session properly closed on entry unload and HA shutdown

## v4.3.7 (2026-02-10) - HACS Validation Fix

- Fixed: Removed extra keys from `hacs.json` (only `name`, `render_readme`, `country` allowed)
- Version bump for clean release tag

## v4.3.6 (2026-02-06) - HACS Compliance Release

- Documentation: Native API emphasis in README, English API reference
- Security: Removed sensitive keys and credentials from repository
- Branding: Updated logo and icon to Energa | GRUPA ORLEN identity
- Submitted to HACS default repository (PR #5727 — merged 2026-03-12)

## v4.3.5 (2026-01-28) - Energy Dashboard Spike Fix

- Synced LAB-verified code to fix remaining Energy Dashboard spikes
- Validated on both prosumer and consumer accounts

## v4.3.4 (2026-01-27) - StatisticsBuilder

- Added `StatisticsBuilder` class for incremental sum calculation
- Prevents negative statistics spikes caused by backup/restore cycles
- Anchor-based backward calculation from current meter reading

## v4.3.3 (2026-01-26) - Negative Statistics Fix

- Resolved negative statistics appearing in Energy Dashboard
- Root cause: sum resets after HA backup restoration
- Statistics now rebuild cleanly from meter totals

## v4.2.4 (2026-01-25) - Entity ID Pattern Fix

- Corrected `entity_id` pattern in history import to match PROD sensors
- Changed from `energa_zuzycie` to `panel_energia_zuzycie` pattern

## v4.2.3 (2025-12-28) - State Class Restoration

- Restored `state_class` for Energy Dashboard compatibility

## v4.2.2 (2025-12-28) - Entity Filter Fix

- Corrected entity_id filter to match `panel_energia_` pattern
- Removed incorrect `_stats` requirement from clear_stats filter

## v4.2.1 (2025-12-27) - Statistics Initialization

- Simplified statistics fix with forward calculation in `build_statistics`
- Removed `state_class` from Panel Energia sensors to prevent UNIQUE constraint errors
- Accepted history catch-up spike as expected behavior on first import

---

## v4.2.0 (2025-12-27) - Cost Statistics Fixes & Documentation

> **Note:** This is a **minor release** after v4.1.0, including critical bugfixes and comprehensive documentation improvements.

### 🐛 Critical Bug Fixes

#### 1. NULL Timestamps in Cost Statistics

**Problem:** Cost statistics were being imported to the database but with NULL `start_ts` timestamps, 
making them invisible in the Energy Dashboard (0.00 zł displayed for all periods).

**Root Cause:**

The issue was caused by incorrect creation of `StatisticData` objects in the `build_statistics()` function:

```python
# WRONG - Constructor syntax creates object, not TypedDict
StatisticData(start=datetime_obj, sum=value, state=value)

# CORRECT - Plain dict, as expected by Home Assistant's internal API
{"start": datetime_obj, "sum": value, "state": value}
```

Home Assistant's `StatisticData` is defined as a `TypedDict` (in `homeassistant/components/recorder/models/statistics.py`).
When called as a constructor like `StatisticData(...)`, Python does NOT create a dict - it creates a TypedDict 
type hint object. The internal HA code in `db_schema.py` uses `stats["start"].timestamp()` to convert the 
datetime to a Unix timestamp. When `stats` is not a proper dict, this access fails silently and `start_ts` 
becomes NULL.

**Solution:**
- Changed from `StatisticData(...)` constructor to plain dict `{...}` format
- Added `homeassistant.util.dt` import for proper timezone handling
- Used `dt_util.as_utc()` for UTC timezone conversion

#### 2. Incorrect Meter ID in Entity Names

**Problem:** Historical statistics were imported under wrong sensor names (e.g. `sensor.energa_123456_*` 
stead of `sensor.energa_12345678_*`), causing Energy Dashboard to show only partial data.

**Root Cause:**

The `_import_meter_history()` function was using `meter["meter_point_id"]` for building entity IDs:

```python
# WRONG - meter_point_id is API-internal identifier (e.g. 123456)
meter_id = meter["meter_point_id"]
entity_id = f"sensor.energa_{meter_id}_energa_zuzycie"
```

Two identifiers exist in meter data:
- `meter_point_id` (e.g. 123456) - API-internal identifier for communication
- `meter_serial` (e.g. 12345678) - Real meter number visible to user

**Solution:**
- Separated the two identifiers:
  - `meter_point_id` - used only for API calls (`async_get_history_hourly()`)
  - `meter_serial` - used for building user-facing entity IDs
- This matches the original v4.0.2 logic

### 🔧 Additional Fixes
- Fixed Energy Dashboard entity references (removed incorrect `_2` suffix from cost sensor names)
- Updated dictionary access pattern from attribute notation (`.start`, `.state`) to key notation (`["start"]`, `["state"]`)
- Added token expiry handling in Options Flow history import
- Renamed "Reimportuj Statystyki" button to "Wyczyść Statystyki Panelu Energia" for clarity

### 📝 Files Modified
- `__init__.py` - Fixed StatisticData creation, timezone handling, and meter ID usage
- `config_flow.py` - Added token expiry handling, renamed clear_stats button
- `translations/pl.json` - Updated Polish translations
- `translations/en.json` - Added missing English translations

---

## v4.0.2 (2025-12-22) - STABLE RELEASE

**This is a complete rewrite of the integration (Clean Rebuild).**

### 🚀 Key Changes
*   **Architecture:** Simplified sensor logic. Split into "Live Sensors" (for viewing current data) and "Statistics Sensors" (invisible, strictly for Energy Dashboard).
*   **Statistics Repair:** Implemented "Anchor-Based Backward Calculation". Statistics are now calculated by taking the *current* meter reading and subtracting hourly values backwards. This guarantees that **cumulative sums in Home Assistant always match the physical meter reading**, eliminating "negative spikes" and data corruption.
*   **Self-Healing:** The "Download History" (Pobierz Historię) tool now acts as a **repair mechanism**. If your Energy Dashboard shows incorrect spikes, running "Download History" will overwrite the bad data with correctly calculated statistics.

### ✨ New Features
*   **6 Sensors:** 
    *   `Import Total` & `Export Total` (Live readings)
    *   `Daily Import` & `Daily Export` (Live daily counters)
    *   `Panel Energia Import` & `Panel Energia Export` (Invisible, for Dashboard only)
*   **Options Flow:** Configure credentials and run history import directly from Integration Settings.

### 🐛 Bug Fixes
*   Fixed critical bug where `api.py` was generating cumulative sums starting from 0, causing massive spikes when compared to lifetime totals.
*   Fixed `AwesomeVersion` comparison error.
*   Fixed "Unknown" state for live sensors by adding proper `SensorEntity` inheritance.

### 🧹 Cleanup
*   Removed all beta simulation scripts and legacy debug tools.
*   Removed complex "source switching" logic - v4.0 uses a single, robust source of truth.

---

## v3.x Legacy
*   Archived. Please upgrade to v4.0.2 and run "Download History" to clean up your database.
