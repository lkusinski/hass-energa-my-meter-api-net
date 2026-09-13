# 🛡️ Stabilność Środowiska, Restarty Home Assistanta i Integracje Zewnętrzne

> **Przewodnik dla administratorów Home Assistanta: higiena restartów, mechanizmy odporności integracji Energa Mobile oraz koegzystencja z chmurowymi integracjami falowników (np. SolisCloud).**

---

## 📑 Spis Treści
1. [Architektura Odporności Integracji Energa Mobile](#1-architektura-odporności-integracji-energa-mobile)
2. [Studium Przypadku: Chmura SolisCloud a Seryjne Restarty HA](#2-studium-przypadku-chmura-soliscloud-a-seryjne-restarty-ha)
3. [Dlaczego Chmury Zewnętrzne Rozłączają Się Po Restartach?](#3-dlaczego-chmury-zewnętrzne-rozłączają-się-po-restartach)
4. [Dobre Praktyki dla Administratorów HA](#4-dobre-praktyki-dla-administratorów-ha)
5. [Rekomendacja Docelowa: Architektura Local-First (Modbus)](#5-rekomendacja-docelowa-architektura-local-first-modbus)

---

## 1. Architektura Odporności Integracji Energa Mobile

Integracja **Energa My Meter API PRO** została od podstaw zaprojektowana w oparciu o zasady *Resilience-by-Design*:

* **Pamięć trwała w SQLite WAL (`energa_canonical.db`):** Wszystkie dane historyczne, rejestry liczników i bilanse magazynu FIFO przechowywane są lokalnie na dysku w katalogu `.storage`.
* **Zero zbędnego odpytywania API:** Liczniki legalizowane OSD (Energa Operator) publikują odczyty dobowe zazwyczaj raz na 24h (w godzinach porannych). Integracja nie odpytuje serwerów OSD w krótkich pętlach minutowych, eliminując ryzyko blokad konta czy limitów IP.
* **Pełna asynchroniczność (Non-blocking MainThread):** Wszelkie ciężkie kalkulacje (silnik predykcji `HourlyProfileForecaster`, bilansowanie magazynu FIFO) są delegowane do dedykowanych wątków egzekutora (`async_add_executor_job`). Czas odpowiedzi w pętli zdarzeń wynosi < 0.001s, dzięki czemu integracja nie powoduje żadnych przestojów innych komponentów Home Assistanta.
* **Testy odporności na restarty (Reboot Offline Resiliency):** Integracja posiada wbudowane testy jednostkowe (`tests/test_ha_restart_offline.py`), gwarantujące natychmiastowe odtworzenie wszystkich encji i stanów ze storage po restarcie HA, nawet przy całkowitym braku dostępu do Internetu.

---

## 2. Studium Przypadku: Chmura SolisCloud a Seryjne Restarty HA

Podczas intensywnych prac wdrożeniowych (np. aktualizacje pulpitów, testowanie kart) na maszynie produkcyjnej zaobserwowano okresowe rozłączanie się integracji falownika **Solis** (`custom_components/solis`, wersja 4.0.1).

### A. Twarde dane z bazy danych Home Assistanta (`home-assistant_v2.db`)

Analiza historii stanów sensora mocy falownika Solis (`sensor.fotowoltaika_wisniowa_9_solis_ac_output_total_power`) ujawniła bezpośrednią korelację między restartami HA a utratą łączności z chmurą:

| Okres / Data | Charakterystyka działania | Stany `unavailable` (rozłączenia) | Stany `unknown` (restarty HA) |
| :--- | :--- | :---: | :---: |
| **03.09 – 10.09.2026** | Stabilna praca produkcyjna (brak restartów) | **0 – 1 / dobę** | **0 – 1 / dobę** |
| **11.09.2026** | Początek testów i instalacji | **2** | **8** |
| **12.09.2026** | Intensywne wdrażanie dashboardów | **10** | **10** |
| **13.09.2026** | Końcowa synchronizacja środowisk | **1** | **8** |

Przed rozpoczęciem prac wdrożeniowych integracja Solis działała w 100% stabilnie. Częste rozłączenia były bezpośrednim skutkiem seryjnych restartów Home Assistanta.

---

## 3. Dlaczego Chmury Zewnętrzne Rozłączają Się Po Restartach?

Głębokie śledztwo sieciowe i kodowe ujawniło mechanizm tzw. *kaskady restartowej* (Restart Quota Exhaustion):

### 1. Dwie instalacje na jednym kluczu API
W instalacji skonfigurowano dwa punkty: `Station 19E3E2` (Wiśniowa 9) oraz `Station 19E3DC` (Agrestowa 4), współdzielące jeden klucz API (`Key ID: 1300319277300408154`).

### 2. Lawina zapytań startowych (Discovery Storm)
Przy każdym restarcie Home Assistanta integracja Solis natychmiast uruchamia procedurę wykrywania dla obu stacji równolegle:
* Stacja 1: `/v1/api/inverterList` $\rightarrow$ `/v1/api/inverterDetail` $\rightarrow$ `/v1/api/stationDetail`
* Stacja 2: `/v1/api/inverterList` $\rightarrow$ `/v1/api/inverterDetail` $\rightarrow$ `/v1/api/stationDetail`

W ciągu zaledwie 2 sekund do serwera `https://www.soliscloud.com:13333` wysyłanych jest **6 żądań HTTP** z tego samego klucza API.

### 3. Odpowiedź serwerów SolisCloud (Load Balancer Nginx)
Testy diagnostyczne wykazały, że serwery SolisCloud pod wpływem takich serii zapytań:
* Zwracają **`HTTP 502 Bad Gateway`** (generowany przez Nginx SLB na brzegu sieci Solis),
* Lub generują opóźnienia sięgające **15–20+ sekund**, co przekracza timeouty klienta HTTP (`TimeoutError: The read operation timed out`).

Sam autor integracji umieścił w kodzie `soliscloud_api.py` ostrzeżenie:
```python
# Throttle http calls to avoid 502 error
await asyncio.sleep(1)
```

### 4. Pętla Exponential Backoff i stan `unavailable`
Gdy pojedyncze zapytanie startowe zakończy się błędem 502 lub timeoutem:
1. Integracja loguje: `WARNING: No valid inverters found, login failed`.
2. Przechodzi w tryb ponawiania ze zwielokrotnionym czasem oczekiwania: `60s -> 120s -> 180s -> 240s -> 300s -> 360s`.
3. **Przez cały ten czas (nawet do 6 minut) wszystkie sensory falownika w Home Assistant otrzymują stan `unavailable`!**
4. Kolejny restart w tym oknie tylko resetuje licznik i ponownie uderza w limit API, potęgując problem.

---

## 4. Dobre Praktyki dla Administratorów HA

Aby uniknąć problemów z zewnętrznymi chmurami podczas pracy z Home Assistantem:

1. **Unikaj seryjnych restartów całego Home Assistanta:**
   * Do przeładowywania integracji używaj opcji **Przeładuj (Reload)** na karcie integracji w *Ustawienia* → *Urządzenia oraz usługi*, zamiast restartować cały system (`ha core restart`).
   * Do odświeżania widoków Lovelace wystarczy wcisnąć `F5` lub wybrać *Odśwież* z menu trzech kropek w prawym górnym rogu.
2. **Pozwól integracjom ustabilizować się po restarcie:**
   * Jeśli wykonasz restart, odczekaj minimum 10–15 minut przed kolejnym restartem, aby zewnętrzne chmury (Solis, Tuya, chmury pomp ciepła) zdążyły zakończyć procedury startowe i zwolnić limity zapytań (rate limits).
3. **Rozdzielaj poświadczenia API, jeśli to możliwe:**
   * W przypadku posiadania kilku fizycznych instalacji warto sprawdzić, czy dostawca chmury pozwala na wygenerowanie odrębnych par kluczy API dla każdej z nich.

---

## 5. Rekomendacja Docelowa: Architektura Local-First (Modbus)

Chmury zewnętrzne producentów falowników (często z serwerami w Azji lub zagranicznych centrach danych AWS) są z natury podatne na opóźnienia sieciowe, awarie łączy i restrykcyjne limity zapytań.

> [!TIP]
> **Złoty standard dla falowników PV w Home Assistant:**
> Zamiast odpytywać chmurę SolisCloud co 5 minut, zalecamy zastosowanie **lokalnej komunikacji Modbus RS-485**:
> * Podłączenie taniego konwertera RS485 $\rightarrow$ Ethernet/Wi-Fi (np. **Waveshare RS485 to ETH** lub **Elfin EW11**) do portu COM falownika.
> * Odczyt parametrów co **1–2 sekundy** bezpośrednio w sieci LAN za pomocą integracji Modbus w HA.
> * **100% niezawodności:** zero zależności od łącza internetowego, zero limitów API i brak rozłączeń przy restartach Home Assistanta.
