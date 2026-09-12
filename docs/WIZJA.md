# Wizja i Architektura — Bank na żywo + Prognoza rachunku

**Status:** Wszystkie cele zrealizowane i wdrożone produkcyjnie (`v1.6.0` – `v1.6.9`). Stan na wrzesień 2026: integracja posiada pełny, natywny model syntetycznego magazynu energii w Panelu Energia (`synthetic_storage.py`), 1-klik autokonfigurator (`button.py`), predykcję rachunku brutto profilem godzinowym WAL z bezblokadową pętlą zdarzeń (`HourlyProfileForecaster`), automatyczne pobieranie cen RCEm/RCE z PSE OIRE oraz ujednolicony pulpit Centrum Rozliczeń.
Zweryfikowano z rzeczywistymi fakturami OSD na środowisku produkcyjnym i 5 maszynach laboratoryjnych (taryfy G11, G12, G12w, Net-metering, Net-billing, konto demo Energa Operator).

## Cel 1 — Magazyn na żywo w Panelu Energia

**Wymaganie:** wskaźnik naładowania ma pokazywać, ile w danej chwili mam
zmodyfikowanej energii w wirtualnym magazynie Energa Operator — na żywo,
w Panelu Energia, jak bateria.

**Problem dziś:** `sensor.*_bank_kwh/pln` to STAN (`state_class: TOTAL`).
Panel Energia → Bateria wymaga PRZEPŁYWÓW: dwóch liczników
`total_increasing` (ładowanie / rozładowywanie w kWh). Dlatego na labie
skonfigurowano tylko `grid` (zużycie/oddanie per strefa), bez baterii —
prod używa do tego protezy `bank_energii.yaml`
(`sensor.bank_ladowanie/rozladowanie`).

**Architektura docelowa (v0.2.12):** natywne sensory przepływów w integracji,
liczone z delty `Bilansu` między odczytami koordynatora (co godzinę):

- `EnergaBankChargeSensor` (`..._bank_ladowanie`, kWh, `total_increasing`):
  narost, gdy delta Bilansu > 0 (nadwyżka trafia do magazynu).
- `EnergaBankDischargeSensor` (`..._bank_rozladowanie`, kWh, `total_increasing`):
  narost, gdy delta Bilansu < 0 (pobór z magazynu).
- Stary system (net-metering 0.8): przepływy w kWh po współczynniku
  (`export×0.8` wchodzi, `import` schodzi) — bateria 1:1 ze stanem Bank kWh.
- Nowy system (net-billing): bateria energetyczna w kWh (ilościowa) +
  osobno stan depozytu w PLN (`Bank PLN` już jest). Bateria pokazuje ILOŚĆ,
  depozyt pokazuje WARTOŚĆ — dwa uzupełniające się widoki, bo RCE zmienia
  wartość tych samych kWh z miesiąca na miesiąc.
- Przetrwanie restartu: sensory `TOTAL` odbudowują się z `last_reset`/historii
  (jak liczniki energii), bez `bank_energii.yaml` do usunięcia na prod.

**Kryterium akceptacji:** po `Pobierz Historię` Panel Energia → Bateria
pokazuje naładowanie = `Bank kWh` (±1% przybliżenie godzinowe, patrz BANK.md).

## Cel 2 — Prognoza rachunku jak z faktury

**Wymaganie:** wiedzieć, jakiego rachunku się spodziewać — z WSZYSTKIMI
pozycjami z faktury, nie tylko energią czynną.

**Inwentaryzacja opłat (G12W, z faktur 2026, netto):**

1. Sprzedaż energii (Energa Obrót):
   - energia czynna dzienna/nocna (cena/kWh, np. G12W-nowe 07: `0.6107/0.3990`;
     G12W-stare: pobór pokryty magazynem → `0 kWh` do zapłaty),
   - akcyza `5.00 PLN/MWh` od poboru D/N,
   - opłata handlowa (miesięczna, np. `16.18`).
2. Dystrybucja (OSD):
   - abonamentowa (miesięczna, np. `0.70–0.74`),
   - sieciowa stała (miesięczna, np. `20.17`),
   - sieciowa zmienna dzienna/nocna (np. `~0.4017/~0.0851` za kWh),
   - jakościowa (od całości pobranych kWh, np. `0.0332`),
   - OZE (od całości kWh, `0.0073`), kogeneracyjna (`0.0030`),
   - mocowa (miesięczna, np. `24.05`; zależy od mocy umownej: 12.5/16.5 kW).
3. VAT `23%` od całości, minus depozyt (net-billing) lub pokrycie
   z magazynu (net-metering). Odsetki za zwłokę — poza zakresem.

Weryfikacja na fakturach: G12W-nowe 07 (`195.06` sprzedaż + `148.45`
dystrybucja = `343.51` netto → `422.52` brutto − `147.44` depozyt + `0.08`
= `275.16` ✓); G12W-stare 05–06 (`127.10` netto → `156.33` brutto ✓,
energia `0` bo z magazynu).

**Architektura (v0.2.14, zaimplementowane):** moduł `tariff.py` — tabela opłat
(per taryfa G12W/G11, wartości domyślne z faktur + edycja w `Options`):

- `EnergaBillForecastSensor` (`Prognoza Rachunku`, PLN): stan = prognozowana
  dopłata na koniec miesiąca (`do_zapłaty`); atrybuty = pełny rozkład MTD
  i prognozy jak sekcje faktury (`mtd_sale_total`, `mtd_distr_total`,
  `mtd_netto`, `mtd_vat`, `mtd_brutto`, `mtd_deposit`, `mtd_do_zaplaty`,
  `forecast_brutto`, `forecast_do_zaplaty`, `cover_day/night`)
  z ekstrapolacją liniową przepływów na koniec miesiąca.
- Stary system: pobór do wysokości Banku = koszt energii `0`
  (dystrybucja i stałe płatne zawsze — jak faktura G12W stare zasady).
- Nowy system: depozyt MTD (`export×RCEm×1.23`) pomniejsza tylko energię
  czynną, nie dystrybucję ani opłaty stałe.

**Kryterium akceptacji:** lipiec G12W nowe zasady odtworzony z licznika
z dokładnością ±5% do `422.52` brutto (granica: bilans godzinowy
sprzedawcy vs delty licznika + zmiany cen w trakcie miesiąca).

## Zrealizowana Mapa Drogowa

- `v0.2.12` – `v0.3.4`: wczesne prototypy natywnego banku i rozliczania faktur.
- `v1.4.1` – `v1.5.0`: automatyczny bank zero-config FIFO z oficjalnego API Energi (`mchart`), tryb daty faktury, odporny backfill do 730 dni.
- `v1.6.0` – `v1.6.3`: natywny model syntetycznego magazynu energii w Panelu Energia (`synthetic_storage.py`), onboarding survey, 1-klik autokonfigurator (`button.py`), ochrona przed spadkami sum i ujemnymi anomaliami w `RecorderAdapter`.
- `v1.6.4` – `v1.6.7`: dyskretny 15-minutowy cykl odpytywania API, precyzyjne wsparcie G11 Net-billing, odporny parser HTML dla PSE OIRE, oficjalny standard pulpitu 'Centrum Rozliczeń'.
- `v1.6.8` – `v1.6.9`: normalizacja slugów encji (gotowość na HA 2027.2 / 2026.11), obsługa kont demo OSD z pustymi rejestrami eksportu, asynchroniczny forecaster w wątkach roboczych (`async_add_executor_job`), buforowanie RCE i optymalizacja zapytań SQL bazy Recorder.

## Podsumowanie Pytań Projektowych

- **Opłata mocowa vs roczny pobór:** ROZWIĄZANE — wdrożono automatyczną klasyfikację progu rocznego URE 2026 (`capacity_for_annual_use`) w oparciu o historię odczytów licznika, z możliwością ręcznego nadpisania w Opcjach.
- **Zmiany cen w trakcie miesiąca:** ROZWIĄZANE — zastosowano ważone ceny strefowe oraz proporcjonalne rozliczanie MTD.
- **Taryfy jednostrefowe (G11) i demonstracyjne konta OSD:** ROZWIĄZANE — pełna obsługa G11 (zarówno konsumentów, jak i prosumentów net-billingowych) oraz obsługa oficjalnego konta demonstracyjnego Energa Operator z wirtualnym licznikiem.
