# bb-rust-workflow

Lokalny orkiestrator dla zmian w projektach Rust. Uruchamia Claude Code do implementacji, dwa niezależne przeglądy kodu (domyślnie Claude Code i Codex), triage ustaleń oraz ograniczoną liczbę poprawek. Kontrolę etapów, testy i decyzję o zakończeniu podejmuje program, nie odpowiedź agenta. Każdy przebieg pracuje na kopii projektu w `.bb-workflow/runs/<id>/`; **`run` nigdy sam nie przenosi zmian do oryginału**.

Wymaga Python **3.11+** i Git. Program w czasie działania korzysta wyłącznie ze standardowej biblioteki Pythona. Przed użyciem zainstaluj i uwierzytelnij używane przez konfigurację CLI `claude` i `codex` oraz zapewnij `cargo` i komponenty wymagane przez wybrane kontrole jakości. Każda kopia ma własne repozytorium Git z zapisanym stanem początkowym; metadane Git oryginału nie są kopiowane. `doctor` sprawdza dostępność i potrzebne flagi CLI bez uruchamiania sesji modelu. Orkiestrator uruchamia procesy bez powłoki; Bash nie jest jego wymaganiem. Wymagania instalacyjne samego Claude Code, zwłaszcza na Windows, sprawdź w jego oficjalnej dokumentacji.

## Instalacja

W katalogu tego projektu:

```sh
python3 -m pip install -e .
```

Na Windows w PowerShell:

```powershell
py -3.11 -m pip install -e .
```

Po instalacji używaj `bb-workflow` albo `python -m bb_workflow`. Układ `src/` oznacza, że samo uruchomienie `python -m bb_workflow` bez instalacji pakietu nie wystarczy.

## Pierwszy przebieg

Z katalogu repozytorium Rust:

```sh
bb-workflow init
bb-workflow doctor --repo .
bb-workflow plan "Dodaj obsługę błędu odczytu konfiguracji" --repo .
bb-workflow run "Dodaj obsługę błędu odczytu konfiguracji" --repo . --requirements task.toml
```

Na Windows te same polecenia działają w PowerShell, o ile instalator dodał `bb-workflow` do `PATH`; w przeciwnym razie użyj `python -m bb_workflow`. `init` tworzy edytowalny `workflow.toml` (lub ścieżkę podaną przez `--config`). `plan` pokazuje wersję konfiguracji, tryb zgodności, zakres kontroli, dokładne komendy i przypisanie modeli bez uruchamiania agentów. `doctor` pozwala sprawdzić zależności przed kosztownym `run`. Dla `plan` i `run` można podać `--complexity auto|low|medium|high`; `auto` jest prostą heurystyką słów w zadaniu, więc w razie potrzeby wybierz poziom jawnie. `--repo` wskazuje projekt Rust, a `--config` pozwala podać inny plik; bez niego program szuka `workflow.toml` w repozytorium.

Nowy szablon i `Config()` używają `config_version = 3`: `run` wymaga niepustego pliku kryteriów
`--requirements task.toml`. Przygotuj go dla zadania przed uruchomieniem. `plan` bez pliku nadal
działa i pokazuje `requirements_needed`; `doctor` i `verify` nie wymagają kryteriów.
Przykład minimalnego kryterium semantycznego:

```toml
requirements_version = 1
[[criteria]]
id = "error-handling"
description = "Błąd odczytu konfiguracji jest zwracany wywołującemu bez paniki"
kind = "semantic"
verification = "Obaj recenzenci sprawdzają ścieżkę błędu, wywołania i testy"
```

Pełny przykład mechaniczny i semantyczny jest w
[`examples/task-requirements.toml`](examples/task-requirements.toml).
`bb-workflow plan "Zadanie" --requirements task.toml` sprawdza powiązanie z pełnym gate,
bez wywołań modeli. Ścieżka pliku wymagań jest względem katalogu polecenia; `--repo` jej nie zmienia.

Po `run` zanotuj wypisaną ścieżkę katalogu przebiegu. Obejrzyj wynik, logi i zmiany:

```sh
bb-workflow status .bb-workflow/runs/<id>
bb-workflow status .bb-workflow/runs/<id> --summary
```

`status` zwraca JSON. Czytelne podsumowanie i pełne ślady znajdują się także w `report.md` i `state.json` wewnątrz katalogu przebiegu. Jeśli status to `succeeded` i akceptujesz wynik, zastosuj go jawnie:

```sh
bb-workflow apply .bb-workflow/runs/<id>
```

`apply` odmawia działania, gdy od utworzenia kopii **zmienił się dowolny plik oryginalnego projektu objęty migawką**, gdy zmieniła się zatwierdzona kopia kandydata albo przebieg nie zakończył się sukcesem. Obejmuje to także prawa plików. Program zachowuje kopię zapasową zmian w katalogu przebiegu; przy błędzie próbuje wycofać częściowe zastosowanie. Nie ma automatycznego scalania konfliktów. `status` i raport pozwalają przejrzeć rezultat bez modyfikowania oryginału.

Wycofywanie obejmuje wykonane operacje i sprawdza, czy plik nadal zawiera wersję zapisaną przez workflow. Późniejsze edycje użytkownika pozostają zachowane; konflikt oznacza niepełne wycofanie, opisane w `apply.json` wraz ze ścieżką kopii zapasowej. Blokada nie koordynuje zewnętrznych edytorów, a przenośne operacje plikowe nie zapewniają atomowego porównania i zastąpienia: należy unikać edycji podczas samego `apply`.

## Konfiguracja i limity

Domyślny szablon: [`src/bb_workflow/default.toml`](src/bb_workflow/default.toml). Zawiera profile `low`, `medium`, `high` dla ról `implementation`, `reviewer_a`, `reviewer_b`, `triage` i `fixer`, a także opcjonalne nadpisania `[roles.*]`. Wbudowane identyfikatory Claude Haiku 5.5, Sonnet 5.5, Opus 5.5 oraz Codex `gpt-6-luna` i `gpt-6.1-sol` są **przypiętymi przykładami**, nie obietnicą najnowszego modelu lub dostępu na Twoim koncie. Przed uruchomieniem sprawdź je w swoich CLI; katalog dozwolonych modeli można rozszerzyć w `[model_catalog.*]`.

| Złożoność | Implementacja, reviewer A, triage, poprawki | Reviewer B |
| --- | --- | --- |
| low | Claude Haiku 5.5, effort low | Codex GPT-6 Luna, low |
| medium | Claude Sonnet 5.5, effort medium | Codex GPT-6.1 Sol, medium |
| high | Claude Opus 5.5, effort high | Codex GPT-6.1 Sol, high |

Dobór jest punktem startowym, nie wynikiem benchmarku. Wysokie ryzyko (np. `unsafe`, FFI, współbieżność, uwierzytelnianie) podnosi domyślny poziom. Jawny poziom i nadpisania ról mają pierwszeństwo. Przykładowe nadpisanie na końcu wygenerowanego pliku:

```toml
[roles.reviewer_a]
backend = "claude"
model = "claude-sonnet-5-5"
effort = "high"
skills = [".agents/skills/rust-review/SKILL.md"] # Plik musi istnieć w projekcie.
# tools = ["LSP"] # Tylko jeśli masz skonfigurowaną integrację LSP w Claude Code.
```

Program nie przełącza modelu ani backendu po błędzie. Identyfikatory i effort sprawdzono w [dokumentacji Claude Code](https://code.claude.com/docs/en/model-config) oraz lokalnym katalogu modeli Codex. Zgodność katalogu nie gwarantuje dostępu na koncie. Jeżeli backend zgłosi inny model, przebieg kończy się błędem; jeżeli nie zwróci identyfikatora, raport zapisze `observed_model: null`.

### Oszczędzanie kontekstu Claude Code i Codex

Domyślnie prompt zawiera krótkie wyniki udanych kontroli, końcówki błędów i ograniczony fragment diffu. Pełne logi pozostają w `checks/`; agent dostaje ścieżkę do nich. Schemat odpowiedzi jest przekazywany natywnie przez CLI, bez ponownego wklejania do promptu. Dwa niezależne przeglądy i obowiązkowe kontrole pozostają częścią każdego przebiegu. Triage nadal uruchamia się tylko wtedy, gdy recenzenci zgłoszą ustalenia.

Nowe i starsze konfiguracje bez sekcji `[context]` korzystają z tych ustawień:

```toml
[context]
max_diff_chars = 16000
failed_check_tail_chars = 2000
skill_mode = "reference"
```

`max_diff_chars` ogranicza fragment diffu w znakach, z dodatkowym krótkim komunikatem o pominięciu. `failed_check_tail_chars` dotyczy osobno stdout i stderr każdej nieudanej kontroli. Wartość `0` zostawia odsyłacze do danych bez danego fragmentu. Pełne `changed_paths`, katalog kandydata i `baseline_directory` pozwalają sprawdzić pominięte zmiany; agent ma rozszerzać odczyt, gdy potrzebuje dowodów.

`skill_mode = "reference"` przekazuje ścieżki do wybranych `skills`; agent ma przeczytać te pliki przed pracą i sięgać do ich zasobów według potrzeby. `"inline"` przywraca wklejanie ich treści. Odsyłacz zmniejsza początkowy prompt, ale późniejszy odczyt również zużywa tokeny. Wybieraj umiejętności dla konkretnej roli zamiast całego katalogu.

`state.json` zachowuje surowe `agents[].usage`, znormalizowane `agents[].token_usage`, długość promptu (`prompt_chars`, `prompt_bytes`) oraz sumy według backendu w `token_usage.by_backend`. Raport pokazuje zmierzone sumy i liczbę wywołań z pełnym, częściowym albo nieznanym pomiarem. Cache jest liczony zgodnie z formatem danego dostawcy, bez podwójnego dodawania do wejścia. Brak pomiaru to `null`, nie zero. Długość promptu nie obejmuje automatycznych instrukcji CLI, narzędzi, późniejszych odczytów ani rozumowania modelu.

Poprawne pomiary zwrócone razem z błędem dostawcy również trafiają do raportu; nie oznaczają udanej odpowiedzi i mogą obejmować tylko część rzeczywistego zużycia. Raport nie gwarantuje pełnego rozliczenia dostawcy. Przerwanie lub nieoczekiwany błąd bez pomiaru zwiększa liczbę wywołań o nieznanym koszcie.

Do częstego sprawdzania przebiegu z agenta używaj `status --summary`: zwraca postęp, koszt i liczniki bez całej historii kontroli. Dotychczasowy `status` nadal zwraca pełny stan. Przy pracy nad samym orkiestratorem Codex korzysta z krótkiego `AGENTS.md`, a Claude Code importuje go przez `CLAUDE.md`.

Podejście jest zgodne z zaleceniami [Claude Code o ograniczaniu kontekstu](https://code.claude.com/docs/en/costs) i [OpenAI o ładowaniu umiejętności według potrzeby](https://learn.chatgpt.com/docs/build-skills). [Pomiar zmian](docs/token-efficiency.md) rozróżnia wielkość promptów od rzeczywistego zużycia modeli.

Dotyczy to również zagnieżdżonego komunikatu Codex `model rerouted`. Plik `agents/<wywołanie>/invocation.json` zachowuje model żądany, zaobserwowane modele i przekierowania także przy nieudanym wywołaniu. Zwykłe ostrzeżenia CLI nie są traktowane jako przekierowania.

Pole `prepare` zawiera polecenia, które mogą zmieniać pliki przed kontrolami. Szablon tworzony przez `init` zawiera `cargo check --workspace --all-targets`: przygotowuje `Cargo.lock` i pliki generowane przez budowanie przed zamrożeniem źródeł. Etap działa przed kontrolami bazowymi i po każdej implementacji lub naprawie. Pole pominięte w dowolnej konfiguracji oznacza pustą listę; tak samo `Config()` zachowuje pustą listę, aby utrzymać dotychczasowe zachowanie API. Można też jawnie ustawić `prepare = []`.

Po `prepare` program zapamiętuje identyfikator źródeł. Dla v2 i v3 brak `checks` oznacza domyślnie `cargo fmt --all -- --check`, `cargo check --workspace --all-targets --locked`, `cargo test --workspace --all-targets --locked`, osobne `cargo test --workspace --doc --locked` oraz `cargo clippy --workspace --all-targets --locked -- -D warnings`. Plik bez `config_version` oznacza v1; jawna wersja 1 także jest obsługiwana. W v1 brak `checks` zachowuje dawny fallback: `cargo fmt --check`, `cargo check --all-targets`, `cargo test --all-targets`, `cargo clippy --all-targets -- -D warnings` (bez `--locked`). Historyczne jawne polecenia `checks` z szablonu zawierają `--locked` i pozostają niezmienione. Jawnie podane `checks` pozostają bez zmian niezależnie od wersji; nieznana wersja konfiguracji jest odrzucana.

Aby przejść na v3 z kryteriami, ustaw `config_version = 3` i podaj plik przez `--requirements`. Aby jednocześnie przyjąć pełne domyślne kontrole, świadomie zastąp albo usuń własne `checks`. Samo podbicie wersji zachowuje jawne polecenia. Nie włączaj automatycznie `--all-features`; wybierz konfiguracje features zgodnie ze wspieranymi wariantami projektu. Wyniki oraz recenzje odnoszą się do tej samej wersji plików. **Zmiana źródeł podczas kontroli unieważnia wyniki i zatrzymuje przebieg przed recenzjami**; generatory i formatowanie zapisujące pliki należy przenieść do `prepare`. Wykluczone artefakty budowania mogą się zmieniać. Polecenia w `prepare` i `checks` to listy argumentów, bez składni powłoki. Kontrole bazowe pokazują zastane problemy; **przejście kandydata przez kontrole jest obowiązkowe**. Zaufaj tylko komendom z własnego `workflow.toml`: są uruchamiane z uprawnieniami użytkownika.

### Kryteria odbioru i zgodność

Pliki v1/v2 bez `--requirements` zachowują dotychczasowy protokół i jawny tryb `legacy`.
Nie zapewniają odbioru kryteriów. Podanie pliku wymagań w v1/v2 włącza ten sam protokół v2
co v3; błędny plik, odpowiedź lub powiązanie zatrzymuje przebieg bez powrotu do trybu zgodności.
Jawne `checks` pozostają niezmienione we wszystkich trzech wersjach. Brak wersji oznacza v1.

Każde kryterium ma unikalne `id`, `description`, `kind` i niepusty opis `verification`.
Mechaniczne wymaga `check_id` z `verification.gate_plan.checks` w `plan`; opcjonalne `test_name`
wskazuje dokładną nazwę testu Cargo. Semantyczne nie przyjmuje `check_id` ani `test_name`.
Kontrola mechaniczna nie zawęża gate. Dla bezpośredniego `cargo test` także kryterium bez nazwy
wymaga co najmniej jednego rzeczywiście zaliczonego, nieignorowanego testu. Zero przypadków,
`--no-run` i `--list` nie stanowią dowodu; nieznany format wyniku blokuje odbiór. Dla własnych
poleceń nietestowych dowodem jest poprawne, niezmieniające źródeł zakończenie zaufanej komendy.
Pełne stdout/stderr są zachowane i służą do oceny testów, nawet gdy kontekst pokazuje tylko skrót.

Program zapisuje znormalizowane `requirements.json` i `verification_plan.json` przed implementacją.
Zamraża konfigurację oraz pełny plan; edycja pliku kryteriów lub konfiguracji w trakcie przebiegu
nie zmienia przyjętego kontraktu. Każdy wynik ma powiązanie `candidate_id`, `requirements_id`,
`verification_plan_id`. Implementer i Fixer podają wyłącznie propozycje dowodów dla wejściowej
migawki. Obaj niezależni recenzenci oceniają wszystkie kryteria semantyczne. Każda ocena inna niż
obustronne `satisfied` trafia do triage, również bez ustaleń kodowych. Nierozstrzygnięte kryterium
blokuje sukces, a potwierdzone niespełnienie kieruje do ograniczonego Fixera niezależnie od
`blocking_severities`. Po naprawie cały gate i oba review powtarzają się dla nowego kandydata.
Triage nie może nadpisać mechanicznej porażki.

`state.json` zawiera `acceptance.mode`, hashe i `acceptance.rounds` z wynikami mechanicznymi,
niezależnymi ocenami i triage. Raport i `status --summary` ujawniają tryb oraz hashe. Rozbudowany
raport końcowy i dodatkowa kontrola powiązania odbioru przez `apply` należą do etapu 5;
obecne jawne `apply` zachowuje dotychczasowe kontrole manifestów.

### Weryfikacja bez agentów

Polecenie `verify` uruchamia kontrole bez `prepare`, wyboru modeli ani wywołań dostawców. Działa w podanym repozytorium źródłowym, pod blokadą projektu, i sprawdza manifest źródeł przed oraz po kontrolach. Zmianę źródeł wykrywa i raportuje, ale jej nie wycofuje. Weryfikacja nie tworzy migawek kandydata i nigdy nie uprawnia do `apply`.

```sh
bb-workflow verify fast --repo .
bb-workflow verify focused --repo . --package recipe-core --test-filter upper_boundary
bb-workflow verify lint --repo . --config workflow.toml --package recipe-core
bb-workflow verify gate --repo .
```

Każdy profil przyjmuje `--repo` i opcjonalne `--config`. Szybkie profile `fast`, `focused` i `lint` przyjmują opcjonalne `--package`; tylko `focused` obsługuje `--test-filter`. `gate` odrzuca oba zawężenia i wykonuje wszystkie skonfigurowane warianty, zachowując kolejność poleceń z głównego `checks`. Nie ma nadpisania `checks` wewnątrz profilu `gate`.

Profile weryfikacji znajdują się w `[verification.profiles.*]`, niezależnie od `[profiles.low|medium|high]`, które nadal wybierają modele. Bez dodatkowej konfiguracji `fast` wykonuje formatowanie i `cargo check`, `focused` — `cargo check` i testy, `lint` — Clippy, a `gate` — główny zestaw `checks`. Można skonfigurować warianty macierzy gate:

```toml
checks = [["cargo", "check", "--workspace", "--locked"]]

[verification.profiles.gate]
variants = ["default", "no-defaults-linux"]

[verification.variants.default]

[verification.variants.no-defaults-linux]
features = ["serde"]
no_default_features = true
target = "x86_64-unknown-linux-gnu" # Opcjonalnie.
toolchain = "stable" # Opcjonalnie.
```

`checks` na poziomie głównym pozostaje jedynym źródłem poleceń gate; każdy wariant stosuje się do wszystkich tych poleceń. Wariant domyślny nie zmienia argumentów. `features`, `all_features`, `no_default_features`, `target` i `toolchain` są jawne; program nie włącza automatycznie wszystkich features. Jeśli zawężenie lub wariant wymagałby niejednoznacznej transformacji własnego `argv` — na przykład polecenia powłoki albo sprzecznych flag Cargo — konfiguracja jest odrzucana z błędem zamiast po cichu zmieniać zakres.

`verify` używa istniejącego `Cargo.lock`; nie tworzy go ani nie przygotowuje źródeł. Przy `--locked` przygotuj wcześniej pliki źródłowe i lockfile, np. przez `prepare` w zwykłym przebiegu albo ręcznie. Wyniki są zapisywane w `.bb-workflow/verifications/<id>/report.json` i `report.md`, razem z logami poleceń. Poprawny wynik ma status `passed` oraz `apply_eligible = false`. Kody wyjścia: `0` — wszystkie kontrole zaliczone, `1` — kontrola nieudana, zablokowana lub wyczerpany budżet, `130` — przerwanie, `2` — błędna konfiguracja lub użycie. Zwykłe `run` nadal przeprowadza pełny gate przed recenzjami każdego kandydata.

Domyślne limity to `max_cycles = 3` (dokładnie maksymalna liczba wywołań Fixera), `max_agent_calls = 20`, `timeout_seconds = 900` na proces i `max_seconds = 3600` jako budżet uruchamiania etapów. Pozostały czas ogranicza timeout kolejnego procesu. Kopiowanie i hashowanie plików oraz sprzątanie procesów nie są przerywane w środku operacji, więc czas zakończenia może przekroczyć ten budżet. `max_retries` dotyczy tylko odczytowych ról recenzentów/triage; zapisy implementera i Fixera nie są automatycznie ponawiane. Nierozstrzygnięte ustalenie blokuje sukces. Koszt raportowany przez Claude jest sumowany, lecz Codex może nie zwracać kwoty USD; łączny koszt może być **nieznany**. Limity czasu i wywołań nie są limitem pieniężnym.

Ponowienia pomijają błędy wymagające zmiany lokalnej konfiguracji lub środowiska: nieprawidłowe wywołanie, brak programu, zmianę modelu oraz przekroczenie limitu wyjścia. Każda faktycznie podjęta próba pozostaje osobnym wpisem w audycie i zużywa limit wywołań. Błędy dostawcy i parsowania jego odpowiedzi mogą być ponowione w ramach `max_retries`, wyłącznie dla ról odczytowych i po sprawdzeniu niezmienności ich kopii. Osobna walidacja protokołu workflow nadal zatrzymuje przebieg przy niepoprawnej odpowiedzi.

Role Claude dostają narzędzia `Read`, `Glob`, `Grep`; implementer/Fixer także `Edit`, `Write`. `LSP` można dodać jawnie w `tools`. W `mcp_config` można wskazać konfigurację MCP tylko dla Claude. Do `tools` dodaj dokładne nazwy narzędzi MCP zwrócone przez swoją integrację (format `mcp__serwer__narzędzie`, bez wildcardów); zostaną dopuszczone do wykonania bez pytania. Używaj wyłącznie zaufanych narzędzi odczytowych dla recenzentów i triage. Sam plik `mcp_config` nie dopuszcza automatycznie wszystkich narzędzi serwera. Codex jest uruchamiany bez dziedziczenia konfiguracji użytkownika; ten MVP nie obsługuje dla niego dodatkowego MCP. Gdy LSP nie jest dostępne, prompt kieruje agenta do źródeł, testów i wyników `cargo` oraz wymaga ujawnienia ograniczenia. Pliki umiejętności wskazane w `skills` muszą być rzeczywistymi plikami wewnątrz projektu, na przykład `.agents/skills/.../SKILL.md`; dowiązania symboliczne są odrzucane.

Przed pierwszym wywołaniem modelu workflow sprawdza obecność skonfigurowanych skills w początkowej kopii oraz plików MCP wszystkich wybranych ról, także warunkowych `triage` i `fixer`. Zachowuje też kontrole przed użyciem danej roli. Ten krok nie sprawdza uwierzytelnienia CLI ani dostępu do modeli.

## Ograniczenia i odzyskiwanie

`limits.max_output_bytes` ogranicza sumę surowych bajtów stdout i stderr jednego procesu; domyślnie wynosi 16 MiB. Przekroczenie zatrzymuje proces, zapisuje dostępny początek logu i oznacza niepowodzenie, także przy kodzie wyjścia zero. Niepełne odpowiedzi agentów nie są parsowane jako sukces. Odczyt końcowego pliku JSON Codex mieści się w pozostałej części tego limitu. Limit dotyczy przechwytywanego wyjścia, nie wszystkich plików tworzonych przez agentów lub Cargo. Odczyt odbywa się na bieżąco i nie czeka na zamknięcie odziedziczonych uchwytów potomków.

Na POSIX sprzątanie kończy własną grupę procesów; proces, który celowo utworzy inną sesję, może ją opuścić. Na Windows używany jest Job Object. Odrzucane są także punkty ponownej analizy systemu plików, w tym junctions, aby kopiowanie i `apply` nie przechodziły do katalogu poza projektem.

Kopie robocze zapewniają izolację zmian funkcjonalnych, **nie są piaskownicą bezpieczeństwa hosta**. Uruchamiaj workflow na zaufanym repozytorium, z zaufanymi komendami kontroli i serwerami MCP. Metadane `.git`, `.bb-workflow` i `.code-review-graph` zawsze pozostają wykluczone. Domyślne wykluczenia `target`, `build`, `dist` i środowisk Python dotyczą katalogu głównego, więc moduły `src/build`, `src/target` i `src/dist` są zachowywane. `__pycache__` jest pomijany na każdym poziomie. W `exclude` początkowy `/` zakotwicza wzorzec w katalogu głównym, a `!` przywraca pasujące pliki; np. `exclude = ["!/build", "/custom-target"]`. Wzorce są rozpatrywane po kolei, po domyślnych wykluczeniach; obowiązkowych metadanych nie można przywrócić. Niestandardowy katalog wynikowy Cargo należy jawnie wykluczyć. Niewykluczone dowiązania symboliczne i pliki specjalne powodują błąd, zamiast być kopiowane. Logi i prompty pozostają w katalogu przebiegu i mogą zawierać fragmenty źródeł lub sekretów widocznych dla agentów — chroń ten katalog.

Przerwanego przebiegu nie da się automatycznie wznowić. Zachowaj jego `state.json`, raport i logi do ręcznej analizy; kolejny `run` tworzy nową migawkę. Jeśli pozostanie `.bb-workflow/lock`, **najpierw sprawdź, że żaden proces workflow nie działa**, a dopiero potem usuń plik blokady ręcznie. Recenzenci dostają oddzielne kopie tego samego kandydata i oddzielne procesy, wywoływane kolejno w celu kontroli budżetu. To nie jest pełna izolacja całego systemu.

Jeśli projekt zawiera dowiązania w `.claude/skills`, można dodać `exclude = [".claude/skills"]` i wskazać rzeczywiste pliki z `.agents/skills` w konfiguracji ról. Katalogi wykluczone nie są przenoszone do kopii ani objęte porównaniem przy `apply`; wymagane do budowania zasoby muszą znaleźć się w migawce. MVP nie obsługuje zmiany pliku w katalog lub odwrotnie podczas `apply`. Kopie zachowują puste katalogi, lecz `apply` synchronizuje wyłącznie pliki; zmiany samych pustych katalogów nie są przenoszone.

## Testy i przykład

Z katalogu projektu, bez instalowania pakietu (Linux/macOS):

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -q
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_backends.py' -q
ruff check src tests examples
ruff format --check src tests examples
PYTHONPATH=src python3 examples/demo.py
```

W PowerShell ustaw najpierw `$env:PYTHONPATH = "src"`, a następnie użyj `python -m unittest ...`. Po instalacji edytowalnej `PYTHONPATH` nie jest potrzebne. Testy integracji używają atrap dostawców; testy procesów i snapshotów mogą uruchamiać lokalne programy, np. Python i Git. Nie wymagają kont Claude Code ani Codex. Test naprawionego zachowania dodaj w odpowiadającym module `tests/test_<module>.py`, wraz z istotnym przypadkiem błędu.

Zakres odczytu dobieraj według zmiany: `backends.py` odpowiada za wywołania CLI i format wyników, `process.py` za limity i sprzątanie procesów, `engine.py` za etapy, prompty i audyt, `protocol.py` za walidację odpowiedzi oraz triage, `workspace.py` za snapshoty i `apply`. Konfiguracja i routing są w `config.py`, pomiary w `usage.py`, polecenia w `cli.py`. Zmiany promptów wymagają także sprawdzenia `src/bb_workflow/prompts/` i testów kontekstu w `test_engine.py`. Przy zmianach etapów, snapshotów lub `apply` przeczytaj `docs/architecture.md`.

Jeśli masz reviewdog, zbierz diagnostykę Ruff lokalnie z katalogu projektu (Bash):

```sh
set -o pipefail
NO_COLOR=1 ruff check src tests examples --output-format=concise 2>&1 | reviewdog -f=flake8 -reporter=local -filter-mode=nofilter -fail-level=warning
```

Do reviewdog przekazuj surowe wyjście lintera; jeśli przechwytuje je RTK, uruchom producenta jako `rtk bypass ruff check ...`. Błąd wykonania lintera raportuj osobno od ustaleń. Reviewdog nie zastępuje testów ani kontroli formatowania.

`demo.py` używa **atrap agentów i prawdziwych kontroli Cargo**. Prawdziwy przebieg na małym przykładzie (uruchamia modele i zużywa ich limity):

```sh
bb-workflow run "Make increment saturate at u32::MAX and add a regression test" --repo examples/rust-counter --config examples/live.toml --complexity low
```

Przykład zachowuje kod wejściowy; wynik jest w katalogu przebiegu. [Raport weryfikacji](docs/validation.md) rozróżnia testy automatyczne, rzeczywiste sesje modeli i niezweryfikowane platformy. [Macierz CI](.github/workflows/tests.yml) obejmuje Ubuntu/Windows oraz Python 3.11/3.13; nie uruchomiono jej w zdalnym CI w tej sesji.

Więcej o etapach i stanach: [architektura](docs/architecture.md). Porównanie inspiracji i alternatyw: [badanie projektów](docs/research.md).
