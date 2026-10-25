# Rust PR review: konfiguracja dowodów

Ten przykład pokazuje projekt z granicą pamięci, zadaniami async i własną synchronizacją.
Dostosuj zakres do zadania: zwykły kod async nie wymaga automatycznie Loom, a kategorie
nieistotne dla zmiany trzeba uzasadnić podczas review. Adapter jest domyślnie wyłączony.
Nie instaluje Miri, Loom ani bibliotek testowych i nie dodaje nowego modelu lub etapu.

## 1. Zachowaj gate i dodaj testy

W swoim projekcie Rust utwórz konfigurację przez `bb-workflow init`. Zachowaj jej routing,
limity, `prepare` i istniejące kontrole. Do głównego `checks`, przed pierwszą sekcją TOML,
dopisz wymagane testy. Poniżej pełna lista z pięcioma domyślnymi kontrolami i trzema dodatkowymi:

```toml
checks = [
  ["cargo", "fmt", "--all", "--", "--check"],
  ["cargo", "check", "--workspace", "--all-targets", "--locked"],
  ["cargo", "test", "--workspace", "--all-targets", "--locked"],
  ["cargo", "test", "--workspace", "--doc", "--locked"],
  ["cargo", "clippy", "--workspace", "--all-targets", "--locked", "--", "-D", "warnings"],
  ["cargo", "+nightly", "miri", "test", "--locked", "--test", "memory_safety", "--", "arena::stable_reference", "--exact"],
  ["cargo", "test", "--locked", "--test", "async_lifecycle"],
  ["cargo", "test", "--locked", "--test", "loom_sync", "--", "model::handoff", "--exact"],
]
```

To przykład argumentów, nie gotowy zestaw testów Rust. Projekt musi dostarczać odpowiednie
testy integracyjne `memory_safety`, `async_lifecycle` i `loom_sync`, zależności oraz właściwą
konfigurację features. Można wskazać testy już istniejące lub wymagane od implementera;
muszą zostać wykonane dla końcowego kandydata. Dopasuj nazwy i argumenty do projektu.
Nie zastępuj własnego rozszerzonego gate tylko pięcioma kontrolami z przykładu.

Użyj domyślnego wariantu weryfikacji bez transformacji. Miri z jawnym `+nightly` oraz własne
argumenty testów nie są przekształcane przez mechanizm globalnych wariantów features/target/toolchain.
Jeśli projekt używa macierzy, zaplanuj wymagany zakres jawnymi komendami w `checks`;
nie usuwaj potrzebnych wariantów kontroli tylko po to, aby włączyć adapter.
Komendy nie korzystają z powłoki. Wrappery i nextest nie są obsługiwane jako dowód nazwanego testu.

## 2. Powiąż obowiązki z planem

Dopisz do `workflow.toml`:

```toml
[rust_review]
enabled = true
version = 1
required = ["memory", "deadlocks", "cancellation", "task_lifecycle", "loom"]

[[rust_review.evidence]]
kind = "memory"
check_id = "5"
test_name = "arena::stable_reference"

[[rust_review.evidence]]
kind = "deadlocks"
check_id = "6"
test_name = "lifecycle::no_deadlock"

[[rust_review.evidence]]
kind = "cancellation"
check_id = "6"
test_name = "lifecycle::cancel_releases_resources"

[[rust_review.evidence]]
kind = "task_lifecycle"
check_id = "6"
test_name = "lifecycle::worker_stops"

[[rust_review.evidence]]
kind = "loom"
check_id = "7"
test_name = "model::handoff"
```

Numery są ciągami znaków i odnoszą się do powyższej kolejności komend, licząc od zera.
Sprawdź je w `verification.gate_plan.checks` wyniku `plan`; po zmianie planu mogą być inne.
`test_name` to pełna nazwa z libtest, łącznie z modułami, a nie nazwa pliku lub fragment filtra.
Jedna kontrola może potwierdzić kilka nazwanych testów. Każdy wymagany rodzaj potrzebuje co
najmniej jednego wpisu `evidence`; dokładne duplikaty i nieznane rodzaje są odrzucane.

| Rodzaj | Wymagana kontrola | Dodatkowa ocena recenzenta |
| --- | --- | --- |
| `memory` | Bezpośrednie `cargo [+toolchain] miri test` | Własność, aliasowanie, unieważnianie wskaźników, kontrakty FFI i bezpieczni wywołujący |
| `deadlocks` | Bezpośrednie `cargo test` | Postęp, blokady i istotne przeploty |
| `cancellation` | Bezpośrednie `cargo test` | Anulowanie w punktach granicznych i zwalnianie zasobów |
| `task_lifecycle` | Bezpośrednie `cargo test` | Uruchamianie, kończenie i obserwowalne zatrzymanie zadań |
| `loom` | Bezpośrednie `cargo test` uruchamiające model | Śledzone prymitywy, odwzorowanie kodu, asercje, granice i zakończenie eksploracji |

## 3. Podaj własne kryteria zadania

Adapter wymaga niepustego pliku `--requirements` także z konfiguracją v1/v2. Przykładowy
`task.toml`, który należy dostosować do konkretnej zmiany:

```toml
requirements_version = 1

[[criteria]]
id = "shutdown-contract"
description = "Zatrzymanie kolejki kończy zadania i zachowuje ważność udostępnionych referencji zgodnie z publicznym API"
kind = "semantic"
verification = "Sprawdź kontrakt API, ścieżki zatrzymania i anulowania, wywołujących oraz testy regresji"
```

Nie dodawaj własnych identyfikatorów zaczynających się od `rust-review-`. Program zachowuje
kryteria zadania i dopisuje do nich kryteria specyfikacji, zakresu, pamięci, async, synchronizacji
oraz mechaniczne kryteria nazwanych testów. Całość wraz z polityką i pełnym planem zostaje
zamrożona przed implementacją.

Sprawdź konfigurację bez wywoływania modeli:

```sh
bb-workflow plan "Napraw zatrzymanie kolejki i dodaj testy regresji" --repo . --config workflow.toml --requirements task.toml
```

`plan` pokazuje rozszerzone `requirements`, `rust_review` i pełny `verification.gate_plan`.
Sprawdza powiązania komend, ale nie uruchamia testów ani nie potwierdza ich istnienia.
Właściwy przebieg korzysta z już skonfigurowanych dostawców i ich budżetów:

```sh
bb-workflow run "Napraw zatrzymanie kolejki i dodaj testy regresji" --repo . --config workflow.toml --requirements task.toml
```

## Co blokuje sukces i apply

Każdy wymagany test musi być zaliczony pod dokładną nazwą w pełnym, zakończonym wyniku libtest
dla bieżącego kandydata. Kod wyjścia zero, testy ignorowane, zero przypadków, `--no-run`,
`--list`, inna nazwa, brak logów lub nieaktualne powiązanie nie wystarczą. Triage nie może
zastąpić mechanicznego dowodu korzystną oceną. Po naprawie powtarza się cały gate i obie recenzje.

Zaliczenie Miri nie dowodzi wszystkich kontraktów FFI ani prawdziwości argumentu aliasowania.
Zaliczenie nazwanego testu Loom nie potwierdza samo w sobie, że model odpowiada rzeczywistej
implementacji i zakończył właściwą eksplorację. Obaj recenzenci niezależnie oceniają te kwestie.
`unresolved` od choć jednego z nich dla kryterium zakresu, pamięci, async lub synchronizacji
pozostaje blokadą nawet po korzystnym triage. Zwykłe spory o specyfikację są nadal rozstrzygane
przez triage. Samo `findings=[]` lub zapewnienie w `summary` nie zastępuje ocen kryteriów.

Gdy przegląd ujawni obowiązek nieobecny w zamrożonej polityce lub planie, recenzent zwraca
`blocked`. Uzupełnij konfigurację i rozpocznij nowy przebieg; edycja pliku w trakcie `run`
nie rozszerza już przyjętych wymagań. Końcowy raport zachowuje politykę i dowody. Jawne `apply`
odtwarza wygenerowane kryteria i ponownie ocenia zapisane logi oraz nierozstrzygnięte argumenty,
bez wywoływania modeli lub Cargo. Brak wymaganych artefaktów nie pozwala zastosować wyniku.

Adapter jest wbudowany i automatycznie dołączany do obu recenzentów oraz triage. Własne
projektowe `skills` są dodatkiem; nie trzeba instalować upstreamowej umiejętności ani jej
hooków. Metoda pochodzi z [rust-pr-review w rewizji 268582b5](https://github.com/rtmachines/rtm-coding/tree/268582b5c4d8f91bc709294b9ebbe270a181f9b2/skills/rust-pr-review).
Pełna baza reguł nie jest pobierana; udostępnij tylko potrzebne reguły przez istniejącą
konfigurację skills. Brak LSP/MCP jest ujawnianym ograniczeniem, sam w sobie nie jest błędem
programu. Recenzenci mogą korzystać ze źródeł, testów i pełnych logów dostarczonych przez workflow.
