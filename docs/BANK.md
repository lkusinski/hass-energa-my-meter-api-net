# Bank / Wirtualny Magazyn Energii — Kompendium i Architektura (v1.6.9)

**Cel:** Precyzyjne, zgodne z prawem OZE i rzeczywistymi fakturami OSD odwzorowanie wirtualnego magazynu energii (kWh) oraz depozytu prosumenckiego (PLN) w Home Assistant.

## Dwa systemy w jednej integracji (auto-wykrywanie po `prosumer_coefficient`)

| Instalacja | Taryfa | System | Sensor | Jednostka | Formuła i Prezentacja w Panelu Energia |
|---|---|---|---|---|---|
| G11 / G12 / G12w | **stare** net-metering (opust 0.8/0.7) | `sensor.energa_{serial}_bank_wirtualny_kwh` | kWh | `max(0, (export-baseline)×coeff - (import-baseline)) + initial_kwh`. W Panelu Energia: **wirtualna bateria** (ładowanie/rozładowanie) + prowizja OSD 20/30% jako oddanie za 0 zł. |
| G11 / G12 / G12w | **nowe** net-billing miesięczny (od 01.04.2022) | `sensor.energa_{serial}_bank_wirtualny_pln` + `sensor.energa_{serial}_rcem_auto` | PLN | `initial_pln + export×RCEm×1.23 - import×cena_strefa`. W Panelu Energia: **bezpośrednia sieć** z wyceną oddania wg rynkowej ceny odkupu $RCEm \times 1.23$. |

> `{serial}` — numer seryjny Twojego licznika (znormalizowany do małych liter). `G11 Odbiorca`
> (taryfa jednostrefowa, sam pobór bez PV) nie dostaje banku — tylko prosumenci.
> Od `v1.6.5` prosument = flaga `is_export_prosumer` (niezerowy rejestr eksportu lub flaga OSD).

* `initial_kwh` = **1358** (`752+606` `Razem w magazynie` z faktury `06.2026` — stan po rozliczeniu). Ustaw `balance_baseline_import/export` na wskazania `do` z tej faktury + `bank_initial_kwh=1358`. `Bilans>0` nadbudowuje bank.
* `initial_pln` = `0.00` na `01.08.2026` (faktura `07.2026`: `456×0.26288×1.23=147.44`, `Depozyt po 0.00`). RCEm `0.26288` lipiec, `×1.23` od noweli 27.11.2024 Dz.U. 1847.
* Per-strefa G12/G12w: `import_1/export_1` (L1 droga/dzień) + `import_2/export_2` (L2 tania/noc). Ceny taryfowe konfigurowane w `Opcje → Ceny`.
* `G11 bez PV` (faktura konsumencka): własna tabela opłat (handlowa, sieciowa stała, zmienna) — prognoza liczona jak faktura co do grosza.
* **Wymiana licznika:** historia mchart obejmuje też poprzedni licznik. Dzięki powiązaniu z logicznym punktem poboru (PPE) w bazie Canonical Storage (`energa_canonical.db`), wymiana fizycznego licznika nie powoduje utraty historii gospodarstwa.
* **Reimporty są bezpieczne (v1.6.2+):** serie przepływów kontynuują zaimportowane sumy (kotwica sprzed zakresu), a twarde klamrowanie monotoniczności w `RecorderAdapter` uniemożliwia powstawanie ujemnych pików energii.

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
    title: 💰 Rozliczenie Net-Billing — G12W (RCEm × 1.23)
    entities:
      - entity: sensor.bank_wirtualny_pln_<nr-licznika>
        name: Depozyt prosumencki (aktywo PLN)
        icon: mdi:cash-multiple
      - entity: sensor.energa_<nr-licznika>_rcem_auto_<nr-licznika>
        name: RCEm PLN/kWh (PSE auto)
        icon: mdi:chart-line
      - entity: sensor.energa_<nr-licznika>_cena_oddania
        name: Cena odkupu (RCEm × 1.23)
        icon: mdi:currency-usd
      - entity: sensor.prognoza_rachunku_<nr-licznika>
        name: Prognoza rachunku brutto (do zapłaty na koniec m-ca)
        icon: mdi:invoice-text-outline
  - type: markdown
    content: >
      RCE auto: `Options → rce_auto_fetch` (pobierana automatycznie z PSE ~11. dnia miesiąca).
      Depozyt pokrywa wyłącznie energię czynną (zgodnie z art. 4 ust. 11 ustawy o OZE).
      Dystrybucja i opłaty stałe pozostają do uregulowania na fakturze.
```

## Konfiguracja Panelu Energia (`/energy`)

### ⚡ Metoda Automatyczna (1-Click Setup — Rekomendowana)
Wystarczy na karcie urządzenia licznika kliknąć:
`button.energa_{serial}_skonfiguruj_panel_energia`
Integracja bezpośrednio programuje konfigurację Home Assistanta (`.storage/energy`):
- **Stare zasady (Net-metering):** Rejestruje syntetyczne baterie (L1/L2 lub pojedynczą), syntetyczną sieć pobór z cenami oraz syntetyczną sieć oddanie ze stawką 0 zł (prowizja OSD).
- **Nowe zasady (Net-billing):** Rejestruje sieć pobór z cenami oraz sieć oddanie z dynamiczną wyceną wg encji `sensor.energa_{serial}_cena_oddania` ($RCEm \times 1.23$).
- **Fotowoltaika:** Automatycznie dołącza skonfigurowany w Opcjach falownik PV.

### 🔬 Matematyczny Silnik Bilansowania (`synthetic_storage.py`)
W polskim net-meteringu (Ustawa o OZE art. 4 ust. 1 i 11) eksport do sieci nie jest sprzedażą, lecz magazynowaniem energii pomniejszonym o 20% (dla $\le 10$ kWp) lub 30% (dla $> 10$ kWp) prowizji OSD:
1. **Syntetyczny Magazyn Ładowanie:** $\text{Export} \times 0.8$ (lub $0.7$) trafia do sekcji Magazyn Energii (Bateria).
2. **Syntetyczna Sieć Oddanie (Prowizja):** $\text{Export} \times 0.2$ (lub $0.3$) trafia do sekcji Oddawanie do sieci za 0.00 zł/kWh.
3. **Syntetyczny Magazyn Rozładowanie:** Pokrycie poboru z wirtualnego banku za 0 zł/kWh.
4. **Syntetyczna Sieć Pobór:** Wyłącznie pobór netto ponad stan magazynu, fakturowany pełną stawką brutto.

Dzięki temu wskaźnik samowystarczalności budynku oraz koszty w Panelu Energia są w 100% zgodne z fizyką i fakturą OSD.

## Opcje integracji

`Ustawienia → Urządzenia oraz usługi → Energa My Meter → Opcje`:
* **Współczynnik prosumencki (`prosumer_coefficient`):** `0.8` (stare $\le 10$ kWp), `0.7` (stare $> 10$ kWp) lub `0.0` (nowe net-billing / konsument).
* **Ceny taryfowe:** Stawki brutto za kWh dla strefy dziennej i nocnej (lub całodobowej).
* **Automatyczne pobieranie RCEm (`rce_auto_fetch`):** Włączone domyślnie — integracja pobiera oficjalne średnie ważone RCEm z PSE OIRE z 2-godzinnym buforem w pamięci podręcznej.
* **Encja falownika (`inverter_energy_entity`):** Encja produkcji PV do precyzyjnego godzinowego wyliczania autokonsumpcji i realnego zużycia domu.

## Weryfikacja z fakturami

* Faktura G12W-stare `01.05–30.06.2026` (Wiśniowa): `Razem w magazynie L1/L2` = `752+606=1358`. Baseline = wskazania `od` faktury lub `do`; bank start `1358 + (licznik-baseline)×0.8`.
* Faktura G12W-stare `01.07–31.08.2026` (Wiśniowa — Faktura VAT 4104603000/FES/00042):
  * Magazyn przed: L1 = **752 kWh**, L2 = **606 kWh** (Razem = **1358 kWh**).
  * Przepływy w okresie (01.07–31.08):
    * **L1 (dzień):** pobór 83 kWh (w 100% z magazynu L1, pozostało 669 kWh), oddanie 1061 kWh $\times 0.8 =$ **+849 kWh** nowego wkładu.
    * **L2 (noc/weekend):** pobór 342 kWh (w 100% z magazynu L2, pozostało 264 kWh), oddanie 843 kWh $\times 0.8 =$ **+674 kWh** nowego wkładu.
  * Magazyn po rozliczeniu: L1 = **1518 kWh**, L2 = **938 kWh**, **Łącznie = 2456 kWh**.
  * Koszt: **158,72 zł brutto** (129,04 zł netto + 29,68 zł VAT). Energia czynna i zmienna sieciowa: 0,00 zł. Koszty wynikają w 100% z opłat stałych (150,31 zł brutto) oraz ustawowych opłat zmiennych nieumarzalnych od fizycznego poboru (8,41 zł brutto: akcyza 3,03 zł, OZE 3,81 zł, kogeneracja 1,57 zł).
* Faktura G12W-nowe `07.2026`: `456×0.26288×1.23=147.44` → `Depozyt po 0.00` → bank PLN start `0.00`, potem `export×RCE×1.23 - import×cena` per strefa. Sprawdź w `Deweloperskie → Stany → Bank PLN atrybuty`.

## Dualny Magazyn Energii (L1 / L2) w Taryfach Wielostrefowych

W taryfach strefowych (G12, G12w, G12r) operator rozlicza wirtualny magazyn prosumencki **niezależnie dla każdej strefy czasowej**:
- **Podmagazyn L1 (dzienny / szczytowy):** zasilany nadwyżką z produkcji fotowoltaicznej w dzień, pokrywa bieżące pobory w strefie dziennej.
- **Podmagazyn L2 (nocny / weekendowy):** zasilany nadwyżkami weekendowymi i pozaszczytowymi, pokrywa pobory w strefie taniej.

Oba podmagazyny posiadają własną kolejkę FIFO z ważnością wkładów przez 12 miesięcy. Integracja sumuje oba portfele do głównej encji `sensor.energa_<nr-licznika>_bank_wirtualny_kwh` (np. 2456 kWh), a docelowo udostępnia szczegółowe atrybuty strefowe `bank_kwh_l1` (1518 kWh) oraz `bank_kwh_l2` (938 kWh).

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

## Prognoza rachunku brutto i Profil Godzinowy WAL (v1.6.9)

`sensor.energa_{serial}_prognoza_rachunku` (`Prognoza Rachunku Brutto`) liczy pełen rachunek odzwierciedlający strukturę faktury OSD:
- Sprzedaż energii (czynna D/N) + akcyza + opłata handlowa.
- Dystrybucja: stała sieciowa, abonamentowa, zmienna sieciowa D/N, jakościowa, OZE, kogeneracyjna.
- Opłata mocowa: automatyczny dobór progu rocznego poboru URE 2026 (`capacity_for_annual_use`) z możliwością ręcznego nadpisania w Opcjach.
- Rozliczenie prosumenta: depozyt wartościowy (net-billing) lub pokrycie z magazynu (net-metering).
- VAT 23% naliczany ściśle wg reguł fakturowych.
- **Predykcja profilem godzinowym WAL (`HourlyProfileForecaster`):** Po zebraniu co najmniej 7 dni historii w bazie Canonical Storage (`energa_canonical.db`), integracja dekomponuje profil dobowy z uwzględnieniem polskich dni roboczych, weekendów i świąt ustawowych (w tym ruchomych świąt wielkanocnych i Bożego Ciała), wyliczając dynamiczny trend zużycia.
- **Bezblokadowy MainThread (v1.6.9):** Obliczenia profilu są wykonywane asynchronicznie w tle przez pulę wątków roboczych (`async_add_executor_job`), a wynik serwowany natychmiastowo z bufora RAM (< 0.0001s).

## Weryfikacja fakturowa (kotwice liczbowe)

* Faktura G12W-stare 01.05–30.06: magazyn przed `0/0`, po `752+606=1358`;
  przybliżenie deltami `(1067.7+1066.3)×0.8−(109.4+253.6)=1344` vs faktura `1358`
  (~1% — różnica to bilansowanie godzinowe sprzedawcy).
* Faktura G12W-stare 01.07–31.08: magazyn przed `752+606=1358`, po `1518+938=2456`;
  bilans poboru (425 kWh) w 100% z magazynu; nowy wsad 1523 kWh; faktura 158,72 zł brutto.
* Faktura G12W-nowe 07: `456×0.26288×1.23=147.44`, depozyt po `0.00`.
  Faktura liczy z sumy sald godzinowych (456 kWh), sensor z delty licznika (523 kWh) —
  znane ~13% przybliżenie (`hourly_netting_note`). Bank PLN to pozycja netto
  (depozyt − koszt importu), nie sam depozyt.
* RCEm z faktury = RCEm z tabeli PSE (publikacja ~11. dnia miesiąca);
  przed 11. dniem obowiązuje M-2, po 11. — M-1.
