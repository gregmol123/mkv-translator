# Historia zmian

Format według [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/). Wersje programu odpowiadają wydaniom na GitHubie.

## [Niewydane]

### Dodane
- Plan ulepszeń w `TODO.md` i raport badawczy o rozpoznawaniu mowy, standardach napisów i modelach tłumaczenia.
- Dokumentacja: `docs/ARCHITEKTURA.md`, `CONTRIBUTING.md`.

## [1.1] — 2026-11-20

### Zmienione
- Rozpoznawanie mowy przy „Z języka: automatycznie” sprawdza język na próbce dźwięku zamiast ufać oznaczeniu ścieżki. Rosyjskie seriale oznaczone jako `eng` trafiały do Whispera zamiast GigaAM. Gdy próbka nic nie rozstrzyga (np. sama muzyka), zostaje oznaczenie ścieżki.

## [1.0] — 2026-11-03

Pierwsze wydanie na GitHubie. Zawiera całą dotychczasową pracę nad programem.

### Dodane
- **Język okna PL/EN:** przełącznik w oknie, `--ui-lang` w wierszu poleceń; teksty przez `tr()` i słownik `ui_en.py`.
- **Język napisów wynikowych:** polski albo angielski (`--target`); wynik `.pl.srt` / `.en.srt`, ścieżka „Polski” / „English” w MKV.
- **Wybór ścieżki napisów dla pliku** (prawy przycisk w kolejce) i kolumna „Napisy” w tabeli.
- **Model do poprawek** (`--repair-model`) i **model Whisper** (`--whisper-model`) do wyboru w oknie.
- **Pobieranie modeli Ollamy** z okna, z postępem.
- **Zmiana równoległości Ollamy** przed startem kolejki (restart Ollamy z `OLLAMA_NUM_PARALLEL`).
- **OCR napisów graficznych PGS** (EasyOCR) jako źródło do tłumaczenia.
- **Telegazeta** z plików TS i zamiana TS → MKV bez przekodowania.
- **Lektor** Piper albo XTTS-v2 nałożony na przyciszony oryginał; `--revoice` nagrywa lektora od nowa z gotowych napisów.
- **Kontrola jakości** każdego pliku (resztki języka źródłowego, zapętlenia, długość, tempo czytania), raport `raport_jakosci.csv`, stan „Do sprawdzenia”.
- **Automatyczna naprawa** podejrzanych kwestii drugim modelem i skracanie zbyt szybkich napisów.
- **Słowniczek imion** wspólny dla całego serialu (`slowniczek.json`).
- **Wykrywanie gotowych napisów** (wbudowanych i obok filmu) — plik oznaczany „Ma już napisy”.
- **Przenoszenie gotowych filmów** do folderu `skonczone` na dyskach lokalnych.
- **Szacowany czas końca** kolejki i powiadomienia (Windows, telefon przez ntfy).
- **Stały proces audio** — modele rozpoznawania mowy wczytują się raz na kolejkę.
- **Tryb serwera** `--watch` i kontener dla Unraid (`unraid/`).
- **Nazwy w formacie Plex** z możliwością cofnięcia.

### Zmienione
- Domyślny model tłumaczenia: `gemma4:31b` zamiast `translategemma:27b` — wygrał porównanie na dwóch serialach (`Chyornaya grafinya`, `Gorkij 53`). `translategemma:27b` jest teraz pierwszym modelem do poprawek.
- Tłumaczenie z angielskiego i ukraińskiego obok rosyjskiego.

### Naprawione
- Bezpieczna podmiana oryginału MKV: kontrola długości, ścieżek obrazu, dźwięku, napisów i rozdziałów przed zastąpieniem pliku.
- Filmy TS z obrazem z przeplotem (1080i) nie przechodziły do MKV — uzupełnianie znaczników czasu.
- Ścieżki „Forced” (kilka kwestii) nie są już wybierane jako źródło tłumaczenia.
