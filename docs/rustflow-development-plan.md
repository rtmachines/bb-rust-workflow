# Plan rozwoju bb-rust-workflow — mechanizmy inspirowane Rustflow

Data: 2026-10-25
Status: etapy 1–2 zaimplementowane i zweryfikowane; etap 4 zaimplementowany na osobnej gałęzi; etapy 3 i 5 niezależnie do realizacji

## Cel i zachowane gwarancje

Plan obejmuje pięć etapów wdrożenia, każdy zakończony osobną weryfikacją. Celem jest połączenie obecnej kontroli procesu z lepszą diagnostyką Rust i jawnym rozliczaniem wymagań.

Zachowujemy kolejność implementacja → kontrole → dwa niezależne review → triage/poprawki, pracę na kopiach, limity oraz jawne `apply`. Nowe mechanizmy będą uzupełniały warunki sukcesu.

## 1. Pełny zakres kontroli Rust i zgodność konfiguracji

**Status: zakończony i zweryfikowany 2026-10-25.**

Najpierw uzupełniamy domyślny zestaw:

- formatowanie całego workspace;
- `cargo check --workspace --all-targets --locked`;
- `cargo test --workspace --all-targets --locked`;
- osobne `cargo test --workspace --doc --locked`;
- Clippy dla całego workspace i wszystkich targetów, z ostrzeżeniami traktowanymi jako błędy.

Szablon tworzony przez `init` zawiera `prepare = [["cargo", "check", "--workspace", "--all-targets"]]`, uruchamiane przed zamrożeniem źródeł. Pominięte pole `prepare` oznacza pustą listę we wszystkich wersjach konfiguracji, podobnie jak `Config()` zachowuje pustą listę dla zgodności API. Każda późniejsza zmiana źródeł unieważnia kontrolę.

Etap 1 wprowadził `config_version = 2`; od etapu 4 szablon i `Config()` mają wersję 3. Brak pola oznacza wersję 1; jawna wersja 1 jest również obsługiwana, a inne nieznane wersje są odrzucane. Jeśli `checks` nie podano, v2 i v3 używają powyższego pełnego zestawu, a v1 zachowuje dawny fallback: `cargo fmt --check`, `cargo check --all-targets`, `cargo test --all-targets`, `cargo clippy --all-targets -- -D warnings` (bez `--locked`). Historyczne jawne komendy z szablonu zawierają `--locked` i pozostają bez zmian. We wszystkich obsługiwanych wersjach jawne `checks` pozostają dokładnie takie, jak skonfigurował użytkownik.

Migracja do pełnego zakresu v2 wymaga ustawienia `config_version = 2` oraz świadomego zastąpienia lub usunięcia jawnego `checks`; samo podbicie wersji nie zmienia tych poleceń. Nie włączamy automatycznie `--all-features`. `plan` pokazuje wersję, tryb zgodności, zakres i dokładne komendy. Zmiany obejmują [szablon konfiguracji](../src/bb_workflow/default.toml) i [wartości domyślne w kodzie](../src/bb_workflow/config.py).

**Odbiór etapu:** błąd w pakiecie spoza `default-members` oraz błędny doctest blokują sukces. Stare własne komendy nie są po cichu zastępowane.

**Weryfikacja:** pełny zestaw obejmuje 210 testów: 208 zaliczonych, 2 dotyczące natywnych junctions Windows pominięte na Linux. Trzy testy w `tests/test_rust_checks.py` wykonują rzeczywiste polecenia Cargo offline: sprawdzają błąd poza `default-members`, błędny doctest mimo zaliczenia `--all-targets` oraz poprawny przebieg z przygotowaniem lockfile i zamrożonymi źródłami. Testy konfiguracji i CLI potwierdzają zachowanie jawnych komend oraz widoczną migrację. Ruff, lokalny reviewdog i kontrola formatowania zakończyły się pomyślnie. Niezależny przegląd nie wykazał usterek. Dostawców modeli zastąpiły atrapy.

## 2. Wspólne wykonywanie kontroli, szybkie profile i warianty

**Status: implementacja, weryfikacja i przeglądy zakończone 2026-10-25.**

Wykonanie kontroli jest wydzielone z `engine.py` do niewielkiego modułu `verification.py`. Korzystają z niego zarówno orkiestrator, jak i polecenie `verify`. `process.py` nadal odpowiada za procesy, timeouty i sprzątanie.

Profile kontroli umieszczamy w **`[verification.profiles.*]`**. Obecne `[profiles.low|medium|high]` nadal oznaczają dobór modeli.

| Profil | Zakres | Przeznaczenie |
| --- | --- | --- |
| `fast` | Formatowanie i sprawdzenie kompilacji | Szybka informacja po zmianie |
| `focused` | Kompilacja i wybrane testy pakietu | Diagnoza i test regresji |
| `lint` | Clippy | Celowana analiza kodu |
| `gate` | Pełny zestaw z etapu 1 | Obowiązkowa weryfikacja końcowa |

Dodaliśmy jawne warianty features, targetu i toolchainu w `[verification.variants.*]`. Gate wykonuje wszystkie przypisane mu warianty, w kolejności konfiguracji, dla poleceń z głównego `checks`. Główne `checks` pozostaje jedynym źródłem komend gate; profil `gate` nie może definiować własnych komend. Szybkie profile mogą mieć własne listy poleceń. Profile weryfikacji są odrębne od modelowych `[profiles.low|medium|high]`. Nie włączamy automatycznie `--all-features`; macierz ma odzwierciedlać wspierane konfiguracje projektu.

Interfejs polecenia:

```text
bb-workflow verify fast --repo . --package recipe-core
bb-workflow verify focused --repo . --package recipe-core --test-filter upper_boundary
bb-workflow verify gate --repo .
```

Profile `fast`, `focused` i `lint` przyjmują opcjonalne `--package`; `--test-filter` jest dostępne wyłącznie dla `focused`. `gate` odrzuca zawężanie i uruchamia wszystkie skonfigurowane warianty. `verify` działa w rzeczywistym repozytorium źródłowym, pod blokadą, bez agentów i bez `prepare`; używa istniejących źródeł i lockfile. Raporty `report.json` i `report.md` wraz z pełnymi logami trafiają do `.bb-workflow/verifications/<id>/`. Sukces ma status `passed` i `apply_eligible = false`. Manifest źródeł jest sprawdzany przed oraz po kontrolach; zmiany zostają zgłoszone, ale nie są cofane. Przy `--locked` źródła oraz `Cargo.lock` muszą być już przygotowane.

Domyślne profile obejmują: `fast` — formatowanie i `cargo check`; `focused` — `cargo check` oraz testy; `lint` — Clippy; `gate` — główne `checks`. Własne komendy zachowują argv bez zmian, gdy nie zażądano transformacji. Niejednoznaczne zawężanie lub flagi wariantu powodują czytelny błąd konfiguracji. Kody wyjścia `verify`: `0` dla zaliczenia, `1` dla niepowodzenia/zablokowania/wyczerpania budżetu, `130` dla przerwania i `2` dla błędu konfiguracji lub użycia.

**Odbiór etapu:** szybki profil nie może nadać wynikowi statusu uprawniającego do `apply`. Gate odrzuca zawężanie pakietu lub filtra testów. W `run` pełny gate nadal poprzedza recenzje każdego kandydata.

**Weryfikacja etapu:** pełny zestaw obejmuje 238 testów: 236 zaliczonych i 2 pominięte testy natywnych junctions Windows na Linux. Pięć testów w `tests/test_rust_checks.py` wykonuje rzeczywiste kontrole Cargo offline, w tym dwa przypadki dodane w etapie 2. Ruff, lokalny reviewdog i kontrola formatowania zakończyły się pomyślnie. Niezależny przegląd implementacji oraz końcowy przegląd całej gałęzi zatwierdziły zmiany bez krytycznych, ważnych ani drobnych usterek do poprawienia. Kontrole uruchomiono na Linux; testy Windows pominięto, a dostawców modeli zastąpiły atrapy. Transformacje własnych komend Cargo celowo obsługują zachowawczy podzbiór argumentów. Obsługa częściowych wyników po przerwaniu procesu pozostała bez zmian. Etapy 3–5 pozostają do realizacji.

## 3. Klasyfikacja diagnostyki względem baseline

Dodajemy `diagnostics.py`, który analizuje komunikaty Cargo/rustc/Clippy i tworzy skrót dla agenta. Pełne stdout/stderr pozostają w audycie.

Rozdzielamy **wynik wykonania polecenia** od **wyniku parsowania diagnostyki**. Pusta lista znalezionych błędów nie oznacza zaliczonej kontroli.

| Klasyfikacja | Znaczenie |
| --- | --- |
| `new` | Problem nieobecny w porównywalnym baseline |
| `persistent` | Problem nadal występuje |
| `resolved` | Usunięcie potwierdzone zakończoną kontrolą właściwego zakresu |
| `unclassified` | Brak porównywalnego punktu odniesienia |
| `unobserved_previous` | Wcześniejszego problemu nie udało się obecnie ocenić |

Porównywalność obejmuje zakres pakietów i testów, wariant, polecenia, wersje narzędzi oraz istotną konfigurację. Ścieżki normalizujemy względem projektu, ponieważ baseline i kandydat znajdują się w różnych katalogach. Numer linii nie może być jedyną podstawą identyfikacji problemu.

Wyjście testów obsługujemy ostrożnie: Cargo JSON nie zapewnia jednolitego formatu wszystkich rezultatów testów. Nierozpoznany wynik zachowujemy jako niepewny wraz z logiem.

**Odbiór etapu:** błąd kompilacji uniemożliwiający wykonanie testów nie oznacza naprawienia wcześniejszych błędów testowych. Timeout, ucięte wyjście i zmieniony zakres również nie dają `resolved`. Czerwony baseline nadal nie zwalnia z zielonego gate.

## 4. Jawne kryteria odbioru powiązane z dowodami

**Status: implementacja etapu 4 gotowa; weryfikacja opisana poniżej. Etap 3 nie jest zależnością.**


Dodajemy plik wymagań przekazywany do `run`, np.:

```text
bb-workflow run "Popraw walidację nastawy" --requirements task.toml
```

Każde kryterium otrzymuje identyfikator, opis i sposób weryfikacji. Obsługujemy dwa rodzaje:

- **Mechaniczne:** wymagają konkretnej kontroli lub testu. Wynik rozstrzyga program.
- **Semantyczne:** dotyczą zachowania, zgodności z wymaganiem lub ograniczenia zakresu zmian. Oceniają je istniejący recenzenci.

Specyfikację zapisujemy przed implementacją. Każda ocena zostaje związana z:

```text
wersją kandydata + zestawem kryteriów + planem kontroli
```

Implementer i Fixer mogą wskazywać dowody, ale nie zatwierdzają własnej pracy. Obaj recenzenci dostają te same kryteria, bez wzajemnych ocen. Spór trafia do istniejącego triage:

- potwierdzony, naprawialny brak → Fixer i ponowna weryfikacja;
- brak możliwości rozstrzygnięcia → `blocked`;
- spełnione wszystkie kryteria oraz pozostałe warunki → `succeeded`.

Triage nie może nadpisać nieudanego wyniku kontroli mechanicznej. Przefiltrowany test, który wykonał **zero przypadków**, nie stanowi dowodu spełnienia kryterium.

Rozszerzenie wymaga wersjonowanego schematu odpowiedzi — obecny [protokół](../src/bb_workflow/protocol.py) odrzuca nieoczekiwane pola. Nowe konfiguracje wymagają niepustych kryteriów; stare działają w wyraźnie oznaczonym trybie zgodności, bez deklarowania nowych gwarancji.

**Odbiór etapu:** brak oceny kryterium, nieznany identyfikator, nieaktualny dowód lub pozytywna deklaracja implementera nie pozwalają zakończyć zadania sukcesem.

**Implementacja:** plik TOML `requirements_version = 1` zawiera niepuste `criteria` z unikalnym
`id`, `description`, `kind` i wymaganym opisem `verification`. Mechaniczne kryterium wiąże
`check_id` i opcjonalne dokładne `test_name` z całym, niezwężanym gate. Program wymaga aktualnego
wyniku komendy; bezpośredni Cargo test zawsze wymaga dodatniej liczby zaliczonych nieignorowanych
testów, a nazwany test również własnej zakończonej linii sukcesu. Nieznany harness pozostaje
nierozstrzygnięty. Własne komendy nietestowe opierają się na zaufaniu do poprawnego zakończenia.

V3 wymaga kryteriów w `run`. V1/v2 bez pliku zachowują jawny `legacy`; podanie pliku włącza
protokół v2. `plan` obsługuje plik i raportuje brak wymaganych kryteriów; `verify` i `doctor`
działają bez niego. Zamrożone kopie konfiguracji, wymagań i planu zapobiegają zmianie kontraktu
w trakcie przebiegu. Znormalizowane pliki i hashe trafiają do katalogu audytu przed implementacją.
Implementer/Fixer dostają powiązanie wejściowej migawki i mogą jedynie proponować dowody.
Obaj recenzenci oceniają każdy semantyczny ID; każde inne zestawienie niż dwa `satisfied`
uruchamia triage, także bez ustaleń kodowych. Brak oceny/ID/powiązania odrzuca odpowiedź.
`unmet` kieruje do Fixera bez względu na `blocking_severities`, `unresolved` blokuje;
nowy kandydat przechodzi cały gate i oba niezależne review od początku.

Podstawowy stan i raport ujawniają tryb oraz hashe; szczegóły rund są w `acceptance.rounds`.
Rozszerzony końcowy raport oraz sprawdzenie powiązania przez `apply` pozostają etapem 5.
Przykład: [`task-requirements.toml`](../examples/task-requirements.toml); demo offline używa
rzeczywistych kontroli Cargo i jawnie oznaczonych atrap ocen semantycznych.

**Weryfikacja lokalna etapu 4 (2026-10-25):** pełny zestaw na Python 3.14 wykonał
303 testy: 301 zaliczonych, 2 testy natywnych junctions Windows pominięte na Linux.
24 testy integracji odbioru oraz 19 konfiguracji i 13 CLI przeszły także na Python 3.11.
Sześć regresji Cargo używa rzeczywistych poleceń offline; nowa porównuje poprawne zakończenie
z zerem dopasowanych przypadków (kryterium `unmet`) i dokładny zaliczony test (`satisfied`).
Demo z plikiem kryteriów przeszło pełny gate offline. Ruff przez lokalny reviewdog, formatowanie
i kontrola diffu są poprawne. Niezależny przegląd etapu i całej gałęzi pozostaje do wykonania.
Nie uruchamiano płatnych sesji dostawców.

## 5. Raportowanie, integracja z apply i odbiór całości

Raport końcowy pokazuje osobno zakres gate, wykonane warianty, zmiany diagnostyki, dowody dla kryteriów oraz decyzje recenzentów i triage.

`apply` zachowuje obecną kontrolę całego oryginału, kandydata, backup i obsługę błędów. Dla nowych przebiegów dodatkowo sprawdza powiązanie końcowego raportu odbioru z kandydatem. Nie uruchamia ponownie modeli. Stare przebiegi pozostają obsługiwane według dotychczasowego kontraktu; niekompletny nowy przebieg nie może automatycznie przejść do trybu zgodności.

Końcowa weryfikacja obejmuje:

- istniejące regresje orkiestracji, niezależności review i `apply`;
- rzeczywiste małe projekty Cargo: workspace, doctesty, features i niedopasowany filtr testu;
- awarie narzędzi, przerwania, limity oraz zmianę źródeł podczas kontroli;
- migrację konfiguracji, stare raporty i schematy odpowiedzi;
- Ruff, lokalny reviewdog oraz testy platform, dla których deklarujemy wsparcie.

Sesje modeli pozostają zastąpione atrapami w testach automatycznych.

## Kolejność realizacji

Etap 1 → etap 2 → etapy 3 i 4, rozwijane równolegle po ustaleniu wspólnych identyfikatorów kontroli → etap 5.

Pierwszym wdrożeniem powinien być pełny gate z doctestami: daje konkretną poprawę zakresu weryfikacji przy najmniejszej ingerencji w obecną architekturę.
