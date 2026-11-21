# Plan prac — MKV Translator

Stan na 21.11.2026, 16:04. Źródło: raport `reports/Ulepszenia MKV Translatora.md`. Wysiłek mojej pracy: S do godziny, M pół dnia, L dzień lub więcej. Koszt: 0 zł (wszystko lokalnie).

## Zrobione

- [x] Raport „Ulepszenia MKV Translatora” (badania + opinia Qwena + audyt kodu) — reports/Ulepszenia MKV Translatora.md
- [x] CHANGELOG.md, CONTRIBUTING.md, docs/ARCHITEKTURA.md
- [x] Wersja 1.1: język mowy z próbki dźwięku zamiast z oznaczenia ścieżki — GitHub v1.1
- [x] Z1: zaczynamy od Etapu 1 (szybkie poprawki); A4 czeka na decyzję Z3 — 21.11
- [x] Etap 1 bez A4 zrobiony (A1–A3, A5–A12): filtr halucynacji, flagi ścieżek, zapis atomowy, pamięć karty, limity czasu, ślad błędów, język z 8 próbek (Shef 01: ru 92%), słowniczek w poleceniu, schemat JSON, tempo 17/20 zn/s, kanał centralny do Demucsa; testy OK, .exe przebudowany, bez wydania — 21.11
- [x] A4 i Z3: kopia .orig.mkv jako opcja (domyślnie wyłączona, 7 dni po włączeniu, na tym samym dysku co film; Unraid: KOPIA_DNI); kasowane tylko kopie programu; testy OK — 21.11
- [x] C1: kontekst z 4 kwestiami po partii i 5 gotowymi parami tłumaczeń; próba na 90 kwestiach Shef 15 (gemma4:26b, 17 s) OK — 21.11

## Do decyzji (Ty)

- [ ] **Z2** Zestaw wzorcowy B1: wskaż 5–10 filmów/odcinków z oryginalnymi rosyjskimi napisami (posłużą za wzorzec do mierzenia rozpoznawania mowy) — 15 min Twoje
- [ ] **Z4** Licencja repozytorium na GitHubie (np. MIT) — bez niej inni nie mogą legalnie używać kodu — 2 min Twoje
- [ ] **Z5** Wgranie nowej wersji na Unraid, gdy serwer będzie włączony (potem zainstaluj.sh w terminalu Unraid) — 5 min Twoje

## Etap 1 – szybkie poprawki, bezpieczeństwo danych

_ok. 1 dzień mojej pracy, bez pomiarów_

- [x] **A1** Filtr halucynacji Whispera: czarna lista RU/EN („Субтитры сделал…”, „Продолжение следует”), usuwanie powtórzeń, odrzucanie przy niskiej pewności — S
- [x] **A2** Podmiana MKV zachowuje flagi ścieżek (forced, hearing_impaired); check_remux porównuje flagi, załączniki i tagi — S
- [x] **A3** Zapis atomowy .tmp + os.replace dla napisów; metadane silnik/model/język przy rozpoznanym SRT — S
- [x] **A4** Opcjonalna kopia .orig.mkv przez N dni (po decyzji Z3) — S
- [x] **A5** Zwalnianie modeli rozpoznawania mowy z karty przed tłumaczeniem; keep_alive=0 w Ollamie przed rozpoznawaniem — S
- [x] **A6** Limity czasu ffmpeg/ffprobe i procesu audio (Stop zawsze działa), bez ponawiania przy braku pamięci — S/M
- [x] **A7** Pełny ślad błędu do dziennik.txt — S
- [x] **A8** Wykrywanie języka mowy z 6–8 okien rozłożonych po filmie (dziś realnie decyduje ~30 s), kanał centralny, głosowanie — S/M
- [x] **A9** Słowniczek imion w prompcie tłumaczenia i jako podpowiedzi dla Whispera — S
- [x] **A10** Schemat JSON (format) w Ollamie, num_ctx 16k, większe partie — S
- [x] **A11** Tempo czytania wg norm Netflix PL: ostrzeżenie powyżej 17 zn/s, błąd powyżej 20 — S
- [x] **A12** 5.1: kanał centralny także przy separacji głosu; dziennik mówi, której ścieżki i kanałów użyto — S

## Etap 2 – perfekcyjne rozpoznawanie mowy

_ok. 4–6 dni mojej pracy; każdą zmianę mierzymy na B1_

- [ ] **B1** Zestaw wzorcowy 5–10 klipów (muzyka, szept, akcja, piosenka, szybki dialog) i pomiar WER/CER, czasów i halucynacji — M
- [ ] **B2** GigaAM: przejście na wariant v3 RNNT (8,3% WER zamiast 9,1%), porównanie z GigaAM Multilingual 600M — S
- [ ] **B3** Cięcie mowy w ciszy (500–700 ms, maks. 25–28 s) zamiast twardo co 20 s w środku słowa — M
- [ ] **B4** Nowy podział na kwestie: koniec zdania lub pauza, 42 zn × 2 linie, min. 20 klatek, maks. 7 s, przerwa 2 klatki, ogon +0,4 s — M
- [ ] **B5** Silero VAD v6; próba TEN VAD na cichych i szeptanych kwestiach — S/M
- [ ] **B6** Separacja BS/Mel-RoFormer zamiast htdemucs — do wykrywania mowy i podziału; rozpoznawanie na oryginale albo wokalu z domieszką (A/B na B1) — M
- [ ] **B7** Pewność rozpoznania dla każdej kwestii; „niepewne rozpoznanie” w raporcie jakości — M
- [ ] **B8** Dwa modele naraz (GigaAM RNNT + Whisper/GigaAM-ML); przy niezgodności LLM wybiera wersję; tekst tylko z Whispera przy ciszy = halucynacja — L
- [ ] **B9** Interpunkcja: wariant e2e-RNNT albo LLM na tekście RNNT — M
- [ ] **B10** Dokładne czasy słów: Qwen3-ForcedAligner (RU/EN), MMS (UK) — M
- [ ] **B11** Whisper w partiach (batch 16, beam 5) po A1; dla angielskiego próba Parakeet-TDT-0.6B-v3 — M
- [ ] **B12** Rozpoznawanie mówców (pyannote community-1): myślniki dialogowe, bez sklejania mówców, płeć mówiącego — L

## Etap 3 – tłumaczenie

_ok. 2–3 dni mojej pracy_

- [x] **C1** Kontekst: 3–4 kwestie następne + ostatnie 5 par oryginał→tłumaczenie — M
- [ ] **C2** Streszczenie filmu/odcinka generowane raz i dodawane do każdej partii — S
- [ ] **C3** Płeć mówiących (zrobiłem/zrobiłam): wnioskowanie z form rosyjskich + pole w słowniczku — S
- [ ] **C4** Polskie normy zapisu: „…”, cudzysłowy „”, myślniki dialogowe, godziny 14.15 — S
- [ ] **C5** Ocena jakości bez wzorca: MetricX-24 + sprawdzenie języka wyniku; najgorsze kwestie do naprawy — M
- [ ] **C6** Porównanie modeli na Twoich filmach: gemma4:31b kontra MiLMMT-46-12B — M
- [ ] **C7** Tłumaczenie pełnymi zdaniami z rozpoznanej mowy i podział z powrotem wg czasu — L

## Etap 4 – utrzymanie

_ok. 2–3 dni mojej pracy_

- [ ] **D1** Przypięte wersje pakietów audio_env (uv pip compile, hashe) — S
- [ ] **D2** Testy automatyczne (pytest) + test na 30-sekundowym MKV — M
- [ ] **D3** Kontekst pliku zamiast stanu globalnego; ta sama ścieżka audio dla lektora i rozpoznawania — M
- [ ] **D4** Podział programu na moduły; kopia dla Unraid generowana skryptem — L
- [ ] **D5** Składanie MKV przez mkvmerge zamiast ffmpeg — M
