# MKV Translator

Tłumaczy napisy filmów i seriali (rosyjskie, angielskie, ukraińskie) na **polski albo angielski** lokalnymi modelami AI przez [Ollamę](https://ollama.com). Wynik trafia do pliku MKV jako nowa ścieżka napisów albo do pliku `.pl.srt` / `.en.srt` obok filmu.

*English: translates movie/series subtitles (Russian, English, Ukrainian) into Polish or English with local AI models via Ollama. The window and messages are available in Polish and English (language switch in the top-right corner).*

## Co potrafi

- **Źródła napisów:** ścieżki tekstowe w MKV/MP4 (SRT, ASS), telegazeta w plikach TS, napisy graficzne PGS (OCR, EasyOCR), osobne pliki napisów obok filmu.
- **Brak napisów:** rozpoznawanie mowy z dźwięku — GigaAM dla rosyjskiego, Whisper dla innych języków, opcjonalnie z oddzieleniem głosu od muzyki (Demucs).
- **Jakość:** kontrola każdego pliku (resztki języka źródłowego, zapętlenia, długość, tempo czytania), automatyczna naprawa podejrzanych kwestii drugim modelem, raport `raport_jakosci.csv`, słowniczek imion wspólny dla całego serialu.
- **Lektor (opcjonalnie):** polska ścieżka lektora z Piper albo XTTS-v2 nałożona na przyciszony oryginał.
- **Kolejka w oknie:** wznawianie przerwanych plików, szacowany czas końca, powiadomienia (Windows, telefon przez ntfy), wybór ścieżki napisów dla pliku, pobieranie modeli Ollamy, zmiana równoległości Ollamy.
- **Serwer:** tryb pracy ciągłej (`--watch`) i gotowy kontener dla Unraid — program na serwerze, Ollama na komputerze z kartą graficzną.

## Wymagania

- Windows, Python 3.12+ (program główny używa tylko biblioteki standardowej)
- [ffmpeg](https://ffmpeg.org) w `PATH`
- [Ollama](https://ollama.com) z modelem tłumaczącym — domyślny `gemma4:31b` (w teście na dwóch serialach wypadł lepiej niż `translategemma:27b`); dużym modelom najlepiej służy karta z 24–32 GB pamięci
- Funkcje audio (rozpoznawanie mowy, OCR, lektor): karta NVIDIA i [uv](https://docs.astral.sh/uv/) — `install_audio.bat` tworzy środowisko `audio_env` (ok. 6 GB, modele ok. 5 GB dociągają się przy pierwszym użyciu)

## Uruchomienie

Okno kolejki:

```bat
python translate_mkv.py
```

albo gotowy `MKV Translator.exe` z zakładki [Releases](../../releases) — obok niego połóż `audio_tools.py` i `ui_en.py`, jeśli chcesz funkcji audio i angielskiego interfejsu w komunikatach narzędzi audio.

Wiersz poleceń (przykłady):

```bat
python translate_mkv.py --mux -r "D:\Seriale\Serial"
python translate_mkv.py --target en --ui-lang en film.mkv
python translate_mkv.py --whisper --asr whisper --whisper-model large-v3-turbo film.mkv
python translate_mkv.py --check
```

Pełna lista opcji: `python translate_mkv.py --help`.

## Unraid

Folder `unraid/`: skopiuj go na serwer razem z `translate_mkv.py` i `ui_en.py`, ustaw adres Ollamy i foldery w `ustawienia.sh`, potem w terminalu Unraid:

```bash
bash /mnt/user/system/mkv-translator/zainstaluj.sh
```

Kontener co `CO_ILE_MINUT` sprawdza foldery i tłumaczy nowe pliki; postęp: `docker logs -f mkv-translator`.

## Licencje modeli

XTTS-v2 jest na licencji Coqui CPML (wyłącznie użytek niekomercyjny). Pozostałe modele mają własne licencje — sprawdź je przed użyciem.
