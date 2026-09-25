# 🗺️ Mapa Rozwoju i Katalog Propozycji (Roadmap & Feature Proposals)

> **Niniejszy dokument gromadzi propozycje rozbudowy integracji Energa My Meter API PRO w podziale na obszary funkcjonalne. Czekamy na zgłoszenia użytkowników i opinie w GitHub Issues.**

---

## 📑 Spis Treści
1. [Status Obecny (Wersja v1.9.3)](#1-status-obecny)
2. [Obszar 1: Wizualizacja i Nowoczesny UX Lovelace](#2-obszar-1-wizualizacja-i-nowoczesny-ux-lovelace)
3. [Obszar 2: Sensory Decyzyjne i Gotowe Automatyzacje (Blueprints)](#3-obszar-2-sensory-decyzyjne-i-gotowe-automatyzacje-blueprints)
4. [Obszar 3: Dynamiczne Ceny PSE, BESS i Arbitraż Energetyczny](#4-obszar-3-dynamiczne-ceny-pse-bess-i-arbitraż-energetyczny)
5. [Obszar 4: Obsługa Niszowych Taryf i Nowych Modeli OSD](#5-obszar-4-obsługa-niszowych-taryf-i-nowych-modeli-osd)
6. [Jak Zgłaszać Propozycje i Problemy?](#6-jak-zgłaszać-propozycje-i-problemy)

---

## 1. Status Obecny

Integracja posiada w pełni przetestowany i zweryfikowany na 5 środowiskach produkcyjnych fundament:
* ✅ Odczyt i lokalny cache w SQLite WAL (zero zbędnych zapytań do API OSD).
* ✅ Precyzyjne bilansowanie wirtualnego magazynu FIFO (Net-metering 0.8 / 0.7 z izolacją stref L1/L2).
* ✅ Obsługa depozytu prosumenckiego Net-billing z oficjalnymi cenami rynkowymi RCEm PSE.
* ✅ Autonomiczny pulpit `/energa-rachunek` z adaptacją do profilu instalacji (G11, G12, Net-billing).
* ✅ 805 zautomatyzowanych testów jednostkowych (`pytest tests`; 1 skipped).
* ✅ Kanoniczna tożsamość odczytów i baza jako źródło godzinowe z `kwh_source` (v1.9.3).

### 📌 Zadania do wdrożenia przy okazji najbliższego wydania (Backlog UX & Sensors):
* [x] **Kompatybilność testów jednostkowych na platformie Windows (Issue #3):**
  * Zastąpienie `NamedTemporaryFile` przez standardową fixture pytestową `tmp_path` w testach bazy SQLite, eliminując błąd blokady plików (`PermissionError`) na Windowsie.
* [ ] **Sumaryczny pobór MTD dla taryf strefowych (`sensor.energa_{serial}_pobor_energii_mtd`):**
  * Obecnie w G12/G12w tworzone są osobne sensory `Pobór Energii Strefa 1 MTD` i `Strefa 2 MTD`.
  * Dodać także sumaryczny sensor poboru w kWh dla całego miesiąca (biorący wartość z `mtd_import_kwh`), aby użytkownik miał obok siebie `Pobór Energii MTD` (kWh) oraz `Dotychczasowy Rachunek` (PLN).
* [ ] **Domyślne ukrycie technicznych sensorów statystyk (`_attr_entity_registry_visible_default = False`):**
  * Dla sensorów `EnergaStatisticsSensor` i `EnergaCostStatisticsSensor` (`Panel Energia...`).
  * Zapobiegnie to wyświetlaniu mylącego statusu `nieznany` na kartach urządzeń w interfejsie HA, zachowując pełne działanie w tle dla bazy LTS i oficjalnego Panelu Energia.
  * **Gotowe na gałęzi `feature/hide-technical-stats-sensors`** (commit `2a4c34a`, poza `main` celowo) — do scalenia jako początek kolejnego wydania (1.9.4 / 1.10).

---

## 2. Obszar 1: Wizualizacja i Nowoczesny UX Lovelace

* [ ] **Zaawansowane wykresy słupkowe doby (ApexCharts Card z HACS):**
  * Słupkowy profil dobowy z podziałem kolorystycznym na strefy:
    * 🔴 **T1 (Szczyt):** droższa energia.
    * 🟢 **T2 (Pozaszczyt / weekend):** tańsza energia.
  * Wykres słupkowy „Dzień po dniu” z ostatnich 30 dni z naniesioną linią średniego zużycia oraz zaznaczonymi weekendami.
* [ ] **Karta „Najbliższe tanie okno energii” (Smart Energy Window):**
  * Dynamiczny wskaźnik informujący domowników w prosty sposób:
    * *„Strefa pozaszczytowa aktywna jeszcze przez 2h 15m (oszczędzasz ~40%) — idealny czas na pralkę / zmywarkę”*.
    * *„Tani prąd rozpocznie się o 13:00 (za 45 minut)”*.
* [ ] **Karta porównawcza: Falownik PV vs Licznik OSD (Realna Autokonsumpcja):**
  * Zestawienie na jednym wykresie:
    1. Produkcja brutto z falownika (np. Solis, Huawei, Fronius).
    2. Wprowadzenie do sieci wg licznika legalizowanego Energa Operator.
    3. Różnica = **rzeczywista autokonsumpcja domu w czasie rzeczywistym**.

---

## 3. Obszar 2: Sensory Decyzyjne i Gotowe Automatyzacje (Blueprints)

* [ ] **Dedykowane sensory binarne rekomendacji (`binary_sensor`):**
  * `binary_sensor.energa_tania_strefa` — stan `on` w godzinach strefy pozaszczytowej (T2 w G12/G12w).
  * `binary_sensor.energa_ujemne_ceny_rce` — stan `on`, gdy cena rynkowa PSE spada poniżej zera (zagrożenie opłatami przy eksporcie PV).
  * `sensor.energa_rekomendacja_ladowarki` — rekomendacja najtańszego przedziału czasowego na ładowanie samochodu elektrycznego (np. BMW EV / Tesla).
* [ ] **Sensor jakości i świeżości danych (`sensor.energa_jakosc_danych`):**
  * Informacja o dacie ostatniego kompletnego odczytu OSD, statusie zdalnej transmisji licznika oraz ewentualnych brakach w danych z wczorajszego dnia.
* [ ] **Oficjalne szablony automatyzacji (Home Assistant Blueprints):**
  * *Alert o nowym odczycie dobowym:* Powiadomienie na telefon każdego ranka z podsumowaniem wczorajszego zużycia i kosztu.
  * *Alert anomalii zużycia:* Powiadomienie, gdy dzienne zużycie przekracza średnią z ostatnich 7 dni o ponad 50%.
  * *Ostrzeżenie o ujemnych cenach dynamicznych:* Powiadomienie po godzinie 14:00 o planowanych na jutro ujemnych cenach energii.

---

## 4. Obszar 3: Dynamiczne Ceny PSE, BESS i Arbitraż Energetyczny

* [ ] **Pełna ekspozycja silnika arbitrażowego (`ArbitrageEngine`):**
  * Integracja posiada już w kodzie zaawansowany silnik arbitrażu bateryjnego uwzględniający sprawność cyklu magazynu BESS (np. 88%).
  * Planowane wyeksponowanie jako natywne sensory Home Assistanta:
    * `sensor.energa_rekomendacja_ladowania_baterii`
    * `sensor.energa_rekomendacja_rozladowania_baterii`
    * `sensor.energa_szacowany_zysk_z_arbitrazu_dzis`
* [ ] **Automatyczna integracja z wbudowanym panelem Home Assistant *Energy*:**
  * Rozszerzenie przycisku autokonfiguracji o automatyczne mapowanie wirtualnych magazynów i energii sieciowej dla nowych użytkowników.

---

## 5. Obszar 4: Obsługa Niszowych Taryf i Nowych Modeli OSD

Poszukujemy użytkowników i danych testowych dla:
1. **Taryfy trójstrefowej G13:** Podział wirtualnego magazynu na 3 strefy (przedpołudnie, szczyt popołudniowy, noc).
2. **Taryf G12r / G12as:** Taryfy antysmogowe z obniżonymi stawkami nocnymi.
3. **Instalacji prosumenckich > 10 kWp:** Weryfikacja współczynnika opustu **0.7** na rzeczywistych fakturach.
4. **Taryf biznesowych C11 / C12a / C12b:** Dla małych przedsiębiorstw podłączonych do sieci Energa Operator.

---

## 6. Jak Zgłaszać Propozycje i Problemy?

Jeśli chcesz zgłosić nową funkcję, napotkasz błąd w rozliczeniach lub chcesz przekazać dane do przetestowania nowej taryfy:

1. Otwórz zgłoszenie w repozytorium GitHub: **[Issues](https://github.com/lkusinski/hass-energa-my-meter-api-net/issues)**.
2. Wybierz odpowiedni szablon (*Bug Report* lub *Feature Request*).
3. Pamiętaj o **anonimizacji danych osobowych** (nie podawaj imienia, nazwiska, adresu ani haseł — wystarczą zanonimizowane identyfikatory encji i profile taryfowe).
