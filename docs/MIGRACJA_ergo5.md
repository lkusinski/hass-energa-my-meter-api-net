# Migracja: ergo5 ↔ nasza integracja Energa PRO

> **Status:** zweryfikowane na żywo na lab HAOS (2026-09-17).
> Obie integracje mają ten sam `domain = energa_mobile`, więc można je podmieniać **bez utraty encji
> i długoterminowych statystyk (LTS)** — pod warunkiem że wspólne `unique_id` się pokrywają
> (w sprawdzonym zakresie pokrywają się).
>
> Dokument nie zawiera danych osobowych, numerów PPE ani haseł.

## 0. Automatyczne wykrywanie obcej kopii ergo5

Ta integracja potrafi sama wykryć, że w `custom_components/` leży **obca kopia ergo5** (skopiowana
lub przemianowana). Skaner czyta `custom_components/*/manifest.json` i rozpoznaje ergo5 po
`codeowners: ["@ergo5"]` albo po adresie repo `ergo5/hass-energa-my-meter-api`.

Efekt widoczny dla użytkownika:

- **Repairs** (Ustawienia → System → Naprawy) — ostrzeżenie `ergo5_detected` z listą ścieżek.
- **Powiadomienie** trwałe w UI z krótkim komunikatem po polsku
  (`notification_id: ergo5_detected`).
- **Kreator konfiguracji** — przy dodawaniu nowego wpisu pojawia się krok ostrzegawczy z
  checkboxem „Rozumiem, kontynuuj mimo to”; bez zaznaczenia wpis nie zostanie utworzony.

Wykrycie jest tylko ostrzeżeniem: **nie** kasuje encji, nie usuwa wpisu i nie zmienia rozliczeń.
Jeśli dodatkowo wykryta zostanie instalacja ergo5 przez HACS (`.storage/hacs.repositories`),
komunikat to sygnalizuje.

> **Weryfikacja (HA 2026.9):** od HA 2023.6 powiadomienia trwałe **nie są już encjami**, więc
> `GET /api/states` ich nie pokaże. Stan sprawdzisz przez WebSocket: `persistent_notification/get`
> (lista) oraz `repairs/list_issues` (naprawy). Powiadomienia nie przeżywają restartu HA.
>
> **Znane ograniczenie:** gdy obca kopia zniknie bez restartu HA, issue `ergo5_detected` jest
> usuwany, ale powiadomienie nie jest jawnie odrzucane (`persistent_notification.async_dismiss`)
> i pozostaje w UI do restartu lub ręcznego zamknięcia. Po restarcie problem nie występuje.

## 1. Co jest zamieniane

| | ergo5 | nasza |
|---|---|---|
| repozytorium | `ergo5/hass-energa-my-meter-api` | `lkusinski/hass-energa-my-meter-api-net` |
| `domain` | `energa_mobile` | `energa_mobile` |
| numeracja wersji | `4.x` | `1.x` |
| katalog | `custom_components/energa_mobile` | `custom_components/energa_mobile` |

Ponieważ `domain` jest identyczny, Home Assistant traktuje to jako **tę samą integrację**:
wpis konfiguracyjny (`entry_id`), rejestr encji i statystyki recorder pozostają nienaruszone.
Zmienia się tylko kod, który encje tworzy.

## 2. Zmierzony efekt migracji (skrót)

Test: stan „nasza" → podmiana na ergo5 `4.16.0` → powrót na „nasza" (ten sam `entry_id`).

- **Wpis konfiguracyjny**: ten sam `entry_id`, `version`, `minor_version`, `data` i `options`
  przez cały czas — **żadna integracja go nie modyfikuje**.
- **Encje**: żadna z encji bazowych nie zmieniła `entity_id` ani `unique_id`; nic nie zniknęło z rejestru.
- **Duplikaty `_2`**: **nie pojawiły się** (brak nowych kolizji `entity_id`).
- **LTS**: liczba punktów i `sum` dla sensorów Panelu Energia (`panel_energia_strefa_1/2` i koszty)
  **bez zmian** — brak resetu do zera, brak rozjazdu statystyk.
- **Błędy**: `/api/error_log` czysty (HTTP 404) na każdym etapie.
- Po powrocie zostaje **1 osierocona encja** po ergo5 (odpowiednik „nazwy licznika"), którą można usunąć ręcznie.

## 3. Przed migracją (obowiązkowo)

1. **Backup HA**: Ustawienia → System → Kopie zapasowe → *Utwórz kopię* (pełny backup).
2. **Backup plików integracji i rejestrów** (HAOS, przez terminal/dodatek SSH):
   ```sh
   cd /mnt/data
   cp -a /mnt/data/supervisor/homeassistant/custom_components/energa_mobile \
         /mnt/data/energa_mobile.backup
   cp -a /mnt/data/supervisor/homeassistant/.storage/core.config_entries \
         /mnt/data/core.config_entries.backup
   cp -a /mnt/data/supervisor/homeassistant/.storage/core.entity_registry \
         /mnt/data/core.entity_registry.backup
   ```
   Uwaga: `/` i `/tmp` w HAOS bywają pełne — pracuj w `/mnt/data`.
3. **Zapisz stan odniesienia** (do porównania po migracji):
   liczba encji, `entity_id`, `unique_id` kluczowych sensorów, czy `/api/error_log` jest pusty.

## 4. Migracja ergo5 → nasza

### Wariant A — HACS (zalecany dla użytkownika)

1. HACS → *Integracje* → usuń repozytorium ergo5 (albo usuń integrację „Energa My Meter API (Mój Licznik API)").
2. HACS → *Integracje* → ⋮ → *Repozytoria niestandardowe* → dodaj
   `https://github.com/lkusinski/hass-energa-my-meter-api-net`, kategoria **Integration**.
3. Zainstaluj „Energa My Meter API (Mój Licznik) PRO".
4. **Zrestartuj Home Assistant**.
5. Sprawdź, że wpis `energa_mobile` się nie zdublował (powinien być dokładnie jeden) i że encje
   licznikowe oraz Panelu Energia wróciły jako aktywne.

> **HACS — ostrzeżenie:** oba dodatki mają ten sam `domain`, ale są **różnymi repozytoriami**.
> HACS nie zainstaluje obu jednocześnie. Numery wersji (`4.16.0` vs `1.x`) są nieporównywalne —
> HACS może pokazywać fałszywe „aktualizacja dostępna" / downgrade. Zignoruj to i pilnuj, że w HACS
> jest tylko jedno z tych repozytoriów.

### Wariant B — ręczna podmiana katalogu (HAOS / lab)

Nie wymaga HACS; przydatne, gdy brak dostępu do sklepu lub chcesz wymusić konkretny commit.

1. Pobierz naszą integrację (np. `git archive` z gałęzi lub tarball).
2. Podmień katalog (przykład przez plik udostępniony z innego hosta w sieci LAN):
   ```sh
   CC=/mnt/data/supervisor/homeassistant/custom_components
   cd /mnt/data/oc
   rm -rf pkglocal && mkdir pkglocal
   curl -sL -o pkg.tar.gz http://<host-lan>:<port>/<tarball>
   tar xzf pkg.tar.gz -C pkglocal
   cp -a $CC/energa_mobile /mnt/data/oc/energa_mobile.backup
   rm -rf $CC/energa_mobile
   cp -a pkglocal/custom_components/energa_mobile $CC/energa_mobile
   /usr/bin/ha core restart
   ```
3. Poczekaj, aż HA wstanie (2–4 min), i zweryfikuj wersję:
   ```sh
   grep -oE 'version"[^,]*' $CC/energa_mobile/manifest.json
   ```

> **Pułapka techniczna:** przy zdalnym wywołaniu przez `qm guest exec 127 -- /bin/sh -c "…"`
> zmienna `$CC` jest rozwijana przez shell **hosta**, nie gościa. Używaj ścieżek literalnych
> albo escapuj `$` (`\$CC`).

## 5. Weryfikacja po migracji

1. Liczba encji i pełna lista `entity_id` — bez zmian względem stanu odniesienia.
2. `unique_id` kluczowych encji (np. prognoza rachunku, import/eksport, statystyki Panelu) — bez zmian.
3. Brak nowych encji z sufiksem `_2`.
4. Statystyki LTS: liczba punktów i `sum` dla sensorów Panelu Energia nie spadły do zera.
5. `/api/error_log` → HTTP 404 (brak błędów). W UI: Ustawienia → System → *Dzienniki*.
6. Brak naprawy `ergo5_detected` w Ustawienia → System → *Naprawy* po usunięciu obcej kopii
   (powiadomienie w UI zniknie po restarcie — patrz znane ograniczenie w §0).

## 6. Weryfikacja: co zobaczysz w UI

- **Te same encje** co przed migracją są aktywne.
- **Encje drugiej integracji nie są kasowane** — przechodzą w stan `unavailable`
  z atrybutem `restored: true`. To **normalne** i **nie jest duplikatem**. Są to te same wpisy rejestru.
- **Panel Energia**: `entity_id` sensorów statystyk są zgodne w obu integracjach, więc konfiguracja
  Panelu Energia (Ustawienia → Pulpit → Energia) **nie wymaga zmian** i pokazuje dalej historię.

## 7. Revert (powrót z naszej na ergo5)

Postępuj jak w punkcie 4, tylko zamień repozytorium/katalog:

- HACS: usuń nasze repozytorium → dodaj `ergo5/hass-energa-my-meter-api` → *Install* → restart.
- Ręcznie: podmień `custom_components/energa_mobile` na wersję ergo5 → `ha core restart`.

Po powrocie na ergo5:

1. **Ustaw ceny i baseline w Opcjach** integracji. Ergo5 domyślnie nie odziedziczy naszych opcji cenowych
   (`import_price_1/2`, `export_price`) ani `balance_baseline_*` — użyje wartości domyślnych.
   Współdzielony jest tylko `prosumer_coefficient`, jeśli był ustawiony.
2. Po powrocie z ergo5 na **naszą** pozostaje **1 osierocona encja** (odpowiednik „Nazwa Licznika",
   którego nie mamy). Można ją usunąć: Ustawienia → Urządzenia i usługi → Encje → wybierz encję
   (stan `unavailable`, `restored`) → *Usuń*. Integracja celowo usuwa też nieaktualne encje
   ergo5 `..._panel_energia_produkcja_strefa_1/2_cost` (`export_1/2_cost_stats` — od v0.3.0
   koszty produkcji liczone są inaczej); to nie sieroty.
3. Nie usuwaj encji, które są tylko chwilowo `restored` — to one trzymają ciągłość historii i wrócą
   po powrocie właściwej integracji.

## 8. Ostrzeżenia zbiorcze

| Temat | Ryzyko | Postępowanie |
|---|---|---|
| Duplikaty `_2` | W teście **nie wystąpiły**; możliwe przy ręcznie zmienionych `entity_id` | Nie usuwaj rejestru; po migracji zostaw encje `restored` |
| Panel Energia | Konfiguracja zgodna (te same `statistic_id`) | Nie trzeba rekonfigurować |
| LTS / historia | Zachowana, bo `statistic_id == entity_id` | Nie usuwaj encji statystyk |
| HACS a wersje | `4.16.0` vs `1.x` — mylące | Jedno repozytorium naraz; ignoruj fałszywe aktualizacje |
| Opcje cen/baseline | ergo5 nie przenosi naszych cen | Ustaw ręcznie po migracji na ergo5 |
| Wpis `energa_mobile` | Zachowywany 1:1 | Nie usuwaj wpisu; podmieniaj tylko kod |
| Tryb `nowe`/`physical_grid` | Sensory Panelu zwracają `unknown` (native_value=None) | To nie regresja — dane płyną przez statystyki |

## 9. Rollback awaryjny

Jeśli po migracji HA nie wstaje lub encje się rozjadą:

1. Przywróć katalog integracji z backupu (`/mnt/data/energa_mobile.backup`).
2. `ha core restart`.
3. W razie problemów z rejestrem przywróć `.storage/core.entity_registry` i `.storage/core.config_entries`
   z backupu **przy zatrzymanym HA** (`ha core stop` → kopia → `ha core start`).
4. Ostatecznie: pełny backup HA.
