<div align="center">
  <img src="logo.png" alt="Energa My Meter API Logo" width="300"/>
</div>

<h1 align="center">Energa My Meter API PRO (Mój Licznik) for Home Assistant</h1>

![GitHub Release](https://img.shields.io/github/v/release/lkusinski/hass-energa-my-meter-api-net)
[![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
![API](https://img.shields.io/badge/data_source-Native_REST_API-blue)
![Architecture](https://img.shields.io/badge/storage-SQLite_WAL_Canonical-green)
![Tests](https://img.shields.io/badge/tests-344_passed-brightgreen)

> [!NOTE]
> ### 💡 O projekcie: Samodzielna wersja PRO a podstawowa integracja ergo5
> 
> Ten projekt to niezależna, wysoce zaawansowana integracja (wersja **PRO**) dla użytkowników platformy **Energa Mój Licznik** w Home Assistant. 
> 
> Jeśli zależy Ci jedynie na szybkim i prostym odczycie bieżących wskazań licznika, znakomitą i sprawdzoną opcją jest integracja stworzona przez **ergo5** ([`hass-energa-my-meter-api`](https://github.com/ergo5/hass-energa-my-meter-api)). Jej wielkim plusem jest to, że znajduje się już **oficjalnie w domyślnym katalogu HACS**, dzięki czemu instaluje się ją bezpośrednio z wyszukiwarki jednym kliknięciem.
> 
> **Skąd wzięła się ta wersja i dlaczego jest samodzielnym bytem?**
> - **Ewolucja projektu:** Miało to być proste rozwiązanie, ale w miarę analizy rzeczywistych faktur OSD i kolejnych zawiłości rozliczeniowych (izolacja stref L1/L2 w magazynie energii, ścisłe kolejki FIFO z 12-miesięcznym horyzontem ważności, depozyty wartościowe Net-billing z oficjalnymi stawkami PSE RCEm, predykcje rachunku brutto uwzględniające komplet opłat stałych i zmiennych, asynchroniczna autokonsumpcja PV eliminująca lagi OSD czy 730-dniowy backfill historii) powstał kompletny kombajn analityczno-rozliczeniowy.
> - **Dlaczego to NIE jest „nakładka” na inną integrację?** Nie należy traktować tego rozwiązania jako układu *„podstawa + nakładka”*. Nakładka nie działa bez podstawy, a przy dwóch spiętych integracjach odpytujących to samo konto OSD **moim zdaniem po prostu więcej rzeczy może się rozjechać** (konflikty sesji, dublowanie requestów, niespójności stanów w bazie Recorder). Najlepiej po prostu wybrać od razu: albo idziesz w wersję podstawową (od ergo5), albo decydujesz się na to samodzielne, rozbudowane środowisko PRO.
> - **Status HACS:** Wersja PRO ze względu na swój zaawansowany i bezkompromisowy charakter **nigdy nie trafi do oficjalnego domyślnego sklepu HACS**. Dodaje się ją w HACS w 10 sekund jako **Repozytorium Niestandardowe (Custom Repository)**.

---

## ✨ Główne Możliwości Wersji PRO

* 📡 **Natywne API REST:** Bezpośrednia, stabilna komunikacja JSON z platformą Energa Mój Licznik (bez podatnego na awarie scrapingu HTML).
* 🔋 **Natywny Wirtualny Magazyn Energii (Net-Metering 0.8/0.7) w Panelu Energia (`/energy`):**
  * **Matematyczny silnik bilansowania godzinowego (`synthetic_storage.py`):** Zgodnie z polską ustawą o OZE (art. 4 ust. 1 i 11), nadwyżki wprowadzone do sieci dzielone są precyzyjnie na:
    1. *Ładowanie Wirtualnego Magazynu:* $Export \times 0.8$ (dla instalacji $\le 10$ kW) lub $\times 0.7$ (powyżej 10 kW).
    2. *Prowizja Rzeczowa OSD:* $Export \times 0.2$ (lub $0.3$) jako bezpłatne oddanie do sieci (0 zł/kWh).
    3. *Rozładowanie Magazynu:* kompensacja poboru z dostępnego salda banku za 0 zł/kWh opłaty zmiennej.
    4. *Pobór Netto z Sieci:* wyłącznie nadwyżka poboru ponad stan magazynu, taryfikowana pełną stawką brutto.
  * **Wyeliminowanie przekłamań Energy Dashboard:** Rozwiązuje problem natywnego Panelu Energia w HA, który przy net-meteringu drastycznie zaniżał wskaźniki samowystarczalności oraz zawyżał koszty energii.
  * **Obsługa taryf jednostrefowych (G11) i dwustrefowych (G12/G12w):** Precyzyjne bilansowanie między strefami L1 (Dzień) i L2 (Noc) zgodnie z przepisami OZE.
* 🔘 **1-Click Autokonfiguracja Panelu Energia (`button.py`):**
  * Przycisk na karcie urządzenia (`button.energa_{numer}_skonfiguruj_panel_energia`) bezpośrednio programuje `.storage/energy` za pośrednictwem natywnego API `EnergyManager.async_update` — bez restartu HA i bez żmudnego ręcznego mapowania encji!
* 📋 **Ankieta Konfiguracyjna Onboarding (Kreator i Opcje):**
  * Asystent pierwszej instalacji pyta o moc mikroinstalacji ($\le 10$ kW / $> 10$ kW) oraz preferowany model prezentacji (Wirtualny Magazyn vs Model tradycyjny), z pełną możliwością późniejszej edycji w Opcjach integracji.
* 🤖 **Automatyczny Wirtualny Magazyn Energii (Zero-Config FIFO z API Energa):**
  * **Zero ręcznej konfiguracji:** Integracja pobiera roczne agregacje miesięczne bezpośrednio z API Energi (`/resources/mchart`) i automatycznie wylicza stan banku oraz podział L1/L2 wg zasad FIFO bez konieczności wpisywania żadnych danych początkowych!
  * **Opcjonalna kalibracja datą faktury:** Jeśli chcesz skalibrować bank idealnie pod fakturę, podajesz wyłącznie datę z faktury (np. `2024-05-31`) oraz stany L1/L2 z tej faktury — nie musisz spisywać wielocyfrowych stanów licznika!
* 📦 **Podwójny Wirtualny Magazyn Energii (Net-Metering FIFO — stary system):**
  * **Potwierdzona na fakturach OSD izolacja strefowa (L1/L2):** Zgodnie z zasadami Energi w taryfach wielostrefowych (G12, G12w) magazyn dzienny (L1) kompensuje wyłącznie zużycie dzienne, a magazyn nocny (L2) wyłącznie zużycie pozaszczytowe (brak niedozwolonego transferu energii między strefami).
  * Ścisła rachunkowość FIFO z 12-miesięcznym okresem ważności energii (art. 4 ust. 11 ustawy o OZE).
  * Natywne encje wirtualnej baterii (`Bank Ładowanie` i `Bank Rozładowanie`) dla sekcji Magazyn Energii w oficjalnym Panelu Energia HA.
  * Sensor poziomu napełnienia magazynu (`Poziom Magazynu %`) działający już od 3 miesięcy zebranej historii.
* 💰 **Depozyt Prosumencki (Net-Billing — nowy system):**
  * Miesięczne rozliczenie wartościowe w PLN.
  * Automatyczne pobieranie oficjalnych cen rynkowych **RCEm** publikowanych przez **PSE** (~11. dnia każdego miesiąca).
  * Wyliczanie salda depozytu z uwzględnieniem noweli ustawy o OZE (mnożnik 1.23).
* ☀️ **Silnik Autokonsumpcji PV i Realnego Zużycia Domu (Hour-by-Hour Alignment):**
  * Eliminacja pozornej „100% autokonsumpcji” wynikającej z opóźnień OSD Mój Licznik (3–24h) względem falownika PV.
  * Precyzyjne dopasowanie godzinowe produkcji PV i wskazań licznika w zamkniętych interwałach czasowych.
  * Wyliczanie realnego zużycia energii przez budynek oraz oszczędności finansowych **BRUTTO (z 23% VAT)**.
* 📑 **Autonomiczna Prognoza Rachunku (`Prognoza Rachunku Brutto`):**
  * Kompletna kalkulacja bieżącej faktury brutto: energia czynna, opłata handlowa, akcyza, stawki dystrybucyjne zmienne i stałe, opłata jakościowa, mocowa, OZE, kogeneracyjna + VAT 23%.
  * Algorytm wygładzania wczesnomiesięcznego (`smoothed_blend_7d`) eliminujący anomalie w pierwszych dniach miesiąca.
* 🔄 **Odporny Auto-Backfill Historii (do 730 dni / 2 lata):**
  * Asynchroniczny import danych godzinowych bezpośrednio do długoterminowych statystyk Home Assistant w tle.
  * Pełna odporność na restarty HA podczas importu (weryfikacja okna historycznego zamiast pojedynczego punktu, trwały znacznik w konfiguracji).
  * Powiadomienia w HA co 30 przetworzonych dni z % postępu i estymacją czasu.
* 🚀 **Automatyczny Pulpit Lovelace (`/energa-rachunek`):**
  * Integracja automatycznie tworzy i rejestruje dedykowany pulpit w menu bocznym Home Assistant natychmiast po instalacji.
  * Możliwość ponownego wygenerowania jednym kliknięciem przyciskiem urządzenia lub usługą `energa_mobile.generate_dashboard`.
* 🛡️ **Kanonityczny Magazyn Danych (SQLite WAL):**
  * Baza danych powiązana z logicznym punktem poboru (PPE), chroniąca historię przed utratą przy fizycznej wymianie licznika przez monterów OSD.
* 🏠 **Pełne Wsparcie dla Konsumentów bez Fotowoltaiki:**
  * Opcja wyboru zwykłego odbiorcy (współczynnik `0.0`), precyzyjne rozliczenia taryf G11, G12, G12w bez narzucania mechanizmów prosumenckich.

---

## 📦 Instalacja

### Metoda 1: HACS (Repozytorium Niestandardowe — Zalecana)
1. W Home Assistant przejdź do **HACS** → **Integracje**.
2. W prawym górnym rogu kliknij menu (3 kropki) → **Repozytoria niestandardowe** (*Custom repositories*).
3. Wklej adres URL repozytorium:
   ```text
   https://github.com/lkusinski/hass-energa-my-meter-api-net
   ```
4. Kategoria: **Integracja** (*Integration*).
5. Kliknij **Dodaj**, a następnie znajdź na liście **Energa My Meter API PRO** i wybierz **Pobierz**.
6. Zrestartuj Home Assistant.

### Metoda 2: Instalacja ręczna
1. Pobierz archiwum z najnowszego wydania [GitHub Releases](https://github.com/lkusinski/hass-energa-my-meter-api-net/releases).
2. Skopiuj katalog `custom_components/energa_mobile` do folderu `/config/custom_components/` w swoim Home Assistant.
3. Zrestartuj Home Assistant.

---

## ⚙️ Pierwsza Konfiguracja

1. Przejdź do **Ustawienia** → **Urządzenia oraz usługi** → **Dodaj integrację**.
2. Wyszukaj **Energa My Meter**.
3. Podaj login i hasło do portalu *Mój Licznik*.
4. Wybierz Twój profil rozliczeniowy:
   * **Stary system (Net-metering):** Magazyn kWh z opustem 0.8 lub 0.7 (instalacje zgłoszone do 31.03.2022).
     * *Ankieta konfiguracyjna:* Wybierz moc instalacji ($\le 10$ kW — współczynnik 0.8 lub $> 10$ kW — współczynnik 0.7) oraz preferowany model prezentacji:
       * **Wirtualny Magazyn Energii (Rekomendowany):** Syntetyczny magazyn w Panelu Energia, prowizja OSD jako oddanie za 0 zł, pobór z magazynu za 0 zł, sieć wyłącznie za pobór netto.
       * **Model Tradycyjny:** Surowe wskazania licznika w sekcji Sieć.
   * **Nowy system (Net-billing):** Depozyt wartościowy w PLN, oficjalne rynkowe stawki RCEm z PSE (instalacje od 01.04.2022).
   * **Nie posiadam fotowoltaiki (zwykły odbiorca):** Standardowy pobór z sieci wg taryfy G11 / G12 / G12w.
5. Kliknij **Zatwierdź**. Integracja utworzy encje, przypnie gotowy pulpit `/energa-rachunek` do bocznego paska HA, a w tle rozpocznie import do 730 dni historii.

---

## 📊 Konfiguracja Panelu Energia (Energy Dashboard)

### ⚡ Metoda 1: Automatyczna (1-Click Setup — Rekomendowana)
Nie musisz ręcznie konfigurować ani wklejać skomplikowanych encji do Panelu Energia!
1. Przejdź do **Ustawienia** → **Urządzenia oraz usługi** → **Energa My Meter** → Kliknij w urządzenie Twojego licznika.
2. W sekcji **Elementy sterujące (Buttons)** kliknij **Skonfiguruj Panel Energia** (`button.energa_<numer>_skonfiguruj_panel_energia`).
3. Integracja automatycznie skonfiguruje sekcje Sieć, Wirtualny Magazyn Energii (baterie dla strefy L1/L2 lub pojedynczej) oraz Fotowoltaikę bezpośrednio w systemie Home Assistanta (`.storage/energy`)!

---

### 🛠️ Metoda 2: Konfiguracja Ręczna
Jeśli wolisz skonfigurować Panel Energia ręcznie, przejdź do **Ustawienia** → **Pulpity** → **Energia**:

#### 🌞 Wariant 1A: Wirtualny Magazyn Energii (Model Syntetyczny — Rekomendowany dla Net-metering)
1. **Sieć elektryczna — Zużycie (Pobór netto z sieci ponad stan magazynu):**
   * Taryfa G12 / G12w:
     * Strefa 1 (Dzień): `sensor.energa_<numer>_syntetyczna_siec_pobor_strefa_1` z kosztem `sensor.energa_<numer>_panel_energia_strefa_1_cost`
     * Strefa 2 (Noc): `sensor.energa_<numer>_syntetyczna_siec_pobor_strefa_2` z kosztem `sensor.energa_<numer>_panel_energia_strefa_2_cost`
   * Taryfa G11:
     * `sensor.energa_<numer>_syntetyczna_siec_pobor` z kosztem `sensor.energa_<numer>_panel_energia_zuzycie_cost`
2. **Sieć elektryczna — Oddawanie do sieci (Prowizja rzeczowa OSD):**
   * `sensor.energa_<numer>_syntetyczna_siec_oddanie` (rekompensata: stała stawka **0 PLN/kWh** — darmowa prowizja OSD 20% lub 30%).
3. **Magazyny energii (Wirtualne baterie):**
   * Taryfa G12 / G12w (dwie wirtualne baterie L1 i L2):
     * Magazyn Dzienny: wpływająca: `sensor.energa_<numer>_syntetyczny_magazyn_l1_ladowanie`, wypływająca: `sensor.energa_<numer>_syntetyczny_magazyn_l1_rozladowanie`.
     * Magazyn Nocny: wpływająca: `sensor.energa_<numer>_syntetyczny_magazyn_l2_ladowanie`, wypływająca: `sensor.energa_<numer>_syntetyczny_magazyn_l2_rozladowanie`.
   * Taryfa G11 (jeden wirtualny magazyn):
     * Wpływająca: `sensor.energa_<numer>_syntetyczny_magazyn_ladowanie`, wypływająca: `sensor.energa_<numer>_syntetyczny_magazyn_rozladowanie`.
4. **Panele słoneczne (Fotowoltaika):**
   * Dodaj encję całkowitej produkcji ze swojego falownika PV (np. SolarEdge, Huawei, Solis, Fronius).

#### 🏠 Wariant 1B: Stary System (Model Tradycyjny)
1. **Sieć elektryczna — Pobór:** `sensor.energa_<numer>_panel_energia_strefa_*` z kosztami `..._cost`.
2. **Sieć elektryczna — Zwrot:** `sensor.energa_<numer>_panel_energia_produkcja_strefa_*` (bez śledzenia kosztów).
3. **Magazyny energii:** wpływająca: `sensor.energa_<numer>_bank_ladowanie`, wypływająca: `sensor.energa_<numer>_bank_rozladowanie`.

#### 💰 Wariant 2: Nowy System (Net-billing, RCEm w PLN)
1. **Sieć elektryczna — Zużycie:** Dodaj strefy `panel_energia_strefa_*` z przypisanymi encjami `..._cost`.
2. **Sieć elektryczna — Oddawanie:** Dodaj strefy produkcji `panel_energia_produkcja_strefa_*`.
3. **Magazyn energii:** W Net-billingu depozyt jest wartościowy (PLN), więc sekcja baterii kWh pozostaje pusta. Stan depozytu prezentowany jest na dedykowanym pulpicie Lovelace.

#### 🏢 Wariant 3: Konsument bez fotowoltaiki
1. Dodaj encje poboru `sensor.energa_<numer>_panel_energia_*` wraz z ich encjami kosztów `_cost`.

---

## 🎛️ Pulpity Rozliczeń i Karty Lovelace

### Automatyczny Dedykowany Pulpit
Pulpit `/energa-rachunek` jest generowany i rejestrowany automatycznie w menu bocznym HA. Jeśli zechcesz wygenerować go ponownie, wystarczy kliknąć encję:
`button.energa_<numer_licznika>_utworz_pulpit_rozliczen` lub wywołać akcję `energa_mobile.generate_dashboard`.

---

## 📋 Zestawienie Kluczowych Encji

| Nazwa encji | Klasa / Jednostka | Opis |
|---|---|---|
| `button.energa_<numer>_skonfiguruj_panel_energia` | `button` | Automatyczna 1-kliknięciem konfiguracja natywnego Panelu Energia (`.storage/energy`). |
| `sensor.energa_<numer>_syntetyczny_magazyn_*` | `energy` / `kWh` | Syntetyczne przepływy wirtualnego magazynu (ładowanie 80%/70% i rozładowanie) dla sekcji Baterie. |
| `sensor.energa_<numer>_syntetyczna_siec_pobor*` | `energy` / `kWh` | Syntetyczny pobór netto z sieci (zużycie ponad stan magazynu wirtualnego). |
| `sensor.energa_<numer>_syntetyczna_siec_oddanie` | `energy` / `kWh` | Prowizja rzeczowa OSD (20%/30% oddania za 0 zł) dla sekcji Sieć. |
| `sensor.energa_<numer>_panel_energia_*` | `energy` / `kWh` | Statystyki godzinowe dla Panelu Energia (pełna zgodność z walidacją `energy/validate`). |
| `sensor.energa_<numer>_panel_energia_*_cost` | `monetary` / `PLN` | Skumulowany koszt zużycia energii w danej strefie dla Panelu Energia. |
| `sensor.energa_<numer>_bank_wirtualny_kwh` | `energy` / `kWh` | Sumaryczny zapas energii w magazynie wirtualnym (FIFO 12m, Net-metering). |
| `sensor.energa_<numer>_bank_wirtualny_l1_dzien_kwh` | `energy` / `kWh` | Zapas energii w strefie dziennej L1 (izolacja strefowa). |
| `sensor.energa_<numer>_bank_wirtualny_l2_noc_kwh` | `energy` / `kWh` | Zapas energii w strefie nocnej/weekendowej L2 (izolacja strefowa). |
| `sensor.energa_<numer>_bank_wirtualny_pln` | `monetary` / `PLN` | Stan depozytu prosumenckiego w nowym systemie (Net-billing). |
| `sensor.energa_<numer>_magazyn_poziom` | `battery` / `%` | Procentowy poziom napełnienia magazynu energii (od 3 miesięcy historii). |
| `sensor.energa_<numer>_autokonsumpcja_dzis` | `energy` / `kWh` | Autokonsumpcja PV z precyzyjną godzinową synchronizacją. |
| `sensor.energa_<numer>_zuzycie_domu_dzis` | `energy` / `kWh` | Rzeczywiste łączne zużycie energii przez budynek (pobór + autokonsumpcja). |
| `sensor.energa_<numer>_dotychczasowy_rachunek` | `PLN` | Kwota rachunku MTD brutto od początku miesiąca do chwili obecnej. |
| `sensor.energa_<numer>_prognoza_rachunku` | `PLN` | Autonomiczna prognoza rachunku brutto na koniec miesiąca z wygładzaniem wczesnomiesięcznym. |
| `sensor.energa_<numer>_rcem_auto` | `PLN/kWh` | Oficjalna rynkowa cena RCEm pobierana automatycznie z PSE. |
| `sensor.energa_<numer>_bank_ladowanie` | `energy` / `kWh` | Skumulowana energia wprowadzona do magazynu (dla baterii w Panelu Energia). |
| `sensor.energa_<numer>_bank_rozladowanie` | `energy` / `kWh` | Skumulowana energia odebrana z magazynu (dla baterii w Panelu Energia). |
| `sensor.energa_<numer>_stan_licznika_*` | `energy` / `kWh` | Oficjalne stany liczydła OSD (import / eksport / strefy L1 i L2). |

---

## ❓ Najczęstsze Pytania (FAQ)

### Dlaczego ta integracja nie jest w oficjalnym sklepie HACS?
Integracja od ergo5 jest znakomitym, oficjalnym pakietem bazowym dla każdego. Ta integracja jest zaawansowanym projektem specjalistycznym (wersja PRO z własną bazą SQLite, dwustrefowym FIFO, predykcjami taryfowymi, autokonsumpcją i auto-provisioningiem). Aby zachować pełną elastyczność rozwoju i zaawansowaną architekturę, projekt jest dystrybuowany jako Repozytorium Niestandardowe w HACS.

### Dlaczego nie zainstalować obu integracji naraz (ergo5 + ta integracja)?
Nie rekomendujemy łączenia obu integracji na tym samym koncie. To nie jest relacja „podstawa + nakładka”. Przy równoległym odpytywaniu tego samego API Energi przez dwie integracje rośnie ryzyko blokad sesji, niespójności danych oraz konfliktów w statystykach długoterminowych Recorder. Zalecamy wybór jednego rozwiązania: wersja podstawowa od ergo5 albo samodzielna wersja PRO.

### Jak działa izolacja stref L1 i L2 w magazynie energii?
Zgodnie z rzeczywistymi rozliczeniami OSD Energa (zweryfikowanymi na fakturach rozliczeniowych), w taryfach wielostrefowych nadwyżka wyprodukowana w dzień zasila wyłącznie magazyn L1, a nadwyżka poza szczytem wyłącznie magazyn L2. Jeśli w nocy zabraknie energii w magazynie L2, pobór nocny zostanie zafakturowany, nawet jeśli w magazynie L1 pozostał duży zapas. Integracja ściśle odwzorowuje tę zasadę.

### Na jakiej podstawie wyliczany jest procent napełnienia magazynu energii (`Magazyn Poziom` / `%`)?
Wirtualny magazyn energii w systemie opustów (net-metering, art. 4 ust. 11 ustawy o OZE) nie ma stałej pojemności fizycznej (jak bateria chemiczna np. 10 kWh). Zgodnie z ustawą każda nadwyżka energii wprowadzona do sieci w danym miesiącu (pomnożona przez współczynnik opustu 0.8 lub 0.7) jest ważna do odebrania przez **12 miesięcy**, po czym niewykorzystana wygasa.

Z tego względu naturalną, dynamiczną „pojemnością maksymalną” magazynu w danym momencie jest **suma wszystkich wkładów zdeponowanych w sieci w ciągu ostatnich 12 miesięcy** (`deposits_12m_kwh`).

Poziom napełnienia magazynu wyliczany jest według wzoru:
$$\text{Poziom Magazynu (\%)} = \frac{\text{Bieżące saldo w magazynie (Bank kWh)}}{\text{Suma wkładów z ostatnich 12 miesięcy (kWh)}} \times 100\%$$

**Jak to interpretować?**
* **100%:** Przez ostatnie 12 miesięcy wyłącznie produkowałeś energię i nie odebrałeś z sieci ani jednej kilowatogodziny z magazynu (cały roczny depozyt jest nienaruszony).
* **~50%:** Z energii zdeponowanej w sieci w ciągu ostatniego roku zużyłeś do tej pory ok. 50%, a druga połowa wciąż czeka na wykorzystanie w kolejnych miesiącach przed upływem terminu 12 miesięcy.
* **0%:** Magazyn wirtualny został całkowicie wyczerpany.

Dzięki jednostce `%` oraz klasie `device_class: battery`, Home Assistant automatycznie renderuje ten sensor jako graficzny wskaźnik naładowania baterii w kafelkach i panelach Lovelace.

### Jak integracja rozróżnia stary Net-metering (opusty w kWh) od nowego Net-billingu (depozyt w PLN)?
Rozróżnienie następuje automatycznie na podstawie współczynnika prosumenckiego (`prosumer_coefficient`):
* **Współczynnik $\ge 0{,}7$ (np. 0.8 lub 0.7):** Stary system opustów (instalacje zgłoszone do 31 marca 2022 r.). Integracja prowadzi wirtualny magazyn w kWh, rozdziela strefy L1/L2 wg zasad FIFO, wylicza procent napełnienia i udostępnia przepływy ładowania/rozładowania dla baterii w Panelu Energia. Wycena sprzedaży energii (`cena_oddania`) jest w tym trybie wyłączona, ponieważ nadwyżki nie są sprzedawane, lecz deponowane wolumenowo.
* **Współczynnik $< 0{,}7$ (np. 0.0):** Nowy system Net-billing (instalacje po 1 kwietnia 2022 r.). Integracja przełącza się w tryb wartościowy: prowadzi Depozyt Prosumencki w PLN (`bank_wirtualny_pln`), pobiera oficjalne rynkowe ceny RCEm z PSE i wycenia wprowadzoną energię stawką brutto ($RCEm \times 1{,}23$).

---

## 📄 Licencja

Projekt dystrybuowany na warunkach licencji MIT. Szczegóły w pliku [LICENSE.md](LICENSE.md).
