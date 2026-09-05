# Instrukcja Recovery i Procedury Operacyjne Energa HA

Dokumentacja procedur naprawczych, awaryjnych oraz operacyjnych dla integracji Home Assistant `Energa HA` (zgodnie z Definition of Done, Implementation Brief V1.0).

---

## 1. Ponowny import historii (Idempotentny Re-import / Backfill)

### Kiedy stosować:
- Uzupełnienie braków po dłuższej przerwie w pracy Home Assistanta.
- Wymuszenie pobrania pełnych 730 dni historii dla nowej instalacji.
- Naprawa zniekształceń w wykresach po awariach sieciowych.

### Jak wywołać:
W Home Assistant przejdź do: **Narzędzia deweloperskie → Usługi** i wywołaj:
```yaml
service: energa_mobile.fetch_history
data:
  start_date: "2024-09-01"
  days: 730
```

### Dlaczego to jest bezpieczne (Idempotencja):
1. **Baza SQLite WAL:** Każdy odczyt zapisywany jest do tabeli `interval_reading` z kluczem unikalnym `(event_key, revision)` z regułą `ON CONFLICT DO NOTHING`. Ponowne wstawienie tego samego dnia jest operacją typu no-op.
2. **Kotwiczenie sum Recordera (`initial_sum`):** Reimport częściowy nie zeruje licznika `running_sum`. Suma startuje od ostatniego znanego stanu sprzed importowanego zakresu (`_stat_sum_before`), eliminując ryzyko resetów i dołów w statystykach Energy Dashboard.
3. **Praca w tle:** Usługa wraca natychmiast, a postęp raportowany jest w powiadomieniach persistent notification (`energa_import_*`).

---

## 2. Obsługa Korekt Odczytów (Late Corrections & Revisions)

### Kiedy stosować:
- Operator OSD lub PSE publikuje skorygowane dane pomiarowe za minione miesiące.
- Rozbieżność między szacunkiem a oficjalną korektą faktury.

### Architektura zapisu (Append-Only):
- Baza kanoniczna **nigdy nie usuwa wierszy** (`DELETE` jest zabroniony dla zdarzeń pomiarowych i finansowych).
- Korekta rejestrowana jest z wyższym numerem rewizji (`revision = 2`) w tabeli `interval_reading` oraz wpisem w `reading_revision` z polem `provenance` i powodem korekty.
- Widok odczytów wybiera najwyższą rewizję dla danego `event_key`:
  ```sql
  SELECT * FROM interval_reading r
  WHERE r.ppe_id = ? AND r.revision = (
      SELECT MAX(revision) FROM interval_reading sub WHERE sub.event_key = r.event_key
  );
  ```

---

## 3. Wymiana Fizycznego Licznika (Meter Replacement)

### Problem historyczny:
Gdy u użytkownika (np. przypadek instalacji G12w z fotowoltaiką po wymianie legalizacyjnej licznika w maju 2026 r.) dochodzi do legalizacyjnej wymiany licznika, nowy licznik startuje od stanu 0 kWh, podczas gdy historia gospodarstwa wynosi kilka tysięcy kWh. Identyfikacja po numerze seryjnym powodowała rozpad historii i zafałszowanie banku.

### Procedura rozwiązania:
1. **Nadrzędna tożsamość logiczna PPE:**
   Wszystkie sensory i statystyki długoterminowe są zakotwiczone w stałym `PPE` (np. `energa_mobile:PL_12345__grid_import_total`), a nie w numerze seryjnym licznika.
2. **Rejestracja cyklu życia (`meter_lifecycle`):**
   W bazie SQLite zapisywany jest rekord graniczny dla starego i nowego licznika:
   ```python
   # Stary licznik: zamknięcie ważności
   # Nowy licznik: valid_from = data montażu, offset_kwh = stan końcowy starego licznika
   storage.add_meter_lifecycle(
       MeterLifecycle(
           ppe_id="PL_12345",
           meter_id="new_meter_id",
           serial="NEW_SERIAL_2026",
           register="1.8.0",
           valid_from=datetime(2026, 5, 9),
           offset_kwh=Decimal("5192.350"),
       )
   )
   ```
3. **Ciągłość statystyk:**
   Energy Dashboard nie odnotowuje skoku ani spadku – suma rośnie płynnie z uwzględnieniem offsetu.

---

## 4. Rollback i Kopia Bezpieczeństwa Bazy SQLite

### Lokalizacja bazy danych:
Baza kanoniczna integracji znajduje się w standardowym katalogu Home Assistanta:
```
/config/.storage/energa_canonical.db
/config/.storage/energa_canonical.db-wal
/config/.storage/energa_canonical.db-shm
```

### Wykonanie kopii zapasowej przed większymi zmianami:
Przed migracją lub testami wystarczy skopiować plik bazy (przy włączonym trybie WAL zalecane jest wykonanie checkpointu lub zatrzymanie HA):
```bash
cp /config/.storage/energa_canonical.db /config/.storage/energa_canonical.db.bak
```

### Przywrócenie (Rollback):
1. Wyłącz Home Assistant Core (`ha core stop` lub restart kontenera).
2. Przywróć plik `.bak`:
   ```bash
   cp /config/.storage/energa_canonical.db.bak /config/.storage/energa_canonical.db
   rm -f /config/.storage/energa_canonical.db-wal /config/.storage/energa_canonical.db-shm
   ```
3. Uruchom Home Assistant Core (`ha core start`).

---

## 5. Diagnostyka Połączenia i Błędy API Energa

### Kody błędów i zachowanie integracji:

| Kod błędu / Objaw | Przyczyna | Reakcja systemu |
| :--- | :--- | :--- |
| **429 Too Many Requests** | Zbyt częste zapytania do portalu Mój Licznik | Automatyczny exponential backoff z losowym jitterem (klient `EnergaApiClient`); nie spamuje API. |
| **401 Unauthorized** | Wygaśnięcie tokenu sesji | Automatyczna re-autentykacja (`async_login`). Jeśli hasło zostało zmienione, integracja przechodzi w stan `ConfigEntryAuthFailed`. |
| **5xx Server Error** | Przerwa techniczna / awaria serwerów Energi | Do 3 automatycznych ponowień z przerwą; zachowanie stanu offline-first. |
| **Restart HA bez sieci** | Brak połączenia z Internetem | **Brak zerowania encji:** sensory zachowują ostatnie znane stany z bazy SQLite; nie są emitowane sztuczne zera do Recordera. |

### Dostęp do logów diagnostycznych:
Aby włączyć szczegółowe logowanie diagnostyczne integracji, dodaj do `configuration.yaml`:
```yaml
logger:
  default: warning
  logs:
    custom_components.energa_mobile: debug
```
