# Weryfikacja MVP — 2026-10-25

Poniższe wyniki opisują wcześniejszy etap MVP. Późniejszą optymalizację kontekstu Claude/Codex i jej oddzielne pomiary opisuje [raport zużycia kontekstu](token-efficiency.md); nie należy traktować wcześniejszych sesji modeli jako walidacji nowych promptów.

Raport po wdrożeniu trzech etapów poprawek: ochrona `apply`, spójność kontroli i kopii roboczych, odporność adapterów oraz procesów.

## Wyniki końcowe

| Kontrola | Wynik |
| --- | --- |
| Pełny zestaw, Python 3.11.17 / Linux | 168 testów: 166 zaliczonych, 2 pominięte |
| Pełny zestaw, Python 3.13.17 / Linux | 168 testów: 166 zaliczonych, 2 pominięte |
| Pełny zestaw, Python 3.14.8 / Linux | 168 testów: 166 zaliczonych, 2 pominięte |
| Ruff przez reviewdog, lokalny reporter, `nofilter` | Brak diagnostyk |
| Formatowanie Ruff | Zgodne |
| Budowa wheel i sdist | Zakończona poprawnie |
| Instalacja wheel w osobnym środowisku Python 3.11 | `bb-workflow plan` działa poza katalogiem źródeł |
| Testy adapterów z rozpakowanego sdist | 21 testów zaliczonych; pliki kontraktów CLI obecne |
| `doctor` zainstalowanych narzędzi | Claude Code 2.1.308, Codex 0.164.0, Git 2.56.0, Cargo 1.99.1 dostępne |
| Przykład z atrapami agentów i prawdziwym Cargo | `succeeded` |
| Tymczasowy projekt: workflow → apply → ponowne kontrole Cargo | Zakończony poprawnie |
| Rzeczywiste sesje Claude Code + Codex | `succeeded`, 3 wywołania |

Dwa pominięte testy wymagają natywnego Windows i sprawdzają junctions. Pozostałe testy ochrony przed punktami ponownej analizy działają także na Linuxie przez kontrolowane dane `lstat`. Testy Windows API dla procesów wykorzystują atrapy; nie są dowodem natywnej zgodności.

## Regresje objęte testami

- Wycofanie `apply` zachowuje edycję użytkownika przed nieudanym zastąpieniem, po udanym zapisie i po odtworzeniu usuniętego pliku. Pozostałe operacje są wycofywane, a konflikt wskazuje kopię zapasową.
- Każda ścieżka jest sprawdzana przed zastosowaniem. Zmiana kandydata podczas kopiowania zostaje wykryta w pliku tymczasowym, zanim trafi do oryginału.
- Kontrola zmieniająca źródła nie może zatwierdzić innej wersji niż sprawdzona wcześniej. Generowanie w `prepare` jest dozwolone; kontrole i recenzje wskazują ten sam identyfikator kandydata.
- Kopie mają własne repozytoria Git. Rzeczywiste testy sprawdzają `rev-parse`, diff, zachowanie plików niezatwierdzonych, nieśledzonych i ignorowanych przez Git oraz nienaruszenie metadanych oryginału.
- Moduły Rust `src/build`, `src/target` i `src/dist` są kopiowane i kompilują się. Testy obejmują także wzorce zakotwiczone i ponowne dołączanie plików.
- Komunikaty przekierowania modelu Codex są odrzucane; zwykłe ostrzeżenia pozostają dopuszczalne. Wersjonowane przykłady kontraktów Claude 2.1.308 i Codex 0.164.0 są zapisane w `tests/fixtures/backends/`; są znormalizowanymi przykładami, a nie zapisem prawdziwej sesji.
- Limity wyjścia obejmują wspólny budżet stdout/stderr, dokładną granicę, UTF-8, szybkie i ciągłe generowanie wyjścia oraz końcowy plik JSON Codex. Przekroczenie nie jest sukcesem nawet przy kodzie zero.
- Przerwanie, timeout, błąd przygotowania przechwytywania i odziedziczone uchwyty nie blokują zakończenia. Test potomka POSIX z osobną sesją sam sprząta ten proces; gwarancja kończenia potomków nadal dotyczy własnej grupy.
- `doctor --repo` używa wskazanego katalogu dla względnych programów i sprawdza również Git oraz narzędzia etapu `prepare`.

## Rzeczywisty przebieg po poprawkach

Przebieg `6d019aec9c7c4892843d912da2229856`: **succeeded**, 26,916 s, trzy wywołania, bez Fixera.
[Raport przebiegu](../examples/rust-counter/.bb-workflow/runs/6d019aec9c7c4892843d912da2229856/report.md) jest lokalnym artefaktem pomijanym w dystrybucji.

| Rola | Model żądany | Model zgłoszony |
| --- | --- | --- |
| Implementacja | Claude Haiku 5.5, low | Claude Haiku 5.5 |
| Reviewer A | Claude Haiku 5.5, low | Claude Haiku 5.5 |
| Reviewer B | GPT-6 Luna, low | Brak identyfikatora w odpowiedzi CLI |

Agent zastosował `saturating_add` i dodał test granicy `u32::MAX`. Przeszły przygotowanie oraz wszystkie kontrole bazowe i końcowe: formatowanie, kompilacja, testy i Clippy. Końcowe kontrole używały `--locked`. Oryginalny projekt pozostał zgodny z manifestem początkowym, a identyfikatory wszystkich kontroli kandydata zgadzały się z zaakceptowaną wersją.

Claude zgłosił 0,00431994 USD; koszt Codex pozostał nieznany. Brak zgłoszonego identyfikatora modelu Codex jest zapisany jako `observed_model: null`, bez domniemanego potwierdzenia.

Przykład bez sesji modeli, ale z prawdziwym Cargo: `f0af7d2b22ce4fdfa75bab2bc5481548`, **succeeded**. Osobny tymczasowy projekt przeszedł pełne `apply`, w tym dodanie `Cargo.lock`, a następnie wszystkie cztery kontrole Cargo. Oryginalny przykład nie był zmieniany przez `apply`.

## Powtórzenie kontroli

Po instalacji pakietu:

```sh
python -m unittest discover -s tests -v
ruff check src tests examples
ruff format --check src tests examples
python examples/demo.py
bb-workflow doctor --repo examples/rust-counter --config examples/live.toml
```

Rzeczywiste sesje modeli zużywają limity kont:

```sh
bb-workflow run "Make increment saturate at u32::MAX and add a regression test" --repo examples/rust-counter --config examples/live.toml --complexity low
```

## Granice sprawdzenia

- Natywny Windows i zdalna macierz GitHub Actions nie były dostępne w tej sesji. CI obejmuje Ubuntu/Windows i Python 3.11/3.13, pełny zestaw testów, lint, formatowanie i przykład Cargo. Dodane testy natywnych junctions zostaną wykonane na Windows.
- Rzeczywisty przebieg nie wymagał triage ani Fixera; te ścieżki sprawdzono testami z atrapami. Nie uruchamiano wszystkich modeli medium/high ani LSP/MCP.
- Przenośne sprawdzenie zawartości i późniejsze zastąpienie pliku nie są jedną atomową operacją. Należy unikać zewnętrznych edycji podczas `apply`; nagłe zakończenie procesu może wymagać ręcznego odzyskania z kopii.
- Limity stdout/stderr ograniczają przechwytywane dane, nie wszystkie pliki tworzone przez uruchomione programy. Kopie robocze nie izolują hosta; na POSIX potomkowie mogą opuścić własną grupę procesów przez utworzenie osobnej sesji.
- Budżet czasu pozostaje kooperatywny, koszt może być częściowy, a automatycznego wznowienia przebiegu nie dodano.
