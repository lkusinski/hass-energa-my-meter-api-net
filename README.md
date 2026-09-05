<div align="center">
  <img src="logo.png" alt="Energa My Meter API Logo" width="300"/>
</div>

<h1 align="center">Energa My Meter API Integration for Home Assistant</h1>

![GitHub Release](https://img.shields.io/github/v/release/lkusinski/hass-energa-my-meter-api-net)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
![API](https://img.shields.io/badge/data_source-Native_REST_API-blue)
![Architecture](https://img.shields.io/badge/storage-SQLite_WAL_Canonical-green)
![Tests](https://img.shields.io/badge/tests-256_passed-brightgreen)

> [!NOTE]
> Zaawansowana integracja dla klientów **Energa Operator** w Home Assistant, łącząca się bezpośrednio z **natywnym REST API** portalu *Mój Licznik* (bez scrapingu www). Posiada natywne wsparcie dla taryf **G11, G12, G12w, G12r**, pełną obsługę obu systemów prosumenckich (**Stary: Net-metering 0.8/0.7 z wirtualnym magazynem FIFO 12 miesięcy** oraz **Nowy: Net-billing z depozytem PLN i automatycznym cennikiem RCEm z PSE**), autonomiczną prognozę faktury brutto, 1-klikowy generator dedykowanych pulpitów rozliczeń oraz bezbłędną integrację ze statystykami **Panelu Energia (Energy Dashboard)**.

---

## ✨ Główne Możliwości (Architektura V1.0)

* 📡 **Natywne API REST:** Bezpośrednia, stabilna komunikacja JSON z platformą Energa Mój Licznik.
* 🚀 **1-Click Generator Pulpitów Lovelace:** Natywny przycisk urządzenia *„Utwórz Pulpit Rozliczeń”* (`button`) oraz serwis `energa_mobile.generate_dashboard` budujący kompletny pulpit `/energa-rachunek` zintegrowany z bocznym paskiem HA (bez restartu).
* 📊 **Pełna Integracja z Panelem Energia:** Dedykowane sensory statystyk godzinowych (`Panel Energia`) bez fałszywych skoków i resetów.
* 🔋 **Wirtualny Magazyn Energii (Stary System — Net-Metering):**
  * Rachunkowość FIFO z 12-miesięcznym okresem ważności energii (zgodnie z art. 4 ust. 11 ustawy o OZE).
  * Natywne przepływy wirtualnej baterii (`Bank Ładowanie` i `Bank Rozładowanie`) do sekcji Magazyn Energii w Panelu Energia.
  * Sensor poziomu napełnienia magazynu (`Poziom Magazynu %`).
* 💰 **Depozyt Prosumencki (Nowy System — Net-Billing):**
  * Miesięczne rozliczenie wartościowe w PLN.
  * Automatyczne pobieranie oficjalnych cen rynkowych **RCEm** publikowanych przez **PSE** (~11. dnia każdego miesiąca).
  * Wyliczanie salda depozytu z uwzględnieniem noweli ustawy o OZE (mnożnik 1.23).
* 📑 **Autonomiczna Prognoza Rachunku (`Prognoza Rachunku`):**
  * Dokładna kalkulacja bieżącej faktury brutto (energia czynna, opłata handlowa, akcyza, stawki dystrybucyjne zmienne i stałe, opłata jakościowa, mocowa, OZE, kogeneracyjna + VAT 23%).
  * Osobne, zweryfikowane z fakturami tabele opłat dla taryf **G11** oraz **G12w**.
* 🔄 **Automatyczny Backfill Historii (2 lata):**
  * Bezpośrednio po pierwszym logowaniu integracja asynchronicznie pobiera do 730 dni historii godzinowej w tle (bez blokowania interfejsu).
* 🛡️ **Kanonityczny Magazyn Danych (SQLite WAL):**
  * Niezmienna, odporna na wymiany liczników baza danych powiązana z logicznym punktem poboru (PPE).

---

## 📦 Instalacja

### Metoda 1: HACS (Niestandardowe repozytorium)
1. W Home Assistant przejdź do **HACS** → **Integracje** → menu w prawym górnym rogu (3 kropki) → **Repozytoria niestandardowe**.
2. Wklej adres URL: `https://github.com/lkusinski/hass-energa-my-meter-api-net`
3. Kategoria: **Integracja**.
4. Kliknij **Dodaj**, znajdź integrację i wybierz **Pobierz**.
5. Zrestartuj Home Assistant.

### Metoda 2: Instalacja ręczna
1. Pobierz archiwum z [GitHub Releases](https://github.com/lkusinski/hass-energa-my-meter-api-net/releases).
2. Skopiuj katalog `custom_components/energa_mobile` do folderu `/config/custom_components/` na Twoim Home Assistant.
3. Zrestartuj Home Assistant.

---

## ⚙️ Pierwsza Konfiguracja

1. Przejdź do **Ustawienia** → **Urządzenia oraz usługi** → **Dodaj integrację**.
2. Wyszukaj **Energa My Meter**.
3. Podaj dane logowania do portalu *Mój Licznik* (login/email i hasło).
4. Jeśli Twoje konto posiada licznik dwukierunkowy (instalację PV), kreator zapyta o **System rozliczeń**:
   * **Nowe zasady:** Net-billing (rozliczenie miesięczne w PLN, depozyt, RCEm) — instalacje od 01.04.2022.
   * **Stare zasady:** Net-metering (magazyn kWh 0.8 lub 0.7, roczny okres rozliczeniowy) — instalacje zgłoszone do 31.03.2022.
5. Kliknij **Zatwierdź**. Integracja utworzy urządzenia i encje, a w tle rozpocznie pobieranie historii pomiarów z ostatnich 2 lat.

---

## 📊 Konfiguracja Panelu Energia (Energy Dashboard)

Przejdź do **Ustawienia** → **Pulpity** → **Energia**. Skonfiguruj panel zgodnie z Twoim profilem:

### 🌞 Wariant 1: Stary System (Net-metering, opust 0.8/0.7)

W starym systemie nadwyżka energii nie jest sprzedawana za pieniądze, lecz magazynowana w sieci (ze współczynnikiem 0.8 lub 0.7):

1. **Sieć elektryczna — Zużycie (Pobór):**
   * Kliknij **Dodaj zużycie**:
     * Dla taryfy dwustrefowej G12w dodaj osobno:
       * Encja energii: `sensor.energa_<numer>_panel_energia_strefa_1`
         * Koszt: wybierz **Użyj encji śledzącej całkowity koszt** → `sensor.energa_<numer>_panel_energia_strefa_1_cost`
       * Encja energii: `sensor.energa_<numer>_panel_energia_strefa_2`
         * Koszt: wybierz **Użyj encji śledzącej całkowity koszt** → `sensor.energa_<numer>_panel_energia_strefa_2_cost`
     * Dla taryfy jednostrefowej G11:
       * Encja energii: `sensor.energa_<numer>_panel_energia_zuzycie`
       * Koszt: **Użyj encji śledzącej całkowity koszt** → `sensor.energa_<numer>_panel_energia_zuzycie_cost`
2. **Sieć elektryczna — Oddawanie do sieci (Zwrot):**
   * Kliknij **Dodaj oddawanie energii**:
     * Wybierz `sensor.energa_<numer>_panel_energia_produkcja_strefa_1` (oraz strefę 2)
     * Rekompensata: **Nie śledź kosztów** (energia trafia do magazynu kWh, a nie do wypłaty).
3. **Magazyny energii (Baterie wirtualne):**
   * Kliknij **Dodaj system baterii**:
     * Energia wpływająca do baterii: `sensor.energa_<numer>_bank_ladowanie`
     * Energia wypływająca z baterii: `sensor.energa_<numer>_bank_rozladowanie`
4. **Panele słoneczne (Fotowoltaika):**
   * ⚠️ **Ważne:** Do sekcji paneli słonecznych dodawaj **wyłącznie encje z Twojego falownika** (np. SolarEdge, Huawei, Fronius, Deye, GoodWe). **Nigdy nie dodawaj eksportu z licznika Energi jako produkcji PV!** Licznik widzi jedynie nadwyżkę po autokonsumpcji, a nie całkowitą produkcję.

---

### 💰 Wariant 2: Nowy System (Net-billing, RCEm w PLN)

W nowym systemie energia oddana do sieci jest wyceniana według rynkowej ceny energii elektrycznej (RCEm × 1.23) i zasila depozyt w PLN:

1. **Sieć elektryczna — Zużycie (Pobór):**
   * Kliknij **Dodaj zużycie**:
     * Dodaj strefy `panel_energia_strefa_1` oraz `strefa_2` (lub `panel_energia_zuzycie` dla G11) z wyborem **Użyj encji śledzącej całkowity koszt** (`..._cost`).
2. **Sieć elektryczna — Oddawanie do sieci (Zwrot):**
   * Kliknij **Dodaj oddawanie energii**:
     * Dodaj `sensor.energa_<numer>_panel_energia_produkcja_strefa_1` (oraz strefę 2).
3. **Magazyn energii:**
   * W net-billingu magazyn jest pieniężny (w PLN), więc sekcja baterii w Panelu Energia pozostaje pusta. Stan depozytu oraz prognozę rachunku prezentuje dedykowana karta Lovelace.

---

### 🏠 Wariant 3: Konsument bez fotowoltaiki (G11 / G12w)

1. **Sieć elektryczna — Zużycie (Pobór):**
   * Taryfa G11: Dodaj `sensor.energa_<numer>_panel_energia_zuzycie` z kosztem `sensor.energa_<numer>_panel_energia_zuzycie_cost`.
   * Taryfa G12w: Dodaj strefę 1 i strefę 2 wraz z ich encjami `_cost`.

---

## 🎛️ Pulpity Rozliczeń i Karty Lovelace
 
### Opcja 1: Automatyczny Generator (Zalecana — 1 kliknięcie)
Każdy licznik posiada natywną encję przycisku:
`button.energa_<numer_licznika>_utworz_pulpit_rozliczen` (**„Utwórz Pulpit Rozliczeń”**).
Kliknięcie przycisku na kafelku urządzenia automatycznie utworzy dedykowany pulpit `/energa-rachunek` zintegrowany z bocznym paskiem nawigacji Home Assistant. Pulpit dobiera karty i odznaki indywidualnie dla każdego licznika (magazyn kWh FIFO dla Net-meteringu, depozyt PLN i ceny RCEm dla Net-billingu, czysty widok zużycia dla zwykłego konsumenta).

Możesz też wywołać tę akcję za pomocą usługi `energa_mobile.generate_dashboard`.

### Opcja 2: Samodzielna Karta YAML (Ręczna konfiguracja)
Jeśli wolisz zbudować widok samodzielnie, wklej poniższy kod do wybranego pulpitu (**Pulpity** → **Edytuj** → **+ Dodaj kartę** → **Ręcznie / YAML**):

```yaml
type: vertical-stack
cards:
  # KARTA DLA STAREGO SYSTEMU (Net-metering 0.8)
  - type: entities
    title: 🔋 Magazyn Energii (Net-Metering 0.8)
    entities:
      - entity: sensor.energa_<numer_licznika>_bank_wirtualny_kwh
        name: Dostępna energia w magazynie
        icon: mdi:battery-charging-80
      - entity: sensor.energa_<numer_licznika>_magazyn_poziom
        name: Poziom zapełnienia magazynu
      - entity: sensor.energa_<numer_licznika>_dotychczasowy_rachunek
        name: Dotychczas do zapłaty (MTD)
        icon: mdi:cash-clock
      - entity: sensor.energa_<numer_licznika>_prognoza_rachunku
        name: Prognoza rachunku brutto (koniec miesiąca)
        icon: mdi:invoice-text-outline
      - entity: sensor.energa_<numer_licznika>_zuzycie_dzis
        name: Pobór dzisiaj
      - entity: sensor.energa_<numer_licznika>_produkcja_dzis
        name: Oddanie dzisiaj

  # KARTA DLA NOWEGO SYSTEMU (Net-Billing PLN)
  - type: entities
    title: 💰 Rozliczenie Net-Billing (PLN)
    entities:
      - entity: sensor.energa_<numer_licznika>_bank_wirtualny_pln
        name: Saldo depozytu netto
        icon: mdi:cash-multiple
      - entity: sensor.energa_<numer_licznika>_rcem_auto
        name: Bieżąca cena RCEm (PSE)
        icon: mdi:chart-line
      - entity: sensor.energa_<numer_licznika>_dotychczasowy_rachunek
        name: Dotychczas do zapłaty (MTD)
        icon: mdi:cash-clock
      - entity: sensor.energa_<numer_licznika>_prognoza_rachunku
        name: Prognoza rachunku brutto
        icon: mdi:invoice-text-outline
```

---

## 📋 Zestawienie Encji

| Nazwa encji | Klasa / Jednostka | Opis |
|---|---|---|
| `sensor.energa_<numer>_panel_energia_*` | `energy` / `kWh` | Statystyki godzinowe dla Panelu Energia (posiada stan liczbowy ostatniej sumy, pełna zgodność z walidacją `energy/validate`). |
| `sensor.energa_<numer>_panel_energia_*_cost` | `monetary` / `PLN` | Skumulowany koszt zużycia energii w danej strefie dla Panelu Energia. |
| `sensor.energa_<numer>_bank_wirtualny_kwh` | `energy` / `kWh` | Dostępny zapas energii w magazynie wirtualnym (FIFO 12 miesięcy, tylko Net-metering). |
| `sensor.energa_<numer>_bank_wirtualny_pln` | `monetary` / `PLN` | Stan depozytu prosumenckiego w nowym systemie (wartość nieujemna). |
| `sensor.energa_<numer>_magazyn_poziom` | `battery` / `%` | Procentowy poziom napełnienia magazynu energii (tylko Net-metering). |
| `sensor.energa_<numer>_dotychczasowy_rachunek` | `PLN` | Kwota rachunku MTD od początku miesiąca do chwili obecnej. |
| `sensor.energa_<numer>_prognoza_rachunku` | `PLN` | Autonomiczna prognoza rachunku brutto na koniec bieżącego miesiąca z wygładzaniem wczesnomiesięcznym. |
| `sensor.energa_<numer>_rcem_auto` | `PLN/kWh` | Oficjalna rynkowa cena RCEm z tabeli PSE. |
| `sensor.energa_<numer>_bank_ladowanie` | `energy` / `kWh` | Skumulowana energia wprowadzona do magazynu (dla sekcji Bateria w Net-metering). |
| `sensor.energa_<numer>_bank_rozladowanie` | `energy` / `kWh` | Skumulowana energia pobrana z magazynu (dla sekcji Bateria w Net-metering). |
| `sensor.energa_<numer>_zuzycie_dzis` | `energy` / `kWh` | Dzisiejsze zużycie energii. |
| `sensor.energa_<numer>_produkcja_dzis` | `energy` / `kWh` | Dzisiejsza produkcja oddana do sieci. |
| `sensor.energa_<numer>_stan_licznika_*` | `energy` / `kWh` | Oficjalne stany liczydła (import / eksport / strefy L1 i L2). |

---

## ❓ Najczęstsze Pytania (FAQ)

### Dlaczego w Net-billingu nie ma encji wirtualnego magazynu ani baterii?
W systemie Net-billing (instalacje od 1 kwietnia 2022 r.) prosument nie rozlicza się bezgotówkowo w kilowatogodzinach (kWh) w stosunku 0.8/0.7, lecz wartościowo w PLN. Wartość energii oddanej do sieci zasila depozyt prosumencki (`sensor.bank_wirtualny_pln_*`). Tworzenie sztucznej „baterii wirtualnej” w Panelu Energia fałszowałoby bilans energetyczny domu. Bateria wirtualna jest aktywna wyłącznie dla starego systemu (Net-metering).

### Jak działa wygładzanie wczesnomiesięczne prognozy rachunku?
W pierwszych dniach miesiąca kilka godzin poboru mogłoby prowadzić do nierealistycznie zawyżonej prognozy na koniec miesiąca. Algorytm `smoothed_blend_7d` przez pierwsze 7 dni miesiąca płynnie łączy bieżące zużycie MTD ze średnią dobową kroczącą z ostatnich 30 dni (lub 365 dni), zapewniając stabilne i wiarygodne szacunki od 1. dnia miesiąca.

### Kiedy aktualizowana jest cena RCEm?
PSE publikuje oficjalną stawkę RCEm około 11. dnia każdego miesiąca za miesiąc poprzedni. Integracja pobiera ją automatycznie raz na dobę bezpośrednio z oficjalnej tabeli PSE i aktualizuje kalkulacje.

---

## 📄 Licencja

Projekt dystrybuowany na warunkach licencji MIT. Szczegóły w pliku [LICENSE.md](LICENSE.md).
