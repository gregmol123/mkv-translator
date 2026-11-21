# Historia zmian

Format według [Keep a Changelog](https://keepachangelog.com/pl/1.1.0/). Wersje programu odpowiadają wydaniom na GitHubie.

## [Niewydane]

### Dodane
- **Rozpoznawanie mówców** (pyannote community-1, opcja w „Gdy brak napisów”, `--diarize`, wymaga tokenu `HF_TOKEN`): każda kwestia dostaje mówcę, dwie krótkie wypowiedzi różnych osób łączą się w kwestię z myślnikami, a płeć mówiącego (z wysokości głosu) trafia do tłumacza, żeby dobrał „zrobiłem/zrobiłam”. (B12)
- **Dwa modele, wersja 2 — domyślny silnik rozpoznawania mowy:** GigaAM i Whisper zapisują całość, a model językowy wybiera lepszy zapis dla każdej minuty filmu. W pomiarze 10,8% błędu słów wobec 13,2% samego GigaAM + Demucs, przy ok. 3,5× dłuższym rozpoznawaniu. (B8)

## [1.3] — 2026-11-21

### Dodane
- **Nowy podział rozpoznanej mowy na kwestie** wg norm Netflix PL: cięcie w najlepszym miejscu (koniec zdania, przecinek, pauza), 2 × 42 znaki, 0,83–7 s, przerwa 2 klatki, napis 0,4 s po ostatnim słowie; po pytaniu kwestie nie są sklejane. (B4)
- **Pewność rozpoznania każdej kwestii** — raport jakości podaje liczbę niepewnych kwestii, powyżej 15% plik idzie „Do sprawdzenia”. (B7)
- **Silnik „GigaAM-RNNT”**: dokładniejsze słowa (12,0% wobec 13,1% w pomiarze), interpunkcję i wielkie litery dopisuje model językowy bez zmiany słów. (B9)
- **Whisper szybciej i dokładniej w czasie:** przetwarzanie w partiach (9× szybciej, 12,1% zamiast 12,6%) i wyrównanie czasów słów Qwen3-ForcedAligner (przesunięcie kwestii 275 → 153 ms) — domyślnie dla Whispera. (B10, B11)
- Opcje do eksperymentów w `audio_tools.py` i `tools/asr_bench.py`: wariant GigaAM (`--gigaam-model`), cięcie mowy (`--vad-silence-ms`, `--vad-max-s`), Silero VAD v6 (`--vad silero6`), separacja BS-RoFormer (`--sep roformer`), domieszka oryginału (`--sep-mix`), silnik Parakeet. W pomiarach nie poprawiły wyniku, więc są wyłączone. (B2, B3, B5, B6, B11)

## [1.2] — 2026-11-21

### Dodane
- Plan ulepszeń w `TODO.md` i raport badawczy o rozpoznawaniu mowy, standardach napisów i modelach tłumaczenia.
- Dokumentacja: `docs/ARCHITEKTURA.md`, `CONTRIBUTING.md`.
- **Eksperymentalnie: dwa modele naraz** (Silnik „GigaAM+Whisper”, `--asr gigaam+whisper`): GigaAM na głosie z Demucsa i Whisper na miksie, rozbieżne kwestie rozstrzyga model językowy, a wybór gubiący słowa jest odrzucany. W pomiarze B1 nie poprawia wyniku (13,0% wobec 13,0%) i trwa 2,6× dłużej — domyślnie wyłączone. (B8, pierwsza wersja)
- **Pomiar rozpoznawania mowy** `tools/asr_bench.py`: rosyjskie napisy z filmu jako wzorzec, błąd słów/znaków, przesunięcie kwestii i kwestie-widma dla kilku ustawień; pierwszy pomiar w `reports/asr_bench 2026-11-21 1658.md`. (B1)
- **Filtr halucynacji rozpoznawania mowy:** usuwa napisy-widma Whispera („Субтитры сделал…”, „Продолжение следует”, „Thanks for watching”), zapętlone zdania i kwestie o bardzo niskiej pewności. (A1)
- **Słowniczek imion w poleceniu dla modelu:** ustalona pisownia imion serialu trafia do tłumaczenia od razu, nie tylko do poprawek po fakcie. (A9)
- **Schemat odpowiedzi w Ollamie:** model musi zwrócić dokładnie tyle kwestii, ile dostał — mniej zgubionych kwestii i ponowień. (A10)
- **Tempo czytania wg norm Netflix PL:** raport jakości pokazuje osobno kwestie szybkie (17–20 znaków/s) i za szybkie (powyżej 20). (A11)
- **Kopia oryginału przy podmianie** (opcja w „Zapis wyniku”, `--backup-days`, `KOPIA_DNI` na Unraid): oryginał zostaje jako `<nazwa>.orig.mkv` i program kasuje go po podanej liczbie dni. Domyślnie wyłączona, bo zajmuje tyle miejsca co film. Kasowane są tylko kopie zapisane w `kopie_oryginalow.json`. (A4)

### Zmienione
- **Streszczenie odcinka dla tłumacza:** przed tłumaczeniem model raz streszcza cały odcinek (postaci, płeć, relacje, ty/Pan) i to streszczenie trafia do każdej partii jako tło. Ok. 10 s na plik; `MKV_SUMMARY=0` wyłącza. (C2)
- **Szerszy kontekst tłumaczenia:** model widzi też 4 kwestie po partii (zdania rozcięte między kwestie) i 5 ostatnich gotowych par oryginał → tłumaczenie, żeby trzymać formę „ty/Pan”, rodzaj mówiących i pisownię między partiami. (C1)
- **Język mowy z 8 próbek** rozłożonych po filmie (głosowanie z wagą pewności) zamiast jednej — piosenka albo lektor w jednym miejscu nie zmienia już wyniku; przy 5.1 próbki z kanału centralnego. (A8)
- **Pamięć karty:** przed rozpoznawaniem mowy Ollama zwalnia model, a po rozpoznawaniu proces audio zwalnia swoje modele — tłumaczenie nie spada częściowo na procesor. (A5)
- **5.1 z oddzielaniem głosu:** Demucs dostaje kanał centralny zamiast całego miksu; dziennik podaje użytą ścieżkę i liczbę kanałów. (A12)
- **Rozpoznany tekst (`.rozpoznane.ru.srt`)** jest używany ponownie tylko wtedy, gdy powstał tym samym silnikiem i w tym samym języku. (A3)

### Naprawione
- **Podmiana MKV zachowuje flagi ścieżek** „wymuszone”, „dla niesłyszących” i „komentarz” — wcześniej były zerowane; kontrola przed podmianą sprawdza teraz flagi i załączniki (czcionki napisów). (A2)
- **Zapis napisów przez plik tymczasowy** — przerwany zapis nie zostawia niepełnego pliku, który następny przebieg wbudowałby w film. (A3)
- **Limity czasu** dla ffmpeg i ffprobe oraz dla procesu audio — zawieszony dysk sieciowy nie blokuje kolejki, a Stop działa zawsze. (A6)
- **Pełny ślad nieprzewidzianego błędu** trafia do `dziennik.txt`. (A7)

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
