# Optymalizacja kontekstu — 2026-10-25

Cel: krótsze wejście dla ról Claude Code i Codex, bez pomijania kontroli, recenzentów, walidacji odpowiedzi ani dowodów wymagających doczytania. Nie zmieniano modeli ani poziomów effort. Zmiany obejmują prompty, przygotowanie kontekstu, ustawienia `[context]` i pomiary.

## Pomiar przed i po

Porównano poprzedni i nowy generator promptów na identycznym zadaniu, ścieżkach i danych syntetycznych. Liczono znaki całego promptu przekazanego do CLI, łącznie z JSON. Wyniki nie są pomiarem tokenów API, kosztu ani jakości odpowiedzi modelu.

| Scenariusz | Przed: znaki | Po: znaki | Redukcja |
| --- | ---: | ---: | ---: |
| Recenzja, jedna krótka udana kontrola | 3 713 | 1 974 | 46,8% |
| Recenzja, cztery udane kontrole z długimi logami | 121 452 | 2 451 | 98,0% |
| Fixer, jedna nieudana kontrola z długimi logami | 32 489 | 6 764 | 79,2% |
| Triage, jedno ustalenie i długa udana kontrola | 32 815 | 2 038 | 93,8% |
| Recenzja z wybranym plikiem umiejętności | 23 291 | 1 838 | 92,1% |
| Osobny przypadek dużego diffu | 65 510 | 18 566 | 71,7% |

Długie logi miały po 12 000 znaków stdout/stderr przed serializacją, plik umiejętności 19 000 znaków, a duża zmiana 4 000 dodanych wierszy. Końcowy przypadek dużego diffu sprawdzono po ustaleniu limitów pozostałych przypadków. Dokładna liczba znaków zależy również od długości ścieżek przebiegu.

Wybrano niewielką zmianę konstrukcji promptu: podsumowania kontroli, odsyłacze do logów i umiejętności, krótszy diff, jeden schemat w natywnym interfejsie CLI. Sześć szablonów ról skrócono łącznie z 3 676 do 2 913 znaków (20,8%). Nie usuwano recenzji ani nie zastępowano ich tańszym modelem.

## Kontekst i adaptery

- `prompts/common.md` i plik roli są ładowane przy wywołaniu. Wspólne reguły mają jednego właściciela.
- `AGENTS.md`/`CLAUDE.md` projektu są wskazywane przez ścieżki; agent czyta właściwe reguły dla oglądanych lub zmienianych plików.
- Wybrane `skills` są wymaganymi odczytami przed pracą; ich zasoby ładowane są według potrzeby. Tryb `inline` pozwala osadzić treść.
- Claude używa `--json-schema`, Codex `--output-schema`. Walidacja odpowiedzi w Pythonie pozostaje obowiązkowa.
- Pełny audyt pozostaje w `checks/` i `state.json`; `status --summary` służy do krótkich odczytów postępu.

Mechanizm odsyłaczy do wybranych umiejętności jest instrukcją workflow, nie obietnicą automatycznego odkrywania umiejętności przez CLI. Automatycznie wczytywane reguły, metadane narzędzi i globalne instrukcje mogą nadal zwiększać kontekst. Zalecenia źródłowe: [Claude Code — koszty](https://code.claude.com/docs/en/costs), [Claude Code — pamięć projektu](https://code.claude.com/docs/en/memory), [OpenAI — skills](https://learn.chatgpt.com/docs/build-skills).

## Weryfikacja i ograniczenia

Końcowa weryfikacja lokalna na Linuxie / Python 3.14.8:

| Kontrola | Wynik |
| --- | --- |
| `PYTHONPATH=src python3 -m unittest discover -s tests -q` | 187 testów: 185 zaliczonych, 2 wymagające Windows pominięte |
| Ruff przez reviewdog, lokalnie, `nofilter` | Brak diagnostyk |
| `ruff format --check src tests examples` | 28 plików zgodnych |
| Przykład: atrapy agentów i rzeczywiste kontrole Cargo | `succeeded`, 3 wywołania, bez Fixera |
| `status --summary` dla przykładu | Zgodny status, odsyłacze do raportu i nieznane tokeny atrap |

Lokalny artefakt przykładu: [report.md](../examples/rust-counter/.bb-workflow/runs/9e4f92ba7a4d485fb78a2bf59bbd5433/report.md), pomijany w dystrybucji. Testy używały kodu z `src/`, bez ponownej instalacji pakietu. Nie uruchamiano zdalnego CI ani natywnego Windows.

Testy sprawdzają zachowanie udanych i nieudanych kontroli, dokładne liczby pominiętych znaków, odczyt pełnych logów, limity zerowe, duże i binarne zmiany, obsługę umiejętności, zgodność starszych konfiguracji oraz niezależność recenzji. Oddzielne testy liczników obejmują cache, brak danych, nieprawidłowe wartości i nieudane wywołania.

Nie wykonano nowego porównania A/B z płatnymi sesjami modeli. Odczyt umiejętności lub pominiętego fragmentu diffu później również zużywa tokeny i może wymagać dodatkowego wywołania narzędzia. Redukcji długości promptu nie należy utożsamiać z takim samym spadkiem kosztu. Do oceny całego przebiegu służą nowe `token_usage`, dane o cache, status końcowy i liczba napraw; porównuj je dla tego samego zadania, modeli i ustawień.
