# Plan prac — MKV Translator

Stan na 21.11.2026, 19:51. Źródło: raport `reports/Ulepszenia MKV Translatora.md`. Wysiłek mojej pracy: S do godziny, M pół dnia, L dzień lub więcej. Koszt: 0 zł (wszystko lokalnie).

## Zrobione

- [x] Raport „Ulepszenia MKV Translatora” (badania + opinia Qwena + audyt kodu) — reports/Ulepszenia MKV Translatora.md
- [x] CHANGELOG.md, CONTRIBUTING.md, docs/ARCHITEKTURA.md
- [x] Wersja 1.1: język mowy z próbki dźwięku zamiast z oznaczenia ścieżki — GitHub v1.1
- [x] Z1: zaczynamy od Etapu 1 (szybkie poprawki); A4 czeka na decyzję Z3 — 21.11
- [x] Etap 1 bez A4 zrobiony (A1–A3, A5–A12): filtr halucynacji, flagi ścieżek, zapis atomowy, pamięć karty, limity czasu, ślad błędów, język z 8 próbek (Shef 01: ru 92%), słowniczek w poleceniu, schemat JSON, tempo 17/20 zn/s, kanał centralny do Demucsa; testy OK, .exe przebudowany, bez wydania — 21.11
- [x] A4 i Z3: kopia .orig.mkv jako opcja (domyślnie wyłączona, 7 dni po włączeniu, na tym samym dysku co film; Unraid: KOPIA_DNI); kasowane tylko kopie programu; testy OK — 21.11
- [x] C1: kontekst z 4 kwestiami po partii i 5 gotowymi parami tłumaczeń; próba na 90 kwestiach Shef 15 (gemma4:26b, 17 s) OK — 21.11
- [x] C2: streszczenie odcinka raz na plik w poleceniu dla modelu; próba Shef 15: 547 kwestii, 9 s, trafne postaci i formy ty/Pan — 21.11
- [x] Z2: 6 odcinków z rosyjskimi napisami (Chyornaya grafinya, Gorkij 53, Poluraspad po 2) w M:\!TEMP\B1 — 21.11
- [x] B1: pomiar 6 odcinków × 2 fragmenty × 4 ustawienia (3479 słów wzorca, 7 min): błąd słów GigaAM 15,2%, GigaAM+Demucs 13,3%, Whisper 12,6%, Whisper+Demucs 15,1%; czasy kwestii GigaAM ~150 ms, Whisper ~290 ms; lepszy z pary GigaAM+Demucs/Whisper na fragment dałby 10,6% → B8 ma sens (reports/asr_bench 2026-11-21 1658.md) — 21.11
- [x] B8 (pierwsza wersja): GigaAM + Whisper naraz, rozbieżne kwestie rozstrzyga model językowy; pomiar na 12 fragmentach: 13,0% wobec 13,0% GigaAM+Demucs — bez zysku, 2,6× wolniej; zostaje jako opcja eksperymentalna, domyślne ustawienie bez zmian — 21.11
- [x] Etap 2, pomiary na 12 fragmentach (błąd słów; obecne GigaAM+Demucs 13,1%): B2 GigaAM v3 RNNT 11,9% (bez interpunkcji), Multilingual 14,8%; B3 cięcie w ciszy 13,5%; B5 Silero v6 13,5%; B6 BS-RoFormer 14,1% (3,6× wolniej), domieszka oryginału 14,9% — bez zysku, zostają jako opcje; B4 nowy podział na kwestie (normy Netflix PL) w użyciu, błąd bez zmian; B7 pewność kwestii w raporcie jakości; B9 RNNT + interpunkcja z modelu językowego 12,0% (opcja GigaAM-RNNT); B10 wyrównanie czasów: Whisper 275→153 ms; B11 Whisper w partiach 12,1% i 9× szybciej — domyślne dla Whispera razem z wyrównaniem; Parakeet 20% (słaby dla rosyjskiego) — 21.11
- [x] B8 wersja 2: dwa pełne zapisy (GigaAM + Whisper w partiach z wyrównaniem), model językowy wybiera lepszy na każdą minutę — 10,8% wobec 13,2%, czasy 123 ms, mniej widm; domyślny silnik „GigaAM+Whisper” (ok. 3,5× dłuższe rozpoznawanie) — 21.11
- [x] B12: rozpoznawanie mówców pyannote community-1 (token HF_TOKEN), myślniki dialogowe, płeć z wysokości głosu w poleceniu dla tłumacza; próba Gorkij 53: 6 mówców (1 K, 5 M) — 21.11
- [x] Etap 3: C3 płeć z form rosyjskich + pole plec w słowniczku; C4 polskie normy zapisu; C5 MetricX-24 XL (dobre 2,0–2,5, złe 13) z naprawą ≥ 8; C7 tłumaczenie zdaniami przy rozpoznanej mowie (MetricX 5,21 wobec 5,59) — 21.11
- [x] C6: gemma4:31b 4,81 vs MiLMMT-46-12B 4,58 (MetricX, 360 kwestii), MiLMMT 2× szybszy, ale bez kontekstu i słowniczka, a MetricX może go faworyzować — domyślny model bez zmian; wersja 1.5 — 21.11

## Do decyzji (Ty)

- [ ] **Z4** Licencja repozytorium na GitHubie (np. MIT) — bez niej inni nie mogą legalnie używać kodu — 2 min Twoje
- [ ] **Z5** Wgranie nowej wersji na Unraid, gdy serwer będzie włączony (potem zainstaluj.sh w terminalu Unraid) — 5 min Twoje

## Etap 1 – szybkie poprawki, bezpieczeństwo danych

_ok. 1 dzień mojej pracy, bez pomiarów_

- [x] **A1** Filtr halucynacji Whispera: czarna lista RU/EN („Субтитры сделал…”, „Продолжение следует”), usuwanie powtórzeń, odrzucanie przy niskiej pewności — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A2** Podmiana MKV zachowuje flagi ścieżek (forced, hearing_impaired); check_remux porównuje flagi, załączniki i tagi — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A3** Zapis atomowy .tmp + os.replace dla napisów; metadane silnik/model/język przy rozpoznanym SRT — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A4** Opcjonalna kopia .orig.mkv przez N dni (po decyzji Z3) — S, ok. 1 h — zrobione 21.11 16:02
- [x] **A5** Zwalnianie modeli rozpoznawania mowy z karty przed tłumaczeniem; keep_alive=0 w Ollamie przed rozpoznawaniem — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A6** Limity czasu ffmpeg/ffprobe i procesu audio (Stop zawsze działa), bez ponawiania przy braku pamięci — S/M, 1–3 h — zrobione 21.11 15:34
- [x] **A7** Pełny ślad błędu do dziennik.txt — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A8** Wykrywanie języka mowy z 6–8 okien rozłożonych po filmie (dziś realnie decyduje ~30 s), kanał centralny, głosowanie — S/M, 1–3 h — zrobione 21.11 15:34
- [x] **A9** Słowniczek imion w prompcie tłumaczenia i jako podpowiedzi dla Whispera — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A10** Schemat JSON (format) w Ollamie, num_ctx 16k, większe partie — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A11** Tempo czytania wg norm Netflix PL: ostrzeżenie powyżej 17 zn/s, błąd powyżej 20 — S, ok. 1 h — zrobione 21.11 15:34
- [x] **A12** 5.1: kanał centralny także przy separacji głosu; dziennik mówi, której ścieżki i kanałów użyto — S, ok. 1 h — zrobione 21.11 15:34

## Etap 2 – perfekcyjne rozpoznawanie mowy

_ok. 4–6 dni mojej pracy; każdą zmianę mierzymy na B1_

- [x] **B1** Zestaw wzorcowy 5–10 klipów (muzyka, szept, akcja, piosenka, szybki dialog) i pomiar WER/CER, czasów i halucynacji — M, ok. 4 h — zrobione 21.11 16:58
- [x] **B2** GigaAM: przejście na wariant v3 RNNT (8,3% WER zamiast 9,1%), porównanie z GigaAM Multilingual 600M — S, ok. 1 h — zrobione 21.11 17:38
- [x] **B3** Cięcie mowy w ciszy (500–700 ms, maks. 25–28 s) zamiast twardo co 20 s w środku słowa — M, ok. 4 h — zrobione 21.11 17:54
- [x] **B4** Nowy podział na kwestie: koniec zdania lub pauza, 42 zn × 2 linie, min. 20 klatek, maks. 7 s, przerwa 2 klatki, ogon +0,4 s — M, ok. 4 h — zrobione 21.11 17:54
- [x] **B5** Silero VAD v6; próba TEN VAD na cichych i szeptanych kwestiach — S/M, 1–3 h — zrobione 21.11 17:54
- [x] **B6** Separacja BS/Mel-RoFormer zamiast htdemucs — do wykrywania mowy i podziału; rozpoznawanie na oryginale albo wokalu z domieszką (A/B na B1) — M, ok. 4 h — zrobione 21.11 17:54
- [x] **B7** Pewność rozpoznania dla każdej kwestii; „niepewne rozpoznanie” w raporcie jakości — M, ok. 4 h — zrobione 21.11 17:54
- [x] **B8** Dwa modele naraz (GigaAM RNNT + Whisper/GigaAM-ML); przy niezgodności LLM wybiera wersję; tekst tylko z Whispera przy ciszy = halucynacja — L, 1–2 dni — zrobione 21.11 18:48
- [x] **B9** Interpunkcja: wariant e2e-RNNT albo LLM na tekście RNNT — M, ok. 4 h — zrobione 21.11 18:19
- [x] **B10** Dokładne czasy słów: Qwen3-ForcedAligner (RU/EN), MMS (UK) — M, ok. 4 h — zrobione 21.11 18:19
- [x] **B11** Whisper w partiach (batch 16, beam 5) po A1; dla angielskiego próba Parakeet-TDT-0.6B-v3 — M, ok. 4 h — zrobione 21.11 18:19
- [x] **B12** Rozpoznawanie mówców (pyannote community-1): myślniki dialogowe, bez sklejania mówców, płeć mówiącego — L, 1–2 dni — zrobione 21.11 18:47

## Etap 3 – tłumaczenie

_ok. 2–3 dni mojej pracy_

- [x] **C1** Kontekst: 3–4 kwestie następne + ostatnie 5 par oryginał→tłumaczenie — M, ok. 4 h — zrobione 21.11 16:04
- [x] **C2** Streszczenie filmu/odcinka generowane raz i dodawane do każdej partii — S, ok. 1 h — zrobione 21.11 16:06
- [x] **C3** Płeć mówiących (zrobiłem/zrobiłam): wnioskowanie z form rosyjskich + pole w słowniczku — S, ok. 1 h — zrobione 21.11 19:30
- [x] **C4** Polskie normy zapisu: „…”, cudzysłowy „”, myślniki dialogowe, godziny 14.15 — S, ok. 1 h — zrobione 21.11 19:30
- [x] **C5** Ocena jakości bez wzorca: MetricX-24 + sprawdzenie języka wyniku; najgorsze kwestie do naprawy — M, ok. 4 h — zrobione 21.11 19:40
- [x] **C6** Porównanie modeli na Twoich filmach: gemma4:31b kontra MiLMMT-46-12B — M, ok. 4 h — zrobione 21.11 19:50
- [x] **C7** Tłumaczenie pełnymi zdaniami z rozpoznanej mowy i podział z powrotem wg czasu — L, 1–2 dni — zrobione 21.11 19:55

## Etap 4 – utrzymanie

_ok. 2–3 dni mojej pracy_

- [ ] **D1** Przypięte wersje pakietów audio_env (uv pip compile, hashe) — S, ok. 1 h
- [ ] **D2** Testy automatyczne (pytest) + test na 30-sekundowym MKV — M, ok. 4 h
- [ ] **D3** Kontekst pliku zamiast stanu globalnego; ta sama ścieżka audio dla lektora i rozpoznawania — M, ok. 4 h
- [ ] **D4** Podział programu na moduły; kopia dla Unraid generowana skryptem — L, 1–2 dni
- [ ] **D5** Składanie MKV przez mkvmerge zamiast ffmpeg — M, ok. 4 h
