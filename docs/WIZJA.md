# Wizja i Architektura — Bank na żywo + Prognoza rachunku

**Status:** cel docelowy (nie zaimplementowane). Stan na 2026-09-03: integracja `v0.2.11`
ma natywny Bank (kWh/PLN, weryfikacja fakturowa w `docs/BANK.md`) i zalążek
`Prognozy Rachunku` (tylko energia czynna). Ten dokument definiuje dokąd zmierzamy.

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

**Architektura docelowa (v0.2.13):** moduł `tariff.py` — tabela opłat
(per taryfa G12W/G11, wartości domyślne z faktur + edycja w `Options`):

- `EnergaBillSensor` (`Prognoza Rachunku Brutto`, PLN, reset miesięczny):
  stan = koszt MTD brutto; atrybuty = pełny rozkład jak sekcje faktury
  (`sprzedaz_energia`, `akcyza`, `oplata_handlowa`, `dystrybucja_zmienna`,
  `oplaty_stale`, `vat`, `depozyt_pokrycie`, `do_zaplaty_forecast`
  z ekstrapolacją liniową na koniec miesiąca).
- Stary system: pobór do wysokości Banku = koszt energii `0`
  (dystrybucja i stałe płatne zawsze — jak faktura Wiśniowej).
- Nowy system: depozyt MTD (`export×RCEm×1.23`) pomniejsza tylko energię
  czynną, nie dystrybucję ani opłaty stałe.

**Kryterium akceptacji:** lipiec Agrestowej odtworzony z licznika
z dokładnością ±5% do `422.52` brutto (granica: bilans godzinowy
sprzedawcy vs delty licznika + zmiany cen w trakcie miesiąca).

## Mapa drogowa

- `v0.2.12` — natywne sensory ładowania/rozładowania Banku → bateria
  na żywo w Panelu Energia; fix `RCEm monetary+measurement`.
- `v0.2.13` — `tariff.py` + pełna prognoza rachunku brutto z rozkładem.
- Potem — migracja prod na fork, usunięcie `bank_energii.yaml`.

## Pytania otwarte

- Opłata mocowa vs moc umowna (12.5 vs 16.5 kW) — stała w opcjach czy
  wyliczana? (dziś stała z faktury).
- Zmiany cen w trakcie miesiąca — proporcja dniowa czy cena z końca miesiąca?
- G11 (jednostrefowa) — ten sam moduł, uproszczony formularz.
