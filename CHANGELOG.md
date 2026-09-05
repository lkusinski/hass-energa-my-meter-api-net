# Changelog

## v1.0.6 (2026-09-05) — Standaryzacja Nazewnictwa Encji do Standardu Home Assistant (Opcja A)

### 🚀 Standaryzacja Nazw & Pełna Zgodność z HA Core
- **Spójne, kanoniczne nazewnictwo encji (`has_entity_name = True`):**
  - Wszystkie sensory integracji posiadają teraz czyste, profesjonalne nazwy w języku polskim w `_attr_name` (np. `Bank Wirtualny kWh`, `Prognoza Rachunku`, `Dotychczasowy Rachunek`, `Koszt Brutto MTD`, `Magazyn Poziom`) bez ręcznie doklejanych numerów seryjnych `({serial})`.
  - Przypisano brakujące `self._attr_device_info = device_info` w klasach sensorów bankowych i rozliczeniowych (`EnergaBankKwhSensor`, `EnergaBankPlnSensor`, `EnergaFirstDataDateSensor`, `EnergaBillForecastSensor`).
  - Home Assistant automatycznie i spójnie generuje identyfikatory encji w przestrzeni urządzenia: `sensor.energa_<serial>_<slug>` (np. `sensor.energa_00069839_bank_wirtualny_kwh`, `sensor.energa_11685328_bank_wirtualny_pln`, `sensor.energa_11685328_prognoza_rachunku`).
  - Wyeliminowano podwójne numery liczników w nazwach (np. `sensor.energa_00069839_magazyn_poziom_00069839` -> `sensor.energa_00069839_magazyn_poziom`).
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
  - Dodano nową dedykowaną encję `sensor.energa_<meter_id>_dotychczasowy_rachunek` (np. `Dotychczasowy Rachunek (11685328)`).
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
