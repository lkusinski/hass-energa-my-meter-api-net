# Bank / Wirtualny Magazyn Energii — jak czytać

**Cel:** w łatwy sposób widzieć ile prądu / kasy masz w magazynie.

## Dwa systemy w jednej integracji (auto-wykrywanie po `prosumer_coefficient`)

| Instalacja | Taryfa | System | Sensor | Jednostka | Formuła |
|---|---|---|---|---|---|
| G12W | **stare** net-metering roczny | `sensor.bank_wirtualny_kwh` (`energa_<nr-licznika>_bank_kwh`) | kWh | `max(0, (export-baseline)×0.8 - (import-baseline)) + initial_kwh` |
| G12W | **nowe** net-billing miesięczny | `sensor.bank_wirtualny_pln` (`energa_<nr-licznika>_bank_pln`) + `sensor.energa_<nr-licznika>_rcem_auto` | PLN | `initial_pln + export×RCE×1.23 - import×cena_strefa` |

> `<nr-licznika>` — podstaw numer swojego licznika. `G11 Odbiorca`
> (taryfa jednostrefowa, sam pobór) nie dostaje banku — tylko prosumenci.

* `initial_kwh` = **1358** (`752+606` `Razem w magazynie` z faktury `06.2026` — stan po rozliczeniu). Ustaw `balance_baseline_import/export` na wskazania `do` z tej faktury + `bank_initial_kwh=1358`. `Bilans>0` nadbudowuje bank.
* `initial_pln` = `0.00` na `01.08.2026` (faktura `07.2026`: `456×0.26288×1.23=147.44`, `Depozyt po 0.00`). RCEm `0.26288` lipiec, `×1.23` od noweli 27.11.2024 Dz.U.1847.
* Per-strefa G12W: `import_1/export_1` (L1 droga) + `import_2/export_2` (L2 tania). Ceny `import_price_1 1.30` / `import_price_2 0.65` w `Options → Ceny` (lub zostaw `1.2453/0.5955` default — ujednolicisz).

## Gdzie zobaczyć

**Encje:** `Deweloperskie → Stany` → `sensor.energa_*_bank_*` — wartość + `Atrybuty`: `net_import_kwh`, `net_export_kwh`, `bilans_kwh` / `rce_price`, `import_cost_pln`, `per_strefa_note`.

**Lovelace — wklej do `Pulpity → Edytuj → + Karta → Ręcznie`:**

```yaml
type: vertical-stack
cards:
  - type: entities
    title: 🔋 Magazyn Wirtualny — G12W stare zasady (0.8)
    entities:
      - entity: sensor.bank_wirtualny_kwh
        name: Bank kWh (do odebrania)
        icon: mdi:battery-charging-80
      - entity: sensor.energa_<nr-licznika>_bilans_prosumencki
        name: Bilans (export×0.8 - import)
      - entity: sensor.bank_ladowanie
        name: Ładowanie (do Baterii w Panelu Energia)
      - entity: sensor.bank_rozladowanie
        name: Rozładowanie (do Baterii w Panelu Energia)
  - type: gauge
    entity: sensor.bank_wirtualny_kwh
    min: 0
    max: 5000
    severity:
      green: 1500
      yellow: 500
      red: 0
    name: Bank kWh

  - type: entities
    title: 💰 Magazyn — G12W nowe zasady (RCE×1.23)
    entities:
      - entity: sensor.bank_wirtualny_pln
        name: Depozyt PLN (ujemny = do zapłaty)
      - entity: sensor.energa_<nr-licznika>_rcem_auto
        name: RCEm PLN/kWh (PSE auto)
      - entity: sensor.energa_<nr-licznika>_bilans_prosumencki
        name: Bilans kWh
  - type: markdown
    content: >
      RCE auto: `Options → rce_auto_fetch` lub ręcznie `bank_rce_price`.
      Aktualizuj co miesiąc (PSE publikuje ~11. dnia). Formuła w atrybutach encji.
```

**Energy Dashboard — bateria (opcjonalnie):**
`Ustawienia → Pulpity → Energia → Sieć → Dodaj zużycie` `Panel Energia Strefa 1/2` + osobno `Bateria` jeśli chcesz słupki ładowania/rozładowania. Bank `sensor.*_bank_*` jest już `state_class: TOTAL` i nadaje się jako bateria, ale i tak najczytelniej jest karta `gauge` powyżej.

## Opcje integracji

`Ustawienia → Urządzenia → Energa → Konfiguruj → Ustaw Ceny Energii`:
* `prosumer_coefficient` `0.8` stara / `0.0` nowa,
* `balance_baseline_import/export` = stan licznika na fakturze początkowej (0 = lifetime),
* `bank_initial_kwh` / `bank_initial_pln` z faktur,
* `bank_rce_price` np. `0.26288` + `rce_auto_fetch` (24h cache w coordinatorze, fallback manual).

`Wykryj pierwszy odczyt` (`button.energa_*_wykryj_pierwszy_odczyt`) — hierarchicznie `today-730d` → `~14 req` `mchart` `0.7s`, nie `2020`.

## Weryfikacja z fakturami

* Faktura G12W-stare `01.05–30.06.2026`: `Razem w magazynie L1/L2` = `752+606=1358`. Baseline = wskazania `od` faktury lub `do`; bank start `1358 + (licznik-baseline)×0.8`.
* Faktura G12W-nowe `07.2026`: `456×0.26288×1.23=147.44` → `Depozyt po 0.00` → bank PLN start `0.00`, potem `export×RCE×1.23 - import×cena` per strefa. Sprawdź w `Deweloperskie → Stany → Bank PLN atrybuty`.

> Po `v0.2.10` możesz usunąć `packages/bank_energii.yaml` — bank jest natywny.

## Lab zweryfikowany 2026-09-03 08:40

- `sensor.bank_wirtualny_kwh` 2472.18 kWh (G12W stare, baseline per-strefa ze wskazań `do` faktury 06.2026), `sensor.bank_wirtualny_pln -415.57 PLN` (G12W nowe, baseline ze wskazań `do` faktury 07.2026, RCE `0.59287` auto — przed fixem RCEm w v0.2.11), `sensor.energa_<nr-licznika>_rcem_auto 0.59287`
- Czyszczenie `core.entity_registry` przy `ha core stop` (`jq` na `/mnt/data/supervisor/homeassistant/.storage/`): usunięto 3 orphan `bank_pln` + 3 duplikaty `button` `serial` → 3 banki + 3 `data_pierwszego_odczytu` + 3 `button` (point_id)

## Dalej — v0.2.11 autokalibracja rozliczeń (FIFO 12 m-cy)

> Reset „1 stycznia" (stare) i „co miesiąc" (nowe) byłyby NIEZGODNE z przepisami.
> Oba systemy to kroczące okna FIFO 12 m-cy. Włącz w `Options → Ceny`:
> `enable_auto_settlement`, ustaw `settlement_date` (np. `2026-06-30`),
> dla starych opcjonalnie `use_rolling_365d` (wymaga `Pobierz Historię`).

* Stare: bank z ostatnich 365 dni statystyk (`rolling_365d`), atrybuty
  `settlement_next` / `days_to_settlement` / `validity_note`.
  Podstawa: energia ważna 12 m-cy od końca miesiąca wprowadzenia, FIFO
  (`energa.pl/dom/strefa-prosumenta/net-metering`, `enerad.pl`).
* Nowe: `sensor.energa_XXX_prognoza_rachunku` (MTD + liniowa prognoza końca
  miesiąca), `deposit_valid_until` (+12 m-cy od przypisania M+1), `refund_cap_note`
  (20% RCEm / 30% RCE, Dz.U. 1847). Podstawa: `energa.pl net-billing`, `gov.pl` 27.12.2024.
* RCE auto bierze **oficjalne RCEm z tabeli PSE** (średnia ważona, jak na fakturze),
  nie zwykłą średnią RCE. Reguła: przed 11. dniem miesiąca obowiązuje M-2, po 11. — M-1.
  Tabela: `pse.pl/oire/rcem-rynkowa-miesieczna-cena-energii-elektrycznej`.

## Prognoza rachunku brutto (v0.2.14)

`sensor.energa_<nr-licznika>_bill_forecast` (`Prognoza Rachunku`) liczy jak
faktura: sprzedaż D/N + akcyza + handlowa + dystrybucja + VAT 23% −
rozliczenie prosumenta (depozyt / pokrycie magazynem). Stan = prognozowana
dopłata na koniec miesiąca, atrybuty = pełny rozkład MTD i prognozy.
Stawki w `Options → Ceny` (`tariff_*`, domyślne G12W z faktur 2026).

## Weryfikacja fakturowa 2026-09-04 (prod read-only vs lab)

* Faktura G12W-stare 01.05–30.06: magazyn przed `0/0`, po `752+606=1358`;
  przybliżenie deltami `(1067.7+1066.3)×0.8−(109.4+253.6)=1344` vs faktura `1358`
  (~1% — różnica to bilansowanie godzinowe sprzedawcy). `1358+1114.18=2472.18` prod==lab.
* Faktura G12W-nowe 07.2026: `456×0.26288×1.23=147.44`, depozyt po `0.00`.
  Faktura liczy z sumy sald godzinowych (456 kWh), sensor z delty licznika (523 kWh) —
  znane ~13% przybliżenie (`hourly_netting_note`). Bank PLN to pozycja netto
  (depozyt − koszt importu), nie sam depozyt.
* RCEm 07 `0.26288` z faktury = RCEm z tabeli PSE (publ. 11.08.2026). Prod `0.26288`
  poprawne na dziś (RCEm sierpnia dopiero 11.09).
* Znalezione niespójności prod (do poprawy ręcznie, NIE ruszane):
   sensor magazynu (stare): atrybut `formula` mówi `783`, a liczy `1358`;
   sensor magazynu (nowe): atrybut `formula` mówi `+147.44`, a liczy bez offsetu (poprawnie);
   Licznik G12W-nowe na prod ma `coefficient 0.8` (stara formuła bilansu) zamiast `0.0`;
  prod jedzie na upstream `v4.15.2` (brak natywnego banku) — do migracji na fork.
