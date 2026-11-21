# Architektura MKV Translatora

Opis dla osoby, która chce zmieniać kod. Program to dwa procesy Pythona i lokalny serwer Ollama.

```
translate_mkv.py  ──(JSON przez stdin/stdout)──>  audio_tools.py serve   (audio_env: torch, Whisper, GigaAM, Demucs, EasyOCR, Piper, XTTS)
       │
       └──(HTTP /api/chat)──>  Ollama  (gemma4:31b, translategemma:27b ...)
```

## Pliki

| Plik | Co robi |
|---|---|
| `translate_mkv.py` | Cały program główny: okno kolejki (tkinter), wiersz poleceń, tłumaczenie, kontrola jakości, składanie MKV, tryb serwera. Tylko biblioteka standardowa Pythona. |
| `audio_tools.py` | Narzędzia wymagające karty graficznej, uruchamiane w środowisku `audio_env`: rozpoznawanie mowy, wykrywanie języka mowy, separacja głosu, OCR napisów PGS, lektor. |
| `ui_en.py` | Angielskie teksty okna i komunikatów. Kluczem jest polski tekst z kodu. |
| `unraid/` | Kontener Docker dla serwera Unraid: tylko ffmpeg i Python, tłumaczy zdalna Ollama. |

## Przebieg jednego pliku — `process()`

1. **Język wyniku** z `--target` (`OUT["code"]`: `pl` albo `en`).
2. **Czy jest co robić:** gotowe napisy w języku docelowym (wbudowane albo obok filmu) dają stan „Ma już napisy”; istniejący wynik daje „Pominięto”.
3. **Źródło napisów** — `pick_source_cues()`, w tej kolejności:
   - ścieżki tekstowe w pliku (SRT, ASS, telegazeta), z wyborem najpełniejszej w języku źródłowym, bez ścieżek „Forced”;
   - napisy graficzne PGS przez OCR (`ocr_cues()` → `audio_tools.py ocr`);
   - osobny plik napisów obok filmu (`external_cues()`);
   - rozpoznawanie mowy (`transcribe()` → `audio_tools.py transcribe`), także gdy napisów jest podejrzanie mało.
4. **Tłumaczenie** — `translate_cues()`: partie po 30 kwestii do Ollamy, kilka partii naraz (`--parallel`), zapis postępu w `.pl.part.json` do wznawiania.
5. **Naprawa i kontrola:** `repair_segments()` (drugi model dla podejrzanych kwestii), `unify_names()` (słowniczek imion serialu), `retime()` (czas wyświetlania), `shorten_fast()` (skracanie zbyt szybkich), `quality_check()` → stan „Gotowe” albo „Do sprawdzenia”, wiersz w `raport_jakosci.csv`.
6. **Zapis:** `.pl.srt` obok filmu albo `mux()` do MKV; przy podmianie oryginału `check_remux()` sprawdza długość, ścieżki i rozdziały, zanim nowy plik zastąpi stary.
7. **Opcjonalnie:** lektor (`audio_tools.py tts`) i przeniesienie do folderu `skonczone`.

## Rozpoznawanie mowy — `transcribe()`

1. Wybór ścieżki dźwiękowej (`main_audio()`) i, przy „automatycznie”, rozpoznanie języka na 15-minutowej próbce (`detect_speech_language()`), bo oznaczenia ścieżek bywają błędne.
2. Wyodrębnienie dźwięku ffmpegiem: kanał centralny z 5.1 albo stereo 44,1 kHz dla Demucsa.
3. `audio_tools.py transcribe`: opcjonalna separacja głosu (Demucs), potem GigaAM v3 (rosyjski) albo faster-whisper (inne języki), scalanie słów w kwestie.
4. Wynik w `.rozpoznane.ru.srt` (nazwa historyczna), używany ponownie przy wznowieniu.

## Proces audio

`audio_tools.py serve` działa przez całą kolejkę, żeby modele wczytały się raz. Polecenia przychodzą jako JSON w jednym wierszu, koniec odpowiedzi oznacza `\x1eKONIEC`. `MKV_AUDIO_PERSIST=0` wyłącza ten tryb. Błędy z pełnym śladem trafiają do `audio_blad.txt`.

## Okno — `QueueApp`

- Stan pliku trzymany w `self.states` (klucz po polsku), w tabeli widać `tr(stan)`.
- Praca w wątku roboczym, wyniki wracają przez kolejkę zdarzeń `self.events` odczytywaną w `poll()` co 100 ms.
- Kolejka i ustawienia zapisują się w `kolejka.json` obok programu; dziennik w `dziennik.txt`.
- Zmiana języka okna przebudowuje okno (`switch_ui_lang()`), kolejka przechodzi przez `kolejka.json`.

## Teksty i języki

Wszystkie teksty dla użytkownika przechodzą przez `tr("polski tekst {0}", argument)`. Nowy tekst wymaga wpisu w `ui_en.py` — inaczej w angielskim oknie pokaże się po polsku. Odstępy na brzegach nie są częścią klucza.

## Pliki robocze obok filmu

| Plik | Znaczenie |
|---|---|
| `<film>.pl.srt` / `.en.srt` | wynik (usuwany po wbudowaniu, jeśli powstał w tym przebiegu) |
| `<film>.pl.bak.srt` | poprzedni wynik przy tłumaczeniu od nowa |
| `<film>.pl.part.json` | częściowe tłumaczenie do wznowienia |
| `<film>.rozpoznane.ru.srt` | tekst rozpoznany z mowy |
| `<film>.lektor.part/` | gotowe kwestie lektora do wznowienia |
| `slowniczek.json` | pisownia imion w serialu (folder serialu) |
