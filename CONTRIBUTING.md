# Jak współtworzyć

## Środowisko

- Python 3.12+ dla `translate_mkv.py` (tylko biblioteka standardowa).
- `install_audio.bat` tworzy `audio_env` z narzędziami audio (karta NVIDIA, [uv](https://docs.astral.sh/uv/)).
- ffmpeg w `PATH` i Ollama z modelem tłumaczącym.
- `python translate_mkv.py --check` sprawdza instalację.

## Zasady w kodzie

- **Komentarze i komunikaty po polsku**, zwięzłe, jak w reszcie kodu.
- **Każdy tekst dla użytkownika przez `tr()`** z wpisem w `ui_en.py`. Szablony z `{0}`, `{1}` zamiast f-stringów, żeby tłumaczenie mogło zmienić kolejkę słów.
- **Stany pliku** („Gotowe”, „Do sprawdzenia”, „Ma polskie”…) to klucze zapisywane w `kolejka.json` — nie zmieniaj ich tekstu; nazwę pokazywaną zmienia `QueueApp.STATE_LABELS`.
- **Nigdy nie podmieniaj oryginału bez kontroli:** nowy plik powstaje obok, `check_remux()` go sprawdza, dopiero potem `os.replace`.
- **Ścieżki docelowe języka** zawsze przez `OUT["code"]` i `TARGETS`, nie wpisane `.pl.` na sztywno.

## Przed wysłaniem zmian

```bat
python -m pyflakes translate_mkv.py audio_tools.py ui_en.py
python -W error -c "compile(open('translate_mkv.py', encoding='utf-8').read(), 'translate_mkv.py', 'exec')"
```

Zmianę w oknie sprawdź w obu językach (przełącznik „Język okna”). Zmianę w tłumaczeniu sprawdź na kilkudziesięciu prawdziwych kwestiach i opisz wynik w `CHANGELOG.md` w sekcji „Niewydane”.

## Wydanie

Przed wydaniem podnieś `VERSION` w `translate_mkv.py` i przenieś sekcję „Niewydane” w `CHANGELOG.md` pod nowy numer — ten sam numer ma tag na GitHubie (np. `v1.4.1`).

## Budowanie .exe

```bat
python -m PyInstaller --noconfirm --onefile --windowed --name "MKV Translator" --add-data "CHANGELOG.md;." translate_mkv.py
```

`ui_en.py` trafia do środka automatycznie. Obok `.exe` muszą leżeć `audio_tools.py`, `ui_en.py` i `audio_env`, jeśli mają działać funkcje audio.
