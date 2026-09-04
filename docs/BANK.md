# Bank / Wirtualny Magazyn Energii — jak czytać

**Cel:** w łatwy sposób widzieć ile prądu / kasy masz w magazynie.

## Dwa systemy w jednej integracji (auto-wykrywanie po `prosumer_coefficient`)

| Instalacja | Taryfa | System | Sensor | Jednostka | Formuła |
|---|---|---|---|---|---|
| G12W | **stare** net-metering roczny | `sensor.bank_wirtualny_kwh` (`energa_<nr-licznika>_bank_kwh`) | kWh | `max(0, (export-baseline)×0.8 - (import-baseline)) + initial_kwh` |
| G12W | **nowe** net-billing miesięczny | `sensor.bank_wirtualny_pln` (`energa_<nr-licznika>_bank_pln`) + `sensor.energa_<nr-licznika>_rcem_auto` | PLN | `initial_pln + export×RCE×1.23 - import×cena_strefa` |

> `<nr-licznika>` — podstaw numer swojego licznika. `G11 Odbiorca`
> (taryfa jednostrefowa, sam pobór) nie dostaje banku — tylko prosumenci.
> Od `v0.2.15` prosument = flaga `Wytwórca` lub niezerowy licznik eksportu
> (same kody OBIS eksportu przy zerach nie wystarczą); sieroty po starej
> bramce usuwają się same przy starcie.

* `initial_kwh` = **1358** (`752+606` `Razem w magazynie` z faktury `06.2026` — stan po rozliczeniu). Ustaw `balance_baseline_import/export` na wskazania `do` z tej faktury + `bank_initial_kwh=1358`. `Bilans>0` nadbudowuje bank.
* `initial_pln` = `0.00` na `01.08.2026` (faktura `07.2026`: `456×0.26288×1.23=147.44`, `Depozyt po 0.00`). RCEm `0.26288` lipiec, `×1.23` od noweli 27.11.2024 Dz.U.1847.
* Per-strefa G12W: `import_1/export_1` (L1 droga) + `import_2/export_2` (L2 tania). Ceny `import_price_1 1.30` / `import_price_2 0.65` w `Options → Ceny` (lub zostaw `1.2453/0.5955` default — ujednolicisz).
* `Bilans Prosumencki` to diagnostic (ukryty półprodukt: `Bank=max(0,Bilans)+initial`). Nie wieszaj go obok Banku — to ta sama energia liczona podwójnie.
* `G11 bez PV` (faktura konsumencka, 2159 kWh): własna tabela opłat (handlowa `16,18`, sieciowa stała `11,77`, zmienna `0,3485`) — prognoza liczona jak faktura (`2271,74` netto → `2794,24` brutto co do grosza). Akcyza jest już w cenie energii (tylko przypis na fakturze).
* **Wymiana licznika:** historia mchart obejmuje też poprzedni licznik (G12W nowe zasady: sumy 730d większe niż stan nowego). Bank liczony z nowego licznika (baseline) jest poprawny; FIFO i słupki pokazują historię gospodarstwa, nie licznika.
* **Reimporty są bezpieczne (v0.3.4):** serie przepływów kontynuują zaimportowane sumy (kotwica sprzed zakresu), pełne backfille startują od 0. Sensor ładuje MAX z 14 dni — restarty nie zwijają słupków baterii.

## Gdzie zobaczyć

**Encje:** `Deweloperskie → Stany` → `sensor.energa_*_bank_*` — wartość + `Atrybuty`: `net_import_kwh`, `net_export_kwh`, `bilans_kwh` / `rce_price`, `import_cost_pln`, `per_strefa_note`.

**Lovelace — wklej do `Pulpity → Edytuj → + Karta → Ręcznie`:**

```yaml
type: vertical-stack
cards:
  - type: entities
    title: 🔋 Magazyn Wirtualny — G12W stare zasady (0.8)
    entities:
      - entity: sensor.bank_wirtualny_kwh_<nr-licznika>
        name: Bank kWh (do odebrania)
        icon: mdi:battery-charging-80
      - entity: sensor.energa_<nr-licznika>_magazyn_poziom_<nr-licznika>
        name: Poziom magazynu %
      - entity: sensor.energa_<nr-licznika>_bank_ladowanie_<nr-licznika>
        name: Ładowanie (do Baterii w Panelu Energia)
      - entity: sensor.energa_<nr-licznika>_bank_rozladowanie_<nr-licznika>
        name: Rozładowanie (do Baterii w Panelu Energia)
  - type: gauge
    entity: sensor.bank_wirtualny_kwh_<nr-licznika>
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
      - entity: sensor.bank_wirtualny_pln_<nr-licznika>
        name: Depozyt PLN (ujemny = do zapłaty)
      - entity: sensor.energa_<nr-licznika>_rcem_auto_<nr-licznika>
        name: RCEm PLN/kWh (PSE auto)
      - entity: sensor.prognoza_rachunku_<nr-licznika>
        name: Prognoza dopłaty
  - type: markdown
    content: >
      RCE auto: `Options → rce_auto_fetch` lub ręcznie `bank_rce_price`.
      Aktualizuj co miesiąc (PSE publikuje ~11. dnia). Formuła w atrybutach encji.
      (Bilans Prosumencki jest diagnostyczny — to półprodukt do Banku.)
```

**Energy Dashboard — jak wpiąć (v0.3.0, bez ściemy):**
`Ustawienia → Pulpity → Energia`:
* **Stary net-metering (off-grid):** Sieć pobór = `Panel Energia Strefa 1/2`
  (z ceną), Sieć zwrot = `Panel Energia Produkcja Strefa 1/2` BEZ ceny
  (nadwyżka trafia do magazynu kWh, nie na sprzedaż — brak rekompensaty),
  Bateria = `Bank Ładowanie/Rozładowanie`.
  ☀️ Fotowoltaika = TYLKO prawdziwe encje falownika, NIGDY eksport
  z licznika: eksport to nadwyżka PO autokonsumpcji, więc produkcja
  jest wyższa niż zwrot (podpięcie eksportu jako solara zaniża produkcję
  i podwójnie liczy energię: raz jako zwrot, raz jako baterię —
  zwrotu do sieci NIE dodawaj obok baterii).
* **Nowy net-billing (sprzedaż):** Sieć pobór = `Panel Energia Strefa 1/2`
  (z ceną), Sieć zwrot = `Panel Energia Produkcja Strefa 1/2` z ceną =
  encja `Cena Oddania` (żywa sprzedaż `RCEm×1.23`, nie zamrożone 0,95).
  Baterii NIE dodawaj (przepływy to kopia import/eksport; depozyt shows
  `Bank PLN`). Stan depozytu i prognozę pokazuje Lovelace poniżej.
* Bank `sensor.*_bank_*` + `Magazyn Poziom %` (klasa `battery`) na gauge
  w Lovelace — Panel Energia słupków stanu nie umie, tylko przepływy.

## Opcje integracji

`Ustawienia → Urządzenia → Energa → Konfiguruj → Ustaw Ceny Energii`:
* `prosumer_coefficient` `0.8` stara / `0.0` nowa (ustaw ręcznie
  w Options — data aktywacji to data aplikacji, nie umowy),
* `balance_baseline_import/export` = stan licznika na fakturze początkowej (0 = lifetime),
* `bank_initial_kwh` / `bank_initial_pln` z faktur,
* `bank_rce_price` np. `0.26288` + `rce_auto_fetch` (24h cache w coordinatorze, fallback manual).

`Wykryj pierwszy odczyt` (`button.energa_*_wykryj_pierwszy_odczyt`) — hierarchicznie `today-730d` → `~14 req` `mchart` `0.7s`, nie `2020`.

## Weryfikacja z fakturami

* Faktura G12W-stare `01.05–30.06.2026`: `Razem w magazynie L1/L2` = `752+606=1358`. Baseline = wskazania `od` faktury lub `do`; bank start `1358 + (licznik-baseline)×0.8`.
* Faktura G12W-nowe `07.2026`: `456×0.26288×1.23=147.44` → `Depozyt po 0.00` → bank PLN start `0.00`, potem `export×RCE×1.23 - import×cena` per strefa. Sprawdź w `Deweloperskie → Stany → Bank PLN atrybuty`.

> Po `v0.2.10` możesz usunąć `packages/bank_energii.yaml` — bank jest natywny.

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

## Weryfikacja fakturowa (kotwice liczbowe)

* Faktura G12W-stare 01.05–30.06: magazyn przed `0/0`, po `752+606=1358`;
  przybliżenie deltami `(1067.7+1066.3)×0.8−(109.4+253.6)=1344` vs faktura `1358`
  (~1% — różnica to bilansowanie godzinowe sprzedawcy).
* Faktura G12W-nowe 07: `456×0.26288×1.23=147.44`, depozyt po `0.00`.
  Faktura liczy z sumy sald godzinowych (456 kWh), sensor z delty licznika (523 kWh) —
  znane ~13% przybliżenie (`hourly_netting_note`). Bank PLN to pozycja netto
  (depozyt − koszt importu), nie sam depozyt.
* RCEm z faktury = RCEm z tabeli PSE (publikacja ~11. dnia miesiąca);
  przed 11. dniem obowiązuje M-2, po 11. — M-1.
