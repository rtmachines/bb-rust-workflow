# Przegląd niezawodności i pracy agentów — 2026-10-25

Analiza objęła adaptery, procesy, maszynę stanów, protokół odpowiedzi, kontekst promptów, konfigurację, CLI i pomiary oraz odpowiadające im testy. Zakres zawężano grafem kodu, a wnioski sprawdzano w źródłach. Brak wykrytego przepływu w grafie nie był traktowany jako dowód braku zależności. Porównanie zmian wykonano z lokalną kopią stanu początkowego, bez polegania na historii Git.

## Stan początkowy

- 187 testów: wynik poprawny, 2 pominięte. Ruff i kontrola formatowania poprawne.
- Istnieją już niezależne kopie i procesy recenzentów, kontrola niezmienności źródeł, ograniczenia czasu/wyjścia, brak ponawiania zapisujących ról oraz jawne `apply` z kontrolą całego manifestu.
- Prompty mają ograniczone fragmenty diffu i logów z odsyłaczami do pełnych dowodów. Schemat natywny dostawcy jest ponownie walidowany lokalnie. Nie wykazano potrzeby ich dalszego skracania.
- `usage.py` poprawnie rozróżnia brak pomiaru od zera i nie dodaje cache dwukrotnie. Nie wymagał zmiany.

## Potwierdzone problemy i wdrożone poprawki

| Priorytet | Dowód i skutek przed zmianą | Poprawka i test regresji |
| --- | --- | --- |
| Wysoki | `Workflow._agent` ponawiał każdy `BackendError` w roli odczytowej. Zmiana modelu, brak programu i limit wyjścia mogły zużywać kolejne próby bez zmiany konfiguracji. | [Adapter](../src/bb_workflow/backends.py) przekazuje `retryable`; [silnik](../src/bb_workflow/engine.py) respektuje klasyfikację po kontroli niezmienności kopii. Testy: `test_model_drift_is_not_retryable_even_after_process_failure`, `test_nonretryable_backend_failure_stops_without_spending_retry_budget`. |
| Wysoki | Adapter tracił poprawne `usage` i `total_cost_usd` z odpowiedzi zakończonej błędem. Ręczne aktualizowanie sum w silniku pomijało `KeyboardInterrupt` i nieoczekiwane wyjątki: wywołanie bez pomiaru kosztu mogło nie zostać policzone jako nieznane. | Adapter zachowuje pomiary w wyjątku i `invocation.json`; sumy kosztów są wyliczane z rekordów przy zapisie stanu. Testy: `test_failed_claude_retains_reported_usage_and_cost`, `test_failed_attempt_telemetry_is_counted_once_alongside_retry`, `test_keyboard_interrupt_records_agent_interruption_and_releases_lock`, `test_unexpected_provider_exception_keeps_cost_unknown`. |
| Wysoki | `resolve_triage` zachowywał najwyższą wagę grupy, lecz usuwał odrębne dowody duplikatów z danych kierowanych do Fixera. `_fix` nie przekazywał katalogu bazowego mimo dopuszczania ustaleń o usuniętych plikach. | [Protokół](../src/bb_workflow/protocol.py) zachowuje pełne `duplicates`, bez modyfikowania wejścia; Fixer otrzymuje `baseline_directory`. Testy: `test_duplicate_groups_retain_distinct_evidence_and_provenance_for_repair`, `test_fixer_receives_baseline_for_deleted_file_and_duplicate_evidence`. |
| Średni | Brak skill/MCP dla recenzenta wykrywano dopiero po implementacji i kontrolach. | `_execute` sprawdza zasoby wszystkich wybranych ról po utworzeniu kopii, przed kontrolami i dostawcami. Kontrole przy wywołaniu pozostają. Testy: `test_assets_for_later_roles_are_checked_before_any_provider_calls`, `test_skill_removed_after_preflight_is_checked_again_before_role`, `test_existing_role_assets_pass_preflight_and_resolve_mcp_from_original_project`. |
| Średni | README pokazywał testy wymagające uprzedniej instalacji, choć układ `src/` pozwala pracować lokalnie bez instalowania. | [README](../README.md) i [AGENTS.md](../AGENTS.md) zawierają mapę modułów, pełne i skupione polecenia testów oraz użycie reviewdog. `CLAUDE.md` nadal importuje wspólne instrukcje. |

Wybrano zmiany dotyczące granic wywołania dostawcy i przekazywania dowodów. Zapobiegają zbędnym wywołaniom oraz mylącym raportom bez zmiany modeli, kolejności etapów, niezależności przeglądów, schematu odpowiedzi ani zasad `apply`. Runtime pozostaje oparty wyłącznie na bibliotece standardowej Pythona. Nie wykonano szerokiej refaktoryzacji ani zmian promptów na podstawie samej ich długości.

## Pomiar przed i po

Te same scenariusze wykonano za pomocą `EngineTests` na kopii początkowego kodu i kodzie po poprawkach. Wszystkie odpowiedzi i kwoty są syntetycznymi danymi fixture.

| Scenariusz | Przed | Po |
| --- | --- | --- |
| Brak skill recenzenta | `blocked`, 1 wywołanie implementera | `blocked`, 0 wywołań |
| Stały błąd zmiany modelu recenzenta, `max_retries=3` | `failed`, 5 wywołań ogółem: implementer + 4 próby recenzenta | `failed`, 2 wywołania ogółem: implementer + 1 próba recenzenta |
| Nieudane wywołanie z raportowanym kosztem 0,75 USD i 15 tokenami | 0 USD w znanej sumie, 1 nieznany koszt, tokeny `null` | zachowane 0,75 USD i 15 tokenów z fixture; status nadal `failed` |
| Przerwanie pierwszego wywołania bez danych | `interrupted`, 0 wywołań o nieznanym koszcie | `interrupted`, 1 wywołanie o nieznanym koszcie |

Zmierzono liczbę wywołań i poprawność przetwarzania danych fixture. Nie zmierzono oszczędności rzeczywistych tokenów, USD ani czasu odpowiedzi usług. Oczekiwany efekt to pominięcie czasu i ewentualnego kosztu zbędnych prób. Pełniejsze dowody Fixera mogą zwiększyć jego prompt; jest to zamierzona ochrona jakości naprawy. `prompt_chars`/`prompt_bytes` nadal nie są pomiarem tokenów dostawcy.

## Weryfikacja

- `PYTHONPATH=src python3 -m unittest discover -s tests -q`: **200 testów, OK, 2 pominięte**, 35,951 s. To czas kontroli lokalnej, nie benchmark usług.
- Ruff dla `src tests examples`: kod 0, brak diagnostyki. Surowy format `rdjson` przekazano do lokalnego reviewdog z `nofilter`; reviewdog również zakończył się kodem 0.
- `ruff format --check src tests examples`: kod 0, wszystkie 28 plików poprawnie sformatowane.
- Niezależne przeglądy zmian adapterów, silnika i protokołu nie wykazały problemów wymagających poprawki.
- Niepowodzenia końcowych kontroli: **brak**.
- Ograniczenia środowiska: lokalny Linux, Python 3.14.8; 2 testy natywnych junctions Windows pominięte. Nie wykonano macierzy CI ani osobnego uruchomienia na Pythonie 3.11.
- Nie uruchamiano sesji modeli. Testy korzystają z fixture; zgodność rzeczywistych kont i pełny przebieg obu dostawców pozostają nieweryfikowane w tym zadaniu.

## Pozostałe ryzyka i hipotezy

- Nieznane błędy dostawcy pozostają objęte ograniczonym retry w rolach odczytowych. Dalsze klasyfikowanie limitów konta lub błędów uwierzytelnienia wymaga stabilnych, strukturalnych sygnałów dostawcy; nie dodano heurystyk opartych na treści komunikatów.
- Telemetria odzyskana po błędzie może obejmować tylko raportowaną część użycia. Nie jest dowodem pełnego rozliczenia sesji. Automatycznego wznowienia przerwanego przebiegu nadal nie ma.
- Pełna ciągłość logów przy przerwaniu pozostaje ograniczeniem: `backends.invoke` zapisuje stdout/stderr po powrocie `run_process`, a `KeyboardInterrupt` przerywa ten powrót. Sprzątanie procesu i stan `interrupted` są testowane, ale utrwalanie częściowego wyjścia w trakcie takiego przerwania wymaga osobnej zmiany kontraktu przechwytywania logów.
- Potwierdzone, odłożone przypadki brzegowe: `status --summary` dla ręcznie uszkodzonego `state.json` zawierającego `null` zgłasza `TypeError`; skrajnie duży całkowity limit TOML może wywołać `OverflowError`. Są poza wybranym zakresem poprawy granic dostawcy i dowodów.
- Hipoteza do przyszłego pomiaru: ograniczenie zbędnych prób skróci płatne przebiegi z tymi błędami. Nie przypisano temu procentu oszczędności czasu lub tokenów. Zmiana modeli, skracanie dowodów albo równoległe recenzje nie były uzasadnione zebranymi pomiarami.
