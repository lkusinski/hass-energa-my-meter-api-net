# Różnica na L2 vs faktura OSD (≈0,66 zł)

Dokument wyjaśnia, dlaczego kalkulator `verify_period` może pokazać o
**~0,53 zł netto / ~0,66 zł brutto mniej** niż faktura sprzedawcy, mimo że
**metoda rozliczeń jest poprawna**. Chodzi o pojedynczy efekt **±1 kWh w
strefie nocnej (L2)** wynikający z danych, nie z formuły.

> Zakres: net-billing / taryfa G12W, okres **sierpień 2026**, adres testowy
> „Agrestowa". Bez danych osobowych i identyfikatorów — wyłącznie liczby
> rozliczeniowe, które i tak są widoczne na fakturze.

## 1. Sedno

Faktura rozlicza **sumy godzinowych sald dodatnich per strefa** (dodatnie
różnice `pobór − oddanie` w każdej godzinie, zsumowane w obrębie strefy), a
sprzedawca (OSD) podaje je w **całych kWh**:

- L1 (dzień): **398 kWh**
- L2 (noc): **309 kWh**

Nasze szeregi godzinowe z API `mchart`, po **takim samym godzinowym
netowaniu**, dają:

- L1: **398,46 kWh** → 398
- L2: **308,293 kWh** → **308**

Różnica na L2 to ~0,7 kWh i po zaokrągleniu do całych kWh daje **1 kWh mniej**
niż faktura.

## 2. Efekt 1 kWh na L2 (netto → brutto)

| Pozycja | Faktura (309 kWh) | Kalkulator (308 kWh) | Różnica |
|---|---|---|---|
| Energia czynna nocna (0,3990 zł/kWh) | 123,29 | 122,89 | **+0,40** |
| Sieciowa zmienna nocna (0,0851 zł/kWh) | 26,30 | 26,21 | **+0,09** |
| Jakościowa + OZE + kogeneza (baza 707 vs 706 kWh) | — | — | **+0,04** |

Suma różnic: **+0,53 zł netto**, co po VAT daje **+0,66 zł brutto**.

Dla całego rachunku:

| | Faktura | Kalkulator (API) |
|---|---|---|
| Netto | **628,55** | 628,02 |
| VAT | 144,57 | 144,44 |
| Brutto | **773,12** | 772,46 |
| Do zapłaty (po depozycie) | **615,53** | 614,87 |

## 3. Przyczyna

Różnica ~0,7 kWh w strefie nocnej wynika z tego, że **własne godzinowe
bilansowanie sprzedawcy (OSD)** i **nasza godzinowa seria z API** nie muszą
idealnie pokryć się co do granic godzin i precyzji. Wpływają na to:

- precyzja/zaokrąglenia wartości z API,
- granice godzin (przypisanie danego interwału do L1/L2),
- kolejność i sposób netowania w obrębie godziny.

**Nie jest to błąd formuły** — ta sama formuła odtwarza fakturę co do grosza,
gdy wejściowe kWh się zgadzają (patrz sekcja 4).

## 4. Weryfikacja (dlaczego to nie błąd)

- **Akcyza, depozyt, taryfa i dystrybucja zgadzają się co do grosza.**
  Depozyt: `435 × 0,29453 × 1,23 = 157,59` zł (RCEm = 0,29453).
- **Inne adresy trafiają co do grosza:**
  - Wiśniowa (07–08.2026): **158,72** zł,
  - Bursztynowa (08.2026, G11): **84,44** zł.
- Wyłącznie Agrestowa sierpień wykazuje różnicę ±1 kWh na L2, co potwierdza jej
  **daniowe**, a nie metodyczne źródło.

## 5. Wniosek

Ta **±1 kWh (≈0,66 zł)** jest nieusuwalna bez **surowego dziennika godzinowego
sprzedawcy** — nie mamy wglądu w dokładne, niezaokrąglone wartości, które OSD
przypisał do strefy nocnej. Rozbieżność jest jednak **audytowalna**: kalkulator
podaje użyte kWh w atrybucie `kwh` wyniku (m.in. `saldo_plus_1`/`saldo_plus_2` —
sumy godzinowych sald dodatnich per strefa), więc każdy może porównać je
z fakturą i samodzielnie ocenić, że różnica to 1 kWh, a nie błąd rozliczeń.

## 6. Nota akcyzy a wiersz faktury

Na fakturach Energa nota „Na fakturze naliczono akcyzę …" podaje akcyzę liczoną od
**poboru brutto**, a wiersz pozycji „akcyza" — od **nakładki** (pobór brutto − salda
dodatnie). Przykłady:

| Faktura | Nota (pobór brutto) | Wiersz „akcyza" (nakładka) |
|---|---|---|
| Bursztynowa FES/00027 (08.2026) | 0,90 zł za 179 kWh | 0,149 MWh = **0,75 zł** |
| Bursztynowa FES/00025 (06.2026) | 0,81 zł za 161 kWh | 0,147 MWh = **0,74 zł** |
| Agrestowa FES/00045 (08.2026) | 3,85 zł za 768 kWh | 0,038 + 0,023 MWh = **0,31 zł** |

Wniosek: do rachunku wchodzi akcyza **od nakładki**; nota to ustawowy odczyt od energii
pobranej (pobór brutto) i należy ją ignorować przy obliczeniach. Kalkulator integracji
postępuje zgodnie z wierszem (potwierdzone co do grosza na FES/00025 i FES/00027).

Powiązane: [`DASHBOARD.md`](DASHBOARD.md) (studium przypadku „weryfikacja co do
grosza"), [`BANK.md`](BANK.md) (weryfikacja fakturowa i kotwice liczbowe).
