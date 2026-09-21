# Lab smoke test — przed każdym tagiem

Cel: żaden tag (nawet `-beta`) nie idzie bez sprawdzenia, że integracja **wstaje
na realnym Home Assistant** i liczy **co najmniej jedną fakturę**. Testy
jednostkowe/CI nie łapią problemów zależnych od skali danych i czasu — tak
przeszedł `1.9.3-beta.4`, który nie wstawał na labach (issue #4).

## Warunki wstępne
- Artefakt zbudowany z **dokładnie tego commita**, który ma być tagowany.
- Środowisko lab (VM HAOS) z jedną skonfigurowaną instancją `energa_mobile`.
- Backup `custom_components/energa_mobile` i `.storage/core.config_entries`
  przed wdrożeniem; jeden VM naraz (RAM).

## Kroki
1. **Wdróż artefakt** do gościa i uruchom Core.
2. **Setup:** wpis `energa_mobile` musi osiągnąć `state = loaded` w rozsądnym
   czasie (bez `setup_retry`/`ConfigEntryNotReady`). Sprawdź logi pod kątem
   `initial coordinator fetch failed`, `Traceback`, `Error setting up entry`.
3. **Encje:** liczba encji `energa` nie spadła względem poprzedniej wersji.
4. **Faktura:** uruchom `energa_mobile.verify_period` dla okresu z danymi i
   porównaj wynik z regresją (netto/VAT/brutto, „do zapłaty”). Oczekiwane
   wartości: patrz `lab_v193b4/RUNLOG*.md` / changelog danego wydania.
   - Gdy recorder jest wyczyszczony: `energa_mobile.fetch_history`
     (`start_date`, `days`, `meter_id`) → kanoniczna baza, potem `verify_period`
     (`opening_source = canonical`).
5. **Higiena:** `qm shutdown`, `onboot=0`, zatrzymaj serwer artefaktu.

## Kryterium PASS
- `loaded` w kroku 2, brak regresji liczby encji, faktura zgodna z regresją
  (dopuszczalne znane różnice OSD, np. ≤0,66 zł dla Agrestowej).

## Znane pułapki
- Snapshoty labów mają **wyczyszczony recorder** → używaj kanonicznej bazy.
- `entry_id` na VM mogą różnić się od historycznych — pobierz aktualne.
- Nie uruchamiaj dwóch VM jednocześnie.
