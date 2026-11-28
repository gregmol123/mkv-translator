#!/usr/bin/env python3
"""Tłumaczy napisy wbudowane w pliki MKV (rosyjskie, angielskie, ukraińskie) na język polski.

Wynik: plik <nazwa>.pl.srt obok filmu, a z opcją --mux polska ścieżka napisów
trafia wprost do pliku MKV (z --keep-original: do kopii <nazwa>.pl.mkv). Opcjonalnie: napisy rozpoznane
z dźwięku (Whisper) i ścieżka polskiego lektora (Piper albo XTTS-v2) — te funkcje
wykonuje audio_tools.py uruchamiany w środowisku audio_env.
"""
import argparse
import copy
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
import textwrap
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------- język okna i komunikatów
# teksty w kodzie są po polsku i służą za klucze; ui_en.py zawiera ich odpowiedniki angielskie
UI_LANGS = {"pl": "Polski", "en": "English"}
try:
    from ui_en import EN
except ImportError:
    EN = {}
ui = {"lang": os.environ.get("MKV_UI_LANG", "pl")}
EDGE_RE = re.compile(r"(\s*)(.*?)(\s*)$", re.S)


def set_ui_lang(lang):
    ui["lang"] = lang if lang in UI_LANGS else "pl"
    os.environ["MKV_UI_LANG"] = ui["lang"]     # proces audio_tools.py dziedziczy język komunikatów


def tr(text, *args):
    """Tekst dla użytkownika w wybranym języku; szablon z {0}, {1}... jak w str.format.
    Odstępy i znaki nowej linii na brzegach nie są częścią klucza w słowniku."""
    if ui["lang"] != "pl":
        lead, core, trail = EDGE_RE.match(text).groups()
        text = lead + EN.get(core, core) + trail
    return text.format(*args) if args else text


def untr(text, keys):
    """Klucz (tekst polski) spośród keys dla tekstu pokazanego w oknie — odwrotność tr() dla list wyboru."""
    return next((k for k in keys if tr(k) == text), text)


TEXT_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
SUB_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}   # napisy w osobnych plikach obok filmu
# filmy w innych formatach niż MKV: napisy trafiają do pliku <nazwa>.pl.srt obok filmu
VIDEO_EXTS = {".mkv", ".avi", ".mp4", ".m4v", ".ts", ".m2ts", ".mov", ".wmv", ".mpg", ".mpeg", ".vob", ".webm", ".flv"}
# języki źródłowe: kod -> (nazwa angielska dla modelu, nazwa w dopełniaczu, znaczniki ścieżek, słowa w nazwach plików)
LANGS = {"ru": ("Russian", "rosyjskiego", {"rus", "ru"}, {"ru", "rus", "russian", "рус", "русский"}),
         "en": ("English", "angielskiego", {"eng", "en"}, {"en", "eng", "english"}),
         "uk": ("Ukrainian", "ukraińskiego", {"ukr", "uk"}, {"uk", "ukr", "ukrainian", "укр"})}
SOURCE_CHOICES = {"automatycznie": "auto", "rosyjski": "ru", "angielski": "en", "ukraiński": "uk"}
POL_TAGS = {"pol", "pl"}
# języki docelowe: kod -> (nazwa dla modelu, znaczniki ścieżek, słowa w nazwach plików i tytułach ścieżek,
#                          tytuł nowej ścieżki napisów, kod języka w MKV, przymiotnik do komunikatów)
TARGETS = {"pl": ("Polish", POL_TAGS, {"pl", "pol", "polish", "polski", "polskie"}, "Polski", "pol", "polskie"),
           "en": ("English", {"eng", "en"}, {"en", "eng", "english", "angielski", "angielskie"}, "English", "eng",
                  "angielskie")}
TARGET_CHOICES = {"polski": "pl", "angielski": "en"}
OUT = {"code": "pl"}                          # język wyniku bieżącego pliku (ustawia process() z opcji --target)


def target_tags():
    return TARGETS[OUT["code"]][1]


def target_words():
    return TARGETS[OUT["code"]][2]


def target_adj():
    """Przymiotnik do komunikatów: "polskie"/"angielskie" (przetłumaczony na język okna)."""
    return tr(TARGETS[OUT["code"]][5])


def source_order(source):
    """Języki źródłowe w kolejności pierwszeństwa, bez języka docelowego (angielski nie tłumaczy się na angielski)."""
    return [l for l in (list(LANGS) if source == "auto" else [source]) if l != OUT["code"]]
LINE_WIDTH = 42          # maksymalna długość linii napisów
GOOGLE_CHAR_LIMIT = 4500
CLAUDE_MODEL = "claude-opus-5-5"
VERSION = "1.8"                            # numer wydania (GitHub Releases) — w tytule okna, dzienniku i --version
OLLAMA_MODEL = "gemma4:31b"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

TIMING_RE = re.compile(r"(\d+:\d+:\d+[,.]\d+)\s*-->\s*(\d+:\d+:\d+[,.]\d+)(.*)")
TAG_RE = re.compile(r"<[^>]+>")
ASS_RE = re.compile(r"\{\\[^}]*\}")
DIALOG_RE = re.compile(r"^\s*[-–—]")

SYSTEM_PROMPT = """Jesteś zawodowym tłumaczem napisów filmowych z {source} na polski.

Dostajesz obiekt JSON, w którym klucze to numery kwestii, a wartości to kwestie do przetłumaczenia. \
Zwróć wyłącznie obiekt JSON z dokładnie tymi samymi kluczami, w którym wartości to polskie tłumaczenia.

Zasady:
- naturalna, żywa polszczyzna mówiona, zwięzła jak w napisach kinowych; nie tłumacz dosłownie idiomów,
- zachowaj ton, rejestr i wulgaryzmy oryginału,
- imiona i nazwy własne zapisuj w polskiej transkrypcji (np. Сергей -> Siergiej),
- płeć mówiącego i osób, o których mowa, odczytuj z form oryginału (сделал/сделала, я рад/рада, сам/сама)
i zachowuj w polskim (zrobiłem/zrobiłam, cieszę się, sam/sama); gdy oryginał nie rozstrzyga, trzymaj się
wcześniejszych kwestii tej samej osoby,
- zachowaj myślnik na początku kwestii dialogowej, jeśli jest w oryginale,
- każda kwestia to osobna wartość: nie łącz, nie dziel i nie pomijaj kwestii,
- pola pomocnicze (jeśli występują) są tylko do wglądu — nie tłumacz ich i nie zwracaj: "kontekst" to kwestie \
tuż przed tą partią, "dalej" to kwestie tuż po niej (zdanie bywa rozcięte między kwestie), a "przetlumaczone" to \
wcześniejsze pary oryginał -> tłumaczenie — trzymaj się ich formy zwracania się (ty/Pan), rodzaju mówiących i pisowni,
- pole "plec" (jeśli występuje) podaje płeć osoby mówiącej daną kwestię: K = kobieta, M = mężczyzna — użyj jej w formach
o sobie samym (zrobiłam/zrobiłem, byłam/byłem, sama/sam); to oszacowanie z głosu, więc wyraźne formy w oryginale mają
pierwszeństwo.
Odpowiedz samym JSON-em, bez komentarzy i bez bloków kodu."""

SHORTEN_PROMPT = """Jesteś redaktorem polskich napisów filmowych. Dostajesz obiekt JSON: klucze to numery, \
wartości to obiekty z polem "tekst" (polski napis) i "max_znakow". Każdy napis skróć tak, żeby miał najwyżej \
max_znakow znaków: zachowaj sens i ton, usuń zbędne słowa, powtórzenia i wtrącenia, używaj krótszych synonimów. \
Nie dodawaj niczego od siebie. Zwróć wyłącznie obiekt JSON z tymi samymi kluczami, a jako wartości — skrócone napisy."""

SYSTEM_PROMPT_EN = """You are a professional subtitle translator from {source} into English.

You receive a JSON object whose keys are cue numbers and whose values are subtitle cues to translate. \
Return only a JSON object with exactly the same keys, whose values are the English translations.

Rules:
- natural, idiomatic spoken English, concise like cinema subtitles; do not translate idioms literally,
- keep the tone, register and profanity of the original,
- write names in standard English transliteration (e.g. Сергей -> Sergei),
- infer speakers' and referents' gender from the source forms (сделал/сделала, рад/рада) and keep pronouns consistent,
- keep a leading dash of a dialogue line if the original has one,
- every cue is a separate value: do not merge, split or skip cues,
- helper fields (if present) are for reference only — do not translate or return them: "kontekst" holds the cues \
just before this batch, "dalej" the cues just after it (a sentence may be split across cues), and "przetlumaczone" \
earlier source -> translation pairs — keep their forms of address, speakers' genders and spellings consistent,
- the "plec" field (if present) gives the speaker's gender for a cue: K = female, M = male (estimated from the voice;
explicit forms in the source take precedence).
Reply with JSON only, no comments and no code blocks."""

SHORTEN_PROMPT_EN = """You edit English film subtitles. You receive a JSON object: keys are numbers, values are \
objects with a "tekst" field (an English subtitle) and "max_znakow". Shorten every subtitle to at most max_znakow \
characters: keep the meaning and tone, drop filler words, repetitions and asides, use shorter synonyms. \
Add nothing. Return only a JSON object with the same keys and the shortened subtitles as values."""

# oficjalny format zapytania modeli TranslateGemma
TRANSLATEGEMMA_PROMPT = """You are a professional {name} ({code}) to {tname} ({tcode}) translator. Your goal is to \
accurately convey the meaning and nuances of the original {name} text while adhering to {tname} grammar, \
vocabulary, and cultural sensitivities.
Produce only the {tname} translation, without any additional explanations or commentary. Please translate the \
following {name} text into {tname}:


{text}"""


def gemma_prompt(text, lang):
    return TRANSLATEGEMMA_PROMPT.format(name=LANGS[lang][0], code=lang, text=text,
                                        tname=TARGETS[OUT["code"]][0], tcode=OUT["code"])


def system_prompt(lang, names=None, summary=None):
    """Polecenie dla modelu; names: ustalona pisownia imion serialu (słowniczek) jako {rdzeń z oryginału: zapis};
    summary: streszczenie filmu/odcinka (kto jest kim, płeć, relacje) jako tło."""
    if OUT["code"] == "pl":
        text = SYSTEM_PROMPT.format(source=LANGS[lang][1])
        head = "Ustalona pisownia imion i nazw w tym serialu (rdzeń z oryginału -> zapis; odmieniaj po polsku):"
        about = "Streszczenie całego odcinka — tło do rozumienia kwestii, nie tłumacz go:"
    else:
        text = SYSTEM_PROMPT_EN.format(source=LANGS[lang][0])
        head = "Established spelling of names in this series (source stem -> spelling):"
        about = "Summary of the whole episode — background for understanding the cues, do not translate it:"
    if summary:
        text += "\n\n" + about + "\n" + summary
    if names:
        text += "\n\n" + head + "\n" + "\n".join(f"- {k} -> {v}" for k, v in names.items())
    return text


SUMMARY_PROMPT = {
    "pl": "Poniżej są napisy z całego odcinka filmu lub serialu (wybrane kwestie, po kolei). Napisz po polsku "
          "zwięzłe streszczenie (6–10 zdań) jako tło dla tłumacza: o czym jest odcinek, kim są postaci (imiona, "
          "płeć, zawód, relacje), kto jest z kim na \"ty\", a kto na \"Pan/Pani\", miejsce i czas akcji. Tylko "
          "streszczenie, bez wstępu.",
    "en": "Below are subtitles from a whole film or TV episode (selected cues, in order). Write a concise summary "
          "in English (6–10 sentences) as background for a translator: what the episode is about, who the "
          "characters are (names, gender, job, relationships), how they address each other, setting and time. "
          "Summary only, no preamble.",
}
SUMMARY_CHARS = 12000                         # tyle znaków napisów trafia do streszczenia (mieści się w num_ctx)


GLOSSARY_PROMPT_LIMIT = 80                   # tyle imion najwyżej trafia do polecenia (krótszy prompt = szybciej)


def glossary_prompt_names(path):
    """Imiona do polecenia: zablokowane przez użytkownika i te, które w serialu padły co najmniej 2 razy."""
    names = load_glossary(path) if path else {}
    picked = [(k, e) for k, e in names.items()
              if e.get("pisownia") and (e.get("zablokuj") or sum(e.get("warianty", {}).values()) >= 2)]
    picked.sort(key=lambda ke: (not ke[1].get("zablokuj"), -sum(ke[1].get("warianty", {}).values())))
    # pole "plec" (K/M) wpisane przez użytkownika w słowniczku — model wie, czy postać to kobieta czy mężczyzna
    return {k: e["pisownia"] + (f" ({e['plec']})" if e.get("plec") in ("K", "M") else "")
            for k, e in picked[:GLOSSARY_PROMPT_LIMIT]}


# częste słowa angielskie, których nie ma w polszczyźnie (bez "to", "do", "i" — są też polskie)
ENGLISH_WORDS = {"the", "you", "and", "is", "it", "that", "what", "of", "in", "me", "my", "we", "don't",
                 "have", "this", "not", "your", "be", "are", "was", "he", "she", "i'm", "it's", "with", "for"}


def detect_lang(texts):
    """Język tekstu napisów: "ru", "uk", "en" albo None (np. polski, inny, za mało tekstu)."""
    text = " ".join(texts[:300])
    if cyrillic_ratio(text) >= 0.3:
        cyr = sum("Ѐ" <= c <= "ӿ" for c in text)
        # litery tylko ukraińskie (і ї є ґ) — w rosyjskim ich nie ma
        return "uk" if sum(c in "іїєґІЇЄҐ" for c in text) > 0.01 * cyr else "ru"
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) >= 20 and sum(w in ENGLISH_WORDS for w in words) / len(words) > 0.08:
        return "en"
    return None


def still_source(text, lang):
    """Czy tłumaczenie zostało w języku źródłowym (model nie przetłumaczył kwestii)."""
    if lang in ("ru", "uk"):
        return cyrillic_ratio(text) > 0.5
    words = re.findall(r"[a-z']+", text.lower())
    return len(words) >= 3 and sum(w in ENGLISH_WORDS for w in words) / len(words) > 0.25 \
        and not re.search("[ąćęłńśźż]", text.lower())


class FatalError(Exception):
    pass


class ToolDown(FatalError):
    """Awaria narzędzia, a nie pliku (Ollama, środowisko audio) — kolejka staje, plik czeka na następną próbę."""


class OllamaDown(ToolDown):
    """Ollama nie odpowiada (np. komputer z kartą jest wyłączony)."""


# błędy środowiska audio_env, które dotyczą każdego kolejnego pliku, a nie tylko bieżącego
ENV_ERROR_RE = re.compile(r"ModuleNotFoundError|PackageNotFoundError|ImportError|DLL load failed|"
                          r"out of memory|CUDA error|cudaErrorMemoryAllocation", re.IGNORECASE)


class Cancelled(Exception):
    pass


cancel = threading.Event()   # ustawiane przyciskiem Stop w oknie kolejki

# folder programu: obok leżą audio_tools.py i środowisko audio_env
APP_DIR = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
PIPER_VOICES = ["pl_PL-darkman-medium", "pl_PL-gosia-medium", "pl_PL-mc_speech-medium"]
WHISPER_MODELS = ["large-v3", "large-v3-turbo", "medium", "small"]   # od najdokładniejszego
XTTS_VOICES = ["Damien Black", "Viktor Menelaos", "Aaron Dreschner", "Ana Florence", "Claribel Dervla"]


@dataclass
class Cue:
    start: str
    end: str
    extra: str   # ewentualne współrzędne po znaczniku czasu
    text: str


# ---------------------------------------------------------------- ffmpeg

def run(cmd, timeout=None):
    """ffmpeg/ffprobe z limitem czasu: zawieszony dysk sieciowy albo uszkodzony plik nie blokuje kolejki na zawsze."""
    if timeout is None:
        timeout = 300 if cmd[0] == "ffprobe" else 6 * 3600
    try:
        # CREATE_NO_WINDOW: w wersji okienkowej ffmpeg nie może otwierać własnych konsol
        return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise FatalError(tr('{0} nie skończył pracy w {1} min — przerwany (plik albo dysk nie odpowiada)',
                            cmd[0], round(timeout / 60)))


def probe_streams(mkv, kind):
    res = run(["ffprobe", "-v", "error", "-select_streams", kind, "-show_streams", "-of", "json", str(mkv)])
    if res.returncode != 0:
        raise FatalError(tr('ffprobe nie odczytał pliku: {0}', res.stderr.strip()))
    return json.loads(res.stdout or "{}").get("streams", [])


def subtitle_streams(mkv):
    return probe_streams(mkv, "s")


def main_audio(audio, source="ru"):
    """(numer ścieżki audio z oryginalną mową, jej język albo None, gdy nieoznaczona).

    Najpierw ścieżka oznaczona językiem źródłowym (przy "auto": rosyjskim, angielskim, ukraińskim), potem
    pierwsza niepolska.
    """
    order = list(LANGS) if source == "auto" else [source]
    for lang in order:
        found = next((i for i, a in enumerate(audio) if lang_of(a) in LANGS[lang][2]), None)
        if found is not None:
            return found, lang
    found = next((i for i, a in enumerate(audio) if lang_of(a) not in target_tags()), 0)
    return found, None


HELPER_SILENCE = 30 * 60                     # tyle wolno procesowi audio milczeć (wczytywanie modeli to minuty)
HELPER_END = "\x1eKONIEC"                    # znacznik końca polecenia w trybie "serve" (jak w audio_tools.py)
_helper = {"proc": None, "errors": None}     # stały proces audio_tools.py serve — modele zostają w pamięci karty
_helper_lock = threading.Lock()


def helper_command():
    python = APP_DIR / "audio_env" / "Scripts" / "python.exe"
    script = APP_DIR / "audio_tools.py"
    if not python.exists() or not script.exists():
        raise FatalError(tr('funkcje audio wymagają audio_tools.py i środowiska audio_env obok programu — uruchom install_audio.bat'))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "COQUI_TOS_AGREED": "1"}
    return [str(python), str(script)], env


def stop_helper():
    proc = _helper["proc"]
    _helper["proc"] = None
    if proc and proc.poll() is None:
        proc.kill()


def free_helper():
    """Zwalnia pamięć karty zajętą przez modele audio (proces zostaje) — przed tłumaczeniem dużym modelem."""
    proc = _helper["proc"]
    if proc and proc.poll() is None:
        try:
            run_helper(["free"])
        except (FatalError, ToolDown):
            stop_helper()


def ollama_unload():
    """Wyładowuje modele Ollamy z karty (keep_alive=0), żeby rozpoznawanie mowy miało całą pamięć.
    Tylko dla Ollamy na tym komputerze — zdalna nie zajmuje tej karty."""
    if not ollama_is_local():
        return
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/ps", timeout=5) as r:
            loaded = [m["name"] for m in json.load(r).get("models", [])]
        for name in loaded:
            body = json.dumps({"model": name, "keep_alive": 0}).encode("utf-8")
            req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=60).read()
    except (urllib.error.URLError, OSError, ValueError):
        pass                                  # Ollama wyłączona albo starsza — nic do zwolnienia


def run_helper(args):
    """Uruchamia polecenie audio_tools.py i przekazuje jego komunikaty na bieżąco.

    Rozpoznawanie mowy i lektor idą przez stały proces (tryb "serve"): Demucs, GigaAM i głosy wczytują się
    raz na całą kolejkę zamiast przy każdym pliku. MKV_AUDIO_PERSIST=0 wyłącza ten tryb.
    """
    if args[0] in ("transcribe", "tts", "detect", "ocr", "free", "qe") and os.environ.get("MKV_AUDIO_PERSIST", "1") != "0":
        with _helper_lock:
            return run_persistent(args)
    base, env = helper_command()
    proc = subprocess.Popen([*base, *map(str, args)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    errors = []
    reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
    reader.start()
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    while True:
        chunk = proc.stdout.read1(4096)
        if not chunk:
            break
        print(decoder.decode(chunk).replace("\r\n", "\n"), end="", flush=True)
        if cancel.is_set():
            proc.kill()
            raise Cancelled
    proc.wait()
    reader.join(5)
    if proc.returncode != 0:
        text = (errors[0] if errors else b"").decode("utf-8", "replace").strip()
        lines = text.splitlines()
        error = ToolDown if ENV_ERROR_RE.search(text) else FatalError
        raise error(tr('narzędzie audio zakończyło się błędem: {0} (pełny opis w audio_blad.txt)', lines[-1] if lines else proc.returncode))


def run_persistent(args):
    import codecs
    import collections
    proc = _helper["proc"]
    if proc is None or proc.poll() is not None:
        base, env = helper_command()
        proc = subprocess.Popen([*base, "serve"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        errors = collections.deque(maxlen=80)   # stderr trzeba czytać na bieżąco, inaczej proces stanie
        threading.Thread(target=lambda: errors.extend(proc.stderr), daemon=True).start()
        # stdout czyta osobny wątek: główna pętla co sekundę sprawdza Stop, nawet gdy proces długo milczy
        chunks = queue.Queue()

        def pump(out=proc.stdout, q=chunks):
            while True:
                chunk = out.read1(4096)
                q.put(chunk)
                if not chunk:
                    return
        threading.Thread(target=pump, daemon=True).start()
        _helper.update(proc=proc, errors=errors, chunks=chunks)
    _helper["errors"].clear()                 # opis błędu ma dotyczyć tego polecenia, nie poprzednich
    try:
        proc.stdin.write((json.dumps([str(a) for a in args]) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except OSError:
        stop_helper()
        raise ToolDown(tr('proces narzędzi audio przestał działać — spróbuj ponownie'))
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    quiet_since = time.time()
    while True:
        try:
            chunk = _helper["chunks"].get(timeout=1)
        except queue.Empty:
            if cancel.is_set():
                stop_helper()
                raise Cancelled
            if time.time() - quiet_since > HELPER_SILENCE:
                stop_helper()
                raise ToolDown(tr('proces narzędzi audio nie odpowiada od {0} min — zatrzymany', HELPER_SILENCE // 60))
            continue
        quiet_since = time.time()
        if not chunk:                         # proces padł (np. błąd przy imporcie bibliotek)
            stop_helper()
            time.sleep(0.5)                   # wątek czytający stderr dobiera ostatnie wiersze
            text = b"".join(_helper_errors_tail()).decode("utf-8", "replace").strip()
            last = text.splitlines()[-1] if text else tr('proces zakończył się bez opisu')
            error = ToolDown if ENV_ERROR_RE.search(text) or not text else FatalError
            raise error(tr('narzędzie audio zakończyło się błędem: {0} (pełny opis w audio_blad.txt)', last))
        pending += decoder.decode(chunk)
        mark = pending.find(HELPER_END)
        if mark >= 0 and "\n" in pending[mark:]:
            print(pending[:mark].replace("\r\n", "\n").rstrip("\n"), end="", flush=True)
            result = pending[mark + len(HELPER_END):].split("\n", 1)[0].strip()
            code, _, message = result.partition(" ")
            if code == "0":
                print()
                return
            text = message + "\n" + b"".join(_helper_errors_tail()).decode("utf-8", "replace")
            error = ToolDown if ENV_ERROR_RE.search(text) else FatalError
            raise error(tr('narzędzie audio zakończyło się błędem: {0} (pełny opis w audio_blad.txt)', message or '?'))
        cut = pending.rfind("\x1e")           # początek znacznika może przyjść w następnym kawałku
        shown, pending = (pending[:cut], pending[cut:]) if cut >= 0 else (pending, "")
        print(shown.replace("\r\n", "\n"), end="", flush=True)
        if cancel.is_set():
            stop_helper()
            raise Cancelled


def _helper_errors_tail():
    return list(_helper.get("errors") or ())[-20:]


def lang_of(stream):
    # telegazeta w TS podaje język stron po przecinku ("rus,rus") — liczy się pierwszy
    return (stream.get("tags", {}).get("language") or "").lower().split(",")[0]


def extract_srt(mkv, stream_index, out_path):
    res = run(["ffmpeg", "-v", "error", "-y", "-i", str(mkv), "-map", f"0:{stream_index}",
               "-c:s", "srt", str(out_path)])
    if res.returncode != 0:
        raise FatalError(tr('ffmpeg nie wyodrębnił napisów: {0}', res.stderr.strip()))


def mux(mkv, out_path, srt=None, sub_count=0, voice=None, audio=(), replace_audio=False,
        drop_subs=(), drop_audio=(), orig=None):
    """Tworzy kopię MKV z dodanymi polskimi napisami (srt) i/lub ścieżką lektora (voice).

    drop_subs / drop_audio: numery ścieżek z poprzedniego przebiegu, które nowe mają zastąpić.
    orig: ścieżka dźwiękowa, z której rozpoznano mowę — lektor ścisza właśnie ją (None = wybór jak przy rozpoznawaniu).
    """
    if mkvmerge_exe():
        return mux_mkvmerge(mkv, out_path, srt, voice, audio, replace_audio, drop_subs, drop_audio, orig)
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(mkv)]
    # -max_interleave_delta 0: bez tego w plikach z okładką (obraz "attached pic") ffmpeg zapisuje
    # wszystkie napisy jednym blokiem na końcu pliku i odtwarzacze gubią je po przewinięciu
    maps, opts, inputs = ["-map", "0"], ["-c", "copy", "-max_interleave_delta", "0"], 1
    for kind, dropped in (("s", drop_subs), ("a", drop_audio)):
        for i in dropped:
            maps += ["-map", f"-0:{kind}:{i}"]
    if srt:
        cmd += ["-i", str(srt)]
        n = str(sub_count - len(drop_subs))  # indeks nowej ścieżki wśród napisów
        maps += ["-map", f"{inputs}:0"]
        # dotychczasowym ścieżkom zdejmujemy tylko "domyślną" — "wymuszone" i "dla niesłyszących" zostają
        opts += [x for i in range(int(n)) for x in (f"-disposition:s:{i}", "-default")]
        opts += [f"-disposition:s:{n}", "0" if voice else "default",
                 f"-metadata:s:s:{n}", f"language={TARGETS[OUT['code']][4]}",
                 f"-metadata:s:s:{n}", f"title={TARGETS[OUT['code']][3]}"]
        inputs += 1
    if voice:
        cmd += ["-i", str(voice)]
        # lektor na przyciszonym oryginale: oryginał cichnie, gdy lektor mówi (sidechain)
        cmd += ["-filter_complex",
                f"[{inputs}:a]aresample=48000,volume=1.5,asplit=2[v1][v2];"
                f"[0:a:{orig if orig is not None else main_audio(audio, 'auto')[0]}]aresample=48000,"
                "aformat=channel_layouts=stereo[orig];"
                "[orig][v1]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=500[duck];"
                "[duck][v2]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[lektor]"]
        if replace_audio:
            maps += ["-map", "-0:a"]
        n = "0" if replace_audio else str(len(audio) - len(drop_audio))
        maps += ["-map", "[lektor]"]
        opts += [x for i in range(int(n)) for x in (f"-disposition:a:{i}", "-default")]
        opts += [f"-c:a:{n}", "aac", f"-b:a:{n}", "192k", f"-disposition:a:{n}", "default",
                 f"-metadata:s:a:{n}", "language=pol", f"-metadata:s:a:{n}", "title=Polski lektor"]
    res = run(cmd + maps + opts + [str(out_path)])
    if res.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise FatalError(tr('ffmpeg nie złożył pliku MKV: {0}', res.stderr.strip()))



def mkvmerge_exe():
    """mkvmerge z MKVToolNix (PATH albo domyślny folder instalacji); MKV_MKVMERGE=0 wymusza ffmpeg."""
    if os.environ.get("MKV_MKVMERGE") == "0":
        return None
    found = shutil.which("mkvmerge")
    if not found:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            exe = Path(base or "-") / "MKVToolNix" / "mkvmerge.exe"
            if exe.is_file():
                return str(exe)
    return found


def lektor_track(mkv, voice, audio, orig, out):
    """Lektor na przyciszonym oryginale (oryginał cichnie, gdy lektor mówi) jako osobny plik AAC dla mkvmerge."""
    res = run(["ffmpeg", "-v", "error", "-y", "-i", str(mkv), "-i", str(voice), "-filter_complex",
               f"[1:a]aresample=48000,volume=1.5,asplit=2[v1][v2];"
               f"[0:a:{orig if orig is not None else main_audio(audio, 'auto')[0]}]aresample=48000,"
               "aformat=channel_layouts=stereo[orig];"
               "[orig][v1]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=500[duck];"
               "[duck][v2]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[lektor]",
               "-map", "[lektor]", "-c:a", "aac", "-b:a", "192k", str(out)])
    if res.returncode != 0:
        out.unlink(missing_ok=True)
        raise FatalError(tr('ffmpeg nie złożył pliku MKV: {0}', res.stderr.strip()))


def mux_mkvmerge(mkv, out_path, srt, voice, audio, replace_audio, drop_subs, drop_audio, orig):
    """D5: składanie przez mkvmerge — zachowuje rozdziały, załączniki, tagi, flagi i nazwy ścieżek w natywnej
    postaci Matroski (ffmpeg przepisuje kontener i bywa, że gubi część metadanych)."""
    exe = mkvmerge_exe()
    info = json.loads(run([exe, "-J", str(mkv)], timeout=300).stdout or "{}")
    tracks = {"subtitles": [], "audio": []}
    for t in info.get("tracks", []):
        if t.get("type") in tracks:
            tracks[t["type"]].append(t["id"])      # kolejność jak w ffprobe: n-ta ścieżka danego rodzaju
    cmd = [exe, "--quiet", "-o", str(out_path)]
    keep_subs = [tid for i, tid in enumerate(tracks["subtitles"]) if i not in set(drop_subs)]
    keep_audio = [] if voice and replace_audio else         [tid for i, tid in enumerate(tracks["audio"]) if i not in set(drop_audio)]
    if drop_subs:
        cmd += ["-s", ",".join(map(str, keep_subs))] if keep_subs else ["-S"]
    if voice and replace_audio or drop_audio:
        cmd += ["-a", ",".join(map(str, keep_audio))] if keep_audio else ["-A"]
    # dotychczasowym ścieżkom zdejmujemy tylko "domyślną" — "wymuszone" i "dla niesłyszących" zostają
    if srt:
        cmd += [x for tid in keep_subs for x in ("--default-track-flag", f"{tid}:0")]
    if voice:
        cmd += [x for tid in keep_audio for x in ("--default-track-flag", f"{tid}:0")]
    cmd.append(str(mkv))
    if srt:
        cmd += ["--language", f"0:{TARGETS[OUT['code']][4]}", "--track-name", f"0:{TARGETS[OUT['code']][3]}",
                "--default-track-flag", f"0:{0 if voice else 1}", "--sub-charset", "0:UTF-8", str(srt)]
    lektor = out_path.with_name(out_path.stem + ".lektor.mka") if voice else None
    try:
        if voice:
            lektor_track(mkv, voice, audio, orig, lektor)
            cmd += ["--language", "0:pol", "--track-name", "0:Polski lektor", "--default-track-flag", "0:1", str(lektor)]
        res = run(cmd)
    finally:
        if lektor:
            lektor.unlink(missing_ok=True)
    if res.returncode not in (0, 1):            # 1 = ostrzeżenia, plik powstał
        out_path.unlink(missing_ok=True)
        raise FatalError(tr('mkvmerge nie złożył pliku MKV: {0}', (res.stdout + res.stderr).strip()))


# ---------------------------------------------------------------- SRT

def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").lstrip("\ufeff")):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines[:2]):
            m = TIMING_RE.search(line)
            if m:
                body = "\n".join(lines[i + 1:]).strip()
                if body:
                    cues.append(Cue(m.group(1), m.group(2), m.group(3).rstrip(), body))
                break
    return cues


def write_srt(cues, path):
    """Zapis przez plik tymczasowy: przerwany zapis nie zostawia niepełnych napisów pod docelową nazwą."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="\r\n") as f:
        for i, cue in enumerate(cues, 1):
            f.write(f"{i}\n{cue.start} --> {cue.end}{cue.extra}\n{cue.text}\n\n")
    os.replace(tmp, path)


def cyrillic_ratio(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum("Ѐ" <= c <= "ӿ" for c in letters) / len(letters)


def split_cue(cue):
    """Rozbija kwestię na (prefiks ASS, kursywa?, lista segmentów do tłumaczenia)."""
    prefix = "".join(ASS_RE.findall(cue.text))
    italic = cue.text.lstrip().lower().startswith("<i>")
    clean = TAG_RE.sub("", ASS_RE.sub("", cue.text))
    lines = [l.strip() for l in clean.split("\n") if l.strip()]
    if len(lines) > 1 and any(DIALOG_RE.match(l) for l in lines[1:]):
        segments = []                         # dialog: każda wypowiedź (od myślnika) osobno
        for line in lines:
            if DIALOG_RE.match(line) or not segments:
                segments.append(line)
            else:
                segments[-1] += " " + line    # dalszy ciąg zdania tej samej osoby
    else:
        segments = [" ".join(lines)]
    return prefix, italic, segments


def wrap(text):
    if len(text) <= LINE_WIDTH:
        return text
    # dwie możliwie równe linie, łamane na spacji najbliżej środka
    mid = len(text) // 2
    spaces = [i for i, c in enumerate(text) if c == " "]
    if spaces:
        cut = min(spaces, key=lambda i: abs(i - mid))
        a, b = text[:cut], text[cut + 1:]
        if max(len(a), len(b)) <= LINE_WIDTH + 6:
            return f"{a}\n{b}"
    return "\n".join(textwrap.wrap(text, LINE_WIDTH))


def join_cue(prefix, italic, segments):
    if len(segments) > 1:
        body = "\n".join(wrap(seg) if len(seg) > 55 else seg for seg in segments)
    else:
        body = wrap(segments[0]) if segments else ""
    if italic:
        body = f"<i>{body}</i>"
    return prefix + body


# ---------------------------------------------------------------- silniki tłumaczenia

def parse_json_reply(raw, count):
    """Wyciąga z odpowiedzi modelu listę `count` tłumaczeń (obiekt JSON o kluczach "1".."count")."""
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(tr('brak JSON w odpowiedzi'))
    data = json.loads(raw[start:end + 1])
    if isinstance(data.get("kwestie"), dict):
        data = data["kwestie"]
    return [str(data[str(i)]).strip() for i in range(1, count + 1)]


def json_payload(texts, context):
    """Zapytanie dla modelu. context: lista wcześniejszych kwestii albo słownik
    {"kontekst": [...], "przetlumaczone": [[oryginał, tłumaczenie], ...], "dalej": [...]}."""
    payload = {"kwestie": {str(i): t for i, t in enumerate(texts, 1)}}
    if isinstance(context, dict):
        extra = {k: v for k, v in context.items() if v}
        payload = {**extra, **payload}
    elif context:
        payload = {"kontekst": list(context), **payload}
    return json.dumps(payload, ensure_ascii=False, indent=0)


def ollama_names(timeout=5):
    with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=timeout) as r:
        return [m["name"] for m in json.load(r).get("models", [])]


def ensure_ollama():
    """Zwraca listę modeli Ollamy; gdy Ollama nie działa, próbuje ją uruchomić i czeka, aż odpowie."""
    try:
        return ollama_names()
    except (urllib.error.URLError, OSError, ValueError):
        pass
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    cli = shutil.which("ollama")
    if "localhost" not in OLLAMA_URL and "127.0.0.1" not in OLLAMA_URL or not (app.exists() or cli):
        raise OllamaDown(tr('Ollama nie odpowiada pod {0} — uruchom Ollamę i spróbuj ponownie', OLLAMA_URL))
    print(tr('    Ollama nie działa — uruchamiam ją...'))
    subprocess.Popen([str(app)] if app.exists() else [cli, "serve"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(30):
        time.sleep(2)
        try:
            return ollama_names()
        except (urllib.error.URLError, OSError, ValueError):
            continue
    raise OllamaDown(tr('Ollama nie odpowiada pod {0} mimo próby uruchomienia', OLLAMA_URL))


def ollama_is_local():
    return os.name == "nt" and any(h in OLLAMA_URL for h in ("localhost", "127.0.0.1"))


def ollama_server_parallel():
    """OLLAMA_NUM_PARALLEL z ustawień użytkownika Windows — z tą wartością startuje Ollama; None, gdy brak."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return int(winreg.QueryValueEx(key, "OLLAMA_NUM_PARALLEL")[0])
    except (OSError, ValueError, ImportError):
        return None


def restart_ollama(parallel):
    """Ustawia OLLAMA_NUM_PARALLEL i uruchamia Ollamę od nowa — serwer czyta to ustawienie tylko przy starcie.

    Wartość zostaje zapisana na stałe (zmienna środowiskowa użytkownika), więc obowiązuje też po restarcie komputera.
    """
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "OLLAMA_NUM_PARALLEL", 0, winreg.REG_SZ, str(parallel))
    os.environ["OLLAMA_NUM_PARALLEL"] = str(parallel)
    # inne programy (np. Ollama uruchamiana z menu Start) też mają zobaczyć nową wartość
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 3000, None)
    for name in ("ollama app.exe", "ollama.exe"):
        run(["taskkill", "/F", "/IM", name])
    time.sleep(2)
    app = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama app.exe"
    cmd = [str(app)] if app.exists() else [shutil.which("ollama") or "ollama", "serve"]
    # osobny proces: Ollama ma działać dalej po zamknięciu okna programu
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    for _ in range(45):
        time.sleep(2)
        try:
            ollama_names()
            return
        except (urllib.error.URLError, OSError, ValueError):
            continue
    raise OllamaDown(tr('Ollama nie wystartowała ponownie po zmianie ustawienia równoległości'))


def check_install(model=None):
    """Kontrola całej instalacji; brakujące pliki modeli audio są przy okazji pobierane. Zwraca liczbę usterek."""
    problems = 0
    print(time.strftime("\n===== Kontrola instalacji %Y-%m-%d %H:%M ====="))
    for tool in ("ffmpeg", "ffprobe"):
        found = shutil.which(tool)
        problems += not found
        print(f"    {'ok   ' if found else 'BŁĄD '} {tool}{'' if found else ' — brak w PATH'}")
    model = model or OLLAMA_MODEL
    try:
        names = ensure_ollama()
        has = model in names or f"{model}:latest" in names
        problems += not has
        print(tr('    {0} Ollama, model {1}', 'ok   ' if has else tr('BŁĄD '), model) +
              ("" if has else tr(' — brak; pobierz: ollama pull {0}', model)))
        limit = os.environ.get("OLLAMA_NUM_PARALLEL")
        print(tr('    info   równoległych zapytań Ollamy (OLLAMA_NUM_PARALLEL): {0}', limit or tr('domyślnie')))
    except FatalError as e:
        problems += 1
        print(tr('    BŁĄD  {0}', e))
    try:
        run_helper(["check"])
    except FatalError as e:
        problems += 1
        print(tr('    BŁĄD  {0}', e))
    print(tr('    Wszystko działa.') if not problems else tr('    Usterek: {0} — szczegóły wyżej i w audio_blad.txt', problems))
    return problems


MARK_RE = re.compile(r"\[(\d+)\]")


def keep_dash(source, translated):
    """Myślnik dialogowy ma być dokładnie tam, gdzie w oryginale (modele lubią go dodawać i zamieniać na półpauzę)."""
    body = re.sub(r"^\s*[-–—]\s*", "", translated)
    return f"- {body}" if DIALOG_RE.match(source) else body


class OllamaEngine:
    batch_size = 30
    parallel = 2             # ile partii tłumaczyć naraz (sens ma do wartości OLLAMA_NUM_PARALLEL)
    lang = "ru"              # język źródłowy bieżącego pliku (ustawia process())
    names = None             # pisownia imion serialu do polecenia (ustawia translate_cues())
    summary = None           # streszczenie odcinka do polecenia (ustawia translate_cues())

    def __init__(self, model):
        self.model = model
        self.name = f"Ollama ({model})"
        self.line_mode = "translategemma" in model.lower()
        # C6: modele tłumaczące bez czatu (MiLMMT) — surowe zapytanie w formacie z karty modelu, kwestia po kwestii
        self.raw_mode = "milmmt" in model.lower()
        self.think_off = not self.line_mode
        self.use_format = True
        names = ensure_ollama()
        if model not in names and f"{model}:latest" not in names:
            raise OllamaDown(tr("Ollama nie ma modelu '{0}'. Dostępne: {1}", model, ', '.join(names)))

    def _chat(self, messages, attempt=1, fmt=None):
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"num_ctx": 8192}}  # temperatura domyślna: obniżona zapętla gemma4
        if fmt is not None and self.use_format:
            body["format"] = fmt
        if self.think_off:
            body["think"] = False             # bez "myślenia" tłumaczenie jest wielokrotnie szybsze
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                return json.load(r)["message"]["content"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if self.think_off and "think" in detail.lower():
                self.think_off = False        # model nie zna tego parametru
                return self._chat(messages, fmt=fmt)
            if fmt is not None and "format" in detail.lower():
                self.use_format = False       # starsza Ollama bez schematów odpowiedzi
                return self._chat(messages)
            if e.code >= 500 and attempt < 4:  # np. losowe zapętlenie modelu ("token repeat limit")
                return self._chat(messages, attempt + 1)
            # uporczywe 5xx to zwykle brak pamięci karty (np. włączona gra) — nie wina pliku
            raise (OllamaDown if e.code >= 500 else FatalError)(f"Ollama zwróciła błąd {e.code}: {detail}")
        except (urllib.error.URLError, OSError) as e:
            raise OllamaDown(tr('utracono połączenie z Ollamą: {0}', e))

    def _lines(self, text):
        if self.line_mode:
            messages = [{"role": "user", "content": gemma_prompt(text, self.lang)}]
        else:
            line = (f"Przetłumacz kwestię z napisów filmowych z {LANGS[self.lang][1]} na polski. "
                    "Zwróć samo tłumaczenie." if OUT["code"] == "pl" else
                    f"Translate this film subtitle line from {LANGS[self.lang][0]} into "
                    f"{TARGETS[OUT['code']][0]}. Return only the translation.")
            messages = [{"role": "system", "content": line},
                        {"role": "user", "content": text}]
        out = [l.strip() for l in self._chat(messages).strip().split("\n")]
        return [l for l in out if l]

    def _split(self, texts):
        """Tłumaczy linie jednym zapytaniem; gdy model zgubi podział na linie, dzieli partię na pół."""
        if len(texts) == 1:
            return [" ".join(self._lines(texts[0]))]
        out = self._lines("\n".join(texts))
        if len(out) == len(texts):
            return out
        mid = len(texts) // 2
        return self._split(texts[:mid]) + self._split(texts[mid:])

    def _marked(self, texts):
        """Tłumaczy kwestie jednym zapytaniem; każda ma znacznik [n], po którym odnajdujemy ją w odpowiedzi.

        Samo liczenie linii zawodzi na prawdziwych napisach: model skleja zdania rozbite na dwie kwestie.
        """
        text = "\n".join(f"[{i}] {t}" for i, t in enumerate(texts, 1))
        raw = self._chat([{"role": "user", "content": gemma_prompt(text, self.lang)}])
        parts = MARK_RE.split(raw)
        found = {}
        for k in range(1, len(parts) - 1, 2):
            found.setdefault(int(parts[k]), " ".join(parts[k + 1].split()))
        out = [found.get(i, "") for i in range(1, len(texts) + 1)]
        for i, t in enumerate(out):           # zgubione znaczniki — te kwestie tłumaczymy pojedynczo
            if not t:
                out[i] = " ".join(self._lines(texts[i]))
        return out

    def summarize(self, texts):
        """Streszczenie odcinka z kwestii rozłożonych równo po całym filmie; None, gdy model zawiedzie."""
        step = max(1, sum(len(t) + 1 for t in texts) // SUMMARY_CHARS + 1)
        sample = "\n".join(texts[::step])[:SUMMARY_CHARS]
        try:
            text = self._chat([{"role": "system", "content": SUMMARY_PROMPT[OUT["code"]]},
                               {"role": "user", "content": sample}]).strip()
        except FatalError:
            return None
        return text[:2500] or None

    def shorten(self, items):
        """Skraca polskie napisy: items = [(tekst, największa liczba znaków)]; zwraca listę tekstów."""
        payload = {str(i): {"tekst": t, "max_znakow": m} for i, (t, m) in enumerate(items, 1)}
        messages = [{"role": "system", "content": SHORTEN_PROMPT if OUT["code"] == "pl" else SHORTEN_PROMPT_EN},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
        for _ in range(2):
            raw = self._chat(messages)
            try:
                data = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
                out = []
                for i in range(1, len(items) + 1):
                    value = data[str(i)]
                    # modele odsyłają czasem cały obiekt wejściowy z nowym "tekst" zamiast samego napisu
                    out.append(str(value.get("tekst", "")) if isinstance(value, dict) else str(value))
                return out
            except (ValueError, KeyError, AttributeError, TypeError):
                continue
        return [t for t, _ in items]

    def _raw(self, text):
        """Jedna kwestia w formacie MiLMMT: "Translate this from X to Y:\nX: tekst\nY:" (/api/generate, raw)."""
        src, dst = LANGS[self.lang][0], TARGETS[OUT["code"]][0]
        body = {"model": self.model, "raw": True, "stream": False, "options": {"num_ctx": 2048, "stop": ["\n"]},
                "prompt": f"Translate this from {src} to {dst}:\n{src}: {' '.join(text.split())}\n{dst}:"}
        req = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r).get("response", "").strip()
        except (urllib.error.URLError, OSError) as e:
            raise OllamaDown(tr('utracono połączenie z Ollamą: {0}', e))

    def translate(self, texts, context=()):
        if self.raw_mode:
            return [self._raw(t) for t in texts]
        if self.line_mode:
            return self._marked(texts) if len(texts) > 1 else [" ".join(self._lines(texts[0]))]
        if not self.line_mode:
            messages = [{"role": "system", "content": system_prompt(self.lang, self.names, self.summary)},
                        {"role": "user", "content": json_payload(texts, context)}]
            # schemat odpowiedzi: Ollama wymusza dokładnie klucze "1".."n" — mniej zgubionych kwestii i ponowień
            schema = {"type": "object", "properties": {str(i): {"type": "string"} for i in range(1, len(texts) + 1)},
                      "required": [str(i) for i in range(1, len(texts) + 1)]}
            for _ in range(2):
                try:
                    return parse_json_reply(self._chat(messages, fmt=schema), len(texts))
                except (ValueError, KeyError, AttributeError):
                    continue
        return self._split(texts)


class GoogleEngine:
    name = "Google Translate"
    batch_size = 60
    lang = "ru"              # język źródłowy bieżącego pliku (ustawia process())

    def __init__(self):
        try:
            from deep_translator import GoogleTranslator
        except ImportError:
            raise FatalError(tr('Brak pakietu deep-translator. Zainstaluj: pip install deep-translator'))
        self.translator = GoogleTranslator
        self.tr, self.tr_lang = None, None

    def _call(self, text):
        if self.tr_lang != self.lang:         # język źródłowy zmienia się z pliku na plik
            self.tr, self.tr_lang = self.translator(source=self.lang, target=OUT["code"]), self.lang
        for attempt in range(4):
            try:
                return self.tr.translate(text) or ""
            except Exception as e:  # deep-translator zgłasza różne wyjątki sieciowe
                if attempt == 3:
                    raise FatalError(tr('Google Translate nie odpowiada: {0}', e))
                time.sleep(2 * (attempt + 1))

    def _chunk(self, texts):
        out = self._call("\n".join(texts)).split("\n")
        out = [o.strip() for o in out]
        if len(out) != len(texts):            # tłumacz zgubił podział linii
            out = [self._call(t).strip() for t in texts]
        return out

    def translate(self, texts, context=()):
        result, chunk, size = [], [], 0
        for t in texts:
            if chunk and size + len(t) + 1 > GOOGLE_CHAR_LIMIT:
                result += self._chunk(chunk)
                chunk, size = [], 0
            chunk.append(t)
            size += len(t) + 1
        if chunk:
            result += self._chunk(chunk)
        time.sleep(0.3)
        return result


class ClaudeEngine:
    batch_size = 60
    lang = "ru"

    def __init__(self, model):
        try:
            import anthropic
        except ImportError:
            raise FatalError(tr('Brak pakietu anthropic. Zainstaluj: pip install anthropic'))
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.name = f"Claude ({model})"
        self._google = None

    def _fallback(self, texts, reason):
        print(tr('\n    ! Claude: {0} — ten fragment tłumaczy Google Translate', reason))
        if self._google is None:
            self._google = GoogleEngine()
        self._google.lang = self.lang
        return self._google.translate(texts)

    def _ask(self, texts, context):
        response = self.client.messages.create(
            model=self.model,
            max_tokens=16000,
            system=system_prompt(self.lang, getattr(self, "names", None), getattr(self, "summary", None)),
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": json_payload(texts, context)}],
        )
        if response.stop_reason == "refusal":
            return None
        raw = "".join(b.text for b in response.content if b.type == "text")
        return parse_json_reply(raw, len(texts))

    def translate(self, texts, context=()):
        a = self.anthropic
        for attempt in range(2):
            try:
                out = self._ask(texts, context)
                if out is None:
                    return self._fallback(texts, tr('model odmówił tłumaczenia'))
                return out
            except (ValueError, KeyError, AttributeError):
                continue                      # zła struktura odpowiedzi — ponów
            except (a.AuthenticationError, a.PermissionDeniedError) as e:
                raise FatalError(tr('Claude API odrzuciło klucz (ANTHROPIC_API_KEY): {0}', e.message))
            except a.NotFoundError as e:
                raise FatalError(tr("Nieznany model '{0}': {1}", self.model, e.message))
            except a.APIStatusError as e:
                return self._fallback(texts, tr('błąd API {0}', e.status_code))
            except a.APIConnectionError:
                return self._fallback(texts, tr('brak połączenia'))
        return self._fallback(texts, tr('nieprawidłowa odpowiedź'))


def make_engine(name, model):
    if name == "ollama":
        return OllamaEngine(model or OLLAMA_MODEL)
    if name == "claude":
        return ClaudeEngine(model or CLAUDE_MODEL)
    return GoogleEngine()


# ---------------------------------------------------------------- przetwarzanie

def load_checkpoint(path, flat):
    """Wczytuje częściowe tłumaczenie {numer kwestii: tekst}, o ile dotyczy tych samych napisów."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["source"] == flat:
            return {int(i): t for i, t in data["done"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return {}


def save_checkpoint(path, flat, saved):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"source": flat, "done": saved}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- słowniczek imion serialu

GLOSSARY_NAME = "slowniczek.json"
RU_LETTERS = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                      ["a", "b", "w", "g", "d", "ie", "io", "ż", "z", "i", "j", "k", "ł", "m", "n", "o", "p", "r", "s",
                       "t", "u", "f", "ch", "c", "cz", "sz", "szcz", "", "y", "", "e", "iu", "ia"]))
RU_ENDINGS = ("ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ой", "ом", "ем", "ым", "им", "ую",
              "а", "я", "у", "ю", "е", "и", "ы", "ь", "й")
PL_ENDINGS = ("ami", "ach", "owi", "ego", "emu", "em", "om", "ów", "ie", "ą", "ę", "a", "u", "y", "i", "e", "o")
RU_NAME_RE = re.compile(r"\b[А-ЯЁ][а-яё]{2,}\b")
PL_WORD_RE = re.compile(r"\b[A-ZĄĆĘŁŃÓŚŹŻ][a-ząćęłńóśźż]{2,}\b")
SEASON_DIR_RE = re.compile(r"^(season|sezon|сезон|s)\s*\d+$", re.IGNORECASE)


def strip_ending(word, endings):
    for e in endings:
        if word.endswith(e) and len(word) - len(e) >= 3:
            return word[:-len(e)]
    return word


def ru_translit(word):
    """Przybliżony polski zapis rosyjskiego słowa — tylko do dopasowania imienia w tłumaczeniu."""
    return "".join(RU_LETTERS.get(c, c) for c in word.lower())


def similar(a, b):
    import difflib
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def glossary_file(video):
    """slowniczek.json w folderze serialu ("Serial/Season 01/odc.mkv" -> "Serial/")."""
    folder = video.parent
    name = GLOSSARY_NAME if OUT["code"] == "pl" else GLOSSARY_NAME.replace(".json", f".{OUT['code']}.json")
    return (folder.parent if SEASON_DIR_RE.match(folder.name) else folder) / name


def load_glossary(path):
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("imiona", {})
    except (OSError, ValueError, AttributeError):
        return {}


def save_glossary(path, names):
    data = {"opis": "Pisownia imion i nazw w tym serialu. Program wybiera najczęstszy wariant; żeby narzucić "
                    "własny, wpisz go w \"pisownia\" i ustaw \"zablokuj\": true. Pole \"plec\": \"K\" albo "
                    "\"M\" mówi tłumaczowi, czy postać to kobieta, czy mężczyzna.",
            "imiona": dict(sorted(names.items()))}
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass                                  # np. folder tylko do odczytu — słowniczek jest dodatkiem


def unify_names(sources, outputs, glossary_path):
    """Ujednolica zapis imion w tłumaczeniu: w całym serialu jedno imię = jedna pisownia (z polską odmianą).

    Zwraca listę poprawionych tłumaczeń i liczbę poprawek.
    """
    # odmiana przez przypadki tylko w polskim; w angielskim imię zmienia się najwyżej o dopełniacz 's
    endings = PL_ENDINGS if OUT["code"] == "pl" else ("'s", "’s")
    names = load_glossary(glossary_path) if glossary_path else {}
    # imiona: słowa pisane wielką literą w środku zdania (na początku zdania wielka litera nic nie znaczy)
    mid = {m.group() for s in sources for m in RU_NAME_RE.finditer(s)
           if m.start() > 0 and not re.search(r"[.!?…:\-–—]\s*$", s[:m.start()])}
    keys = {strip_ending(w, RU_ENDINGS) for w in mid} | set(names)
    pairs = []                                # (numer kwestii, słowo w tłumaczeniu, klucz rosyjski)
    for i, (src, out) in enumerate(zip(sources, outputs)):
        words = PL_WORD_RE.findall(out)
        for m in RU_NAME_RE.finditer(src):
            key = strip_ending(m.group(), RU_ENDINGS)
            if key not in keys or not words:
                continue
            guess = ru_translit(key)
            best = max(words, key=lambda w: similar(strip_ending(w, endings), guess))
            if similar(strip_ending(best, endings), guess) >= 0.6:
                pairs.append((i, best, key))
    for _, word, key in pairs:                # liczenie wariantów (cały serial)
        entry = names.setdefault(key, {"pisownia": "", "warianty": {}, "zablokuj": False})
        entry["warianty"][word] = entry["warianty"].get(word, 0) + 1
    canon = {}
    for key, entry in names.items():
        if not entry.get("warianty") and not entry.get("pisownia"):
            continue
        if not entry.get("zablokuj") and entry.get("warianty"):
            stems = {}
            for word, n in entry["warianty"].items():
                stems[strip_ending(word, endings)] = stems.get(strip_ending(word, endings), 0) + n
            top = max(stems, key=stems.get)
            # do wyświetlenia: forma podstawowa, gdy wystąpiła, inaczej najczęstsza (przy remisie krótsza)
            entry["pisownia"] = max((w for w in entry["warianty"] if strip_ending(w, endings) == top),
                                    key=lambda w: (w == top, entry["warianty"][w], -len(w)))
        canon[key] = strip_ending(entry["pisownia"], endings)
    all_canon = set(canon.values())
    fixed, outputs = 0, list(outputs)
    for i, word, key in pairs:
        stem, target = strip_ending(word, endings), canon.get(key)
        # tylko wyraźne warianty tego samego imienia (Teniszew/Tieniszew, Michał/Michaił), nigdy inne imię
        if not target or stem == target or stem in all_canon or abs(len(stem) - len(target)) > 2 \
                or stem[0] != target[0] or similar(stem, target) < 0.75:
            continue
        new = target + word[len(stem):]
        replaced = re.sub(rf"\b{re.escape(word)}\b", new, outputs[i])
        if replaced != outputs[i]:
            outputs[i], fixed = replaced, fixed + 1
    if glossary_path and names:
        save_glossary(glossary_path, names)
    return outputs, fixed


# ---------------------------------------------------------------- kontrola jakości

QUALITY_REPORT = Path(os.environ.get("MKV_QUALITY_REPORT") or APP_DIR / "raport_jakosci.csv")
LOOP_RE = re.compile(r"\b(\w+)(?:\W+\1\b){4,}", re.IGNORECASE)      # 5 razy to samo słowo pod rząd
REPEAT_RE = re.compile(r"\b(\w+)(?:\W+\1\b){1,}", re.IGNORECASE)    # powtórzenie w oryginale ("Нет-нет")


def srt_seconds(stamp):
    h, m, s = stamp.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


TARGET_CPS = 15          # wygodna szybkość czytania polskich napisów (znaki na sekundę)
MIN_CUE_SECONDS = 1.0
MAX_CUE_SECONDS = 7.0


def srt_stamp(seconds):
    ms = max(0, round(seconds * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


def retime(cues):
    """Wydłuża kwestie za krótkie do przeczytania (cel: TARGET_CPS, co najmniej 1 s) — tylko w przerwie przed
    następną kwestią, więc napisy nigdy na siebie nie nachodzą. Zwraca (kwestie, liczba wydłużonych)."""
    try:
        times = [(srt_seconds(c.start), srt_seconds(c.end)) for c in cues]
    except ValueError:
        return cues, 0
    out, changed = [], 0
    for i, (cue, (start, end)) in enumerate(zip(cues, times)):
        chars = len(TAG_RE.sub("", ASS_RE.sub("", cue.text)).replace("\n", " "))
        need = min(MAX_CUE_SECONDS, max(MIN_CUE_SECONDS, chars / TARGET_CPS))
        # ostatnia kwestia zostaje bez zmian: wydłużona mogłaby wyjść poza koniec filmu
        limit = times[i + 1][0] - 0.08 if i + 1 < len(times) else end
        new_end = min(start + need, limit)
        if end - start < need and new_end > end + 0.05:
            cue = Cue(cue.start, srt_stamp(new_end), cue.extra, cue.text)
            changed += 1
        out.append(cue)
    return out, changed


def segment_issues(src, out, lang):
    """Usterki jednej przetłumaczonej kwestii (pusta lista = w porządku)."""
    if not any(c.isalpha() for c in src):
        return []
    found = []
    words = re.findall(r"\w+", src)
    # "Rick.", "Deb!", "Jim Clark.", "Hm?" — imiona i wykrzykniki brzmią po polsku tak samo; to nie błąd
    # (tylko dla alfabetu łacińskiego — z cyrylicy nawet imię trzeba zapisać po polsku: Саша -> Sasza)
    same_ok = lang not in ("ru", "uk") and len(words) <= 3 and all(w[0].isupper() or len(w) <= 3 for w in words)
    if still_source(out, lang) and not same_ok:
        found.append("w języku oryginału")
    elif not out.strip() or out.strip() == src.strip() and not same_ok:
        found.append("nieprzetłumaczone")
    # "Nie, nie, nie, nie!" jest w porządku, gdy oryginał też się powtarza ("Нет, нет, нет, нет!")
    if LOOP_RE.search(out) and not REPEAT_RE.search(src) or len(out) > 3 * len(src) + 20:
        found.append("zapętlenia")
    elif len(src) >= 20 and not 0.35 <= len(out) / max(1, len(src)) <= 2.5:
        found.append("długość")
    return found


def cue_cps(cue):
    """Szybkość czytania kwestii (znaki na sekundę); 0, gdy nie da się policzyć."""
    try:
        seconds = srt_seconds(cue.end) - srt_seconds(cue.start)
    except ValueError:
        return 0
    text = TAG_RE.sub("", ASS_RE.sub("", cue.text)).replace("\n", " ")
    return len(text) / seconds if seconds > 0 else 0


CPS_WARN, CPS_MAX = 17, 20                    # tempo czytania: norma Netflix PL dla dorosłych / granica błędu


def quality_check(sources, outputs, cues, lang="ru"):
    """Liczy podejrzane miejsca tłumaczenia; zwraca (ocena, opis, przykłady)."""
    issues = {"w języku oryginału": [], "nieprzetłumaczone": [], "zapętlenia": [], "długość": []}
    for src, out in zip(sources, outputs):
        for kind in segment_issues(src, out, lang):
            issues[kind].append(f"{src}  →  {out}" if kind == "długość" else out or src)
    fast = sum(cue_cps(c) > CPS_MAX for c in cues)
    total = max(1, len(sources))
    bad = (issues["w języku oryginału"] or issues["zapętlenia"] or len(issues["nieprzetłumaczone"]) > 0.02 * total
           or len(issues["długość"]) > 0.05 * total or fast > 0.25 * max(1, len(cues)))
    quick = sum(CPS_WARN < cue_cps(c) <= CPS_MAX for c in cues)
    parts = [f"{len(v)} {tr(k)}" for k, v in issues.items() if v] + ([tr('{0} za szybkich (>20 zn/s)', fast)] if fast else []) \
        + ([tr('{0} szybkich (17–20 zn/s)', quick)] if quick else [])
    examples = [x for v in issues.values() for x in v[:2]][:4]
    return ("Do sprawdzenia" if bad else "Gotowe"), ", ".join(parts) or tr('bez uwag'), examples


# ---------------------------------------------------------------- automatyczna naprawa

# ogólne modele do poprawek (widzą kontekst, umieją skracać); pierwszy dostępny w Ollamie
REPAIR_MODELS = ("translategemma:27b", "gemma4:31b", "gemma4:26b", "qwen3.5:27b", "gemma3:27b")
_repair = {}


def set_repair_model(name):
    """Model do poprawek wybrany ręcznie (pusty = automatycznie z REPAIR_MODELS)."""
    if name:
        os.environ["MKV_REPAIR_MODEL"] = name
    else:
        os.environ.pop("MKV_REPAIR_MODEL", None)
    _repair.clear()


def repair_engine(main):
    """Drugi model Ollamy do poprawek albo None (brak modelu, inny silnik). MKV_REPAIR_MODEL wskazuje własny."""
    if not isinstance(main, OllamaEngine):
        return None
    if "engine" not in _repair:
        wanted = os.environ.get("MKV_REPAIR_MODEL")
        try:
            names = ollama_names()
        except (urllib.error.URLError, OSError, ValueError):
            names = []
        # model wybrany ręcznie może być tym samym co główny (poprawki z kontekstem i tak coś dają)
        model = next((m for m in ([wanted] if wanted else REPAIR_MODELS)
                      if (wanted or m != main.model) and (m in names or f"{m}:latest" in names)), None)
        _repair["engine"] = OllamaEngine(model) if model else None
    return _repair["engine"]


def repair_segments(flat, done, engine, lang, extra=()):
    """Ponownie tłumaczy podejrzane kwestie: najpierw pojedynczo tym samym modelem, potem modelem ogólnym
    z kontekstem poprzednich kwestii. extra: dodatkowe podejrzane (np. najgorsze wg MetricX).
    Zwraca (tłumaczenia, poprawione, podejrzane)."""
    done = list(done)
    bad = sorted({i for i in range(len(flat)) if segment_issues(flat[i], done[i], lang)} | set(extra))
    if not bad:
        return done, 0, 0
    if len(bad) > 20 and len(bad) > 0.4 * len(flat):
        return done, 0, len(bad)              # awaria całego tłumaczenia, nie pojedynczych kwestii
    # zła długość to zwykle przesunięcie: model przeniósł zdanie rozcięte między kwestiami do sąsiedniej —
    # sąsiadów też tłumaczymy pojedynczo, żeby każda kwestia dostała swój fragment
    shifted = {j for i in bad if "długość" in segment_issues(flat[i], done[i], lang) for j in (i - 1, i + 1)
               if 0 <= j < len(flat) and any(c.isalpha() for c in flat[j])}
    neighbours = sorted(shifted - set(bad))
    for j in neighbours:
        try:
            new = engine.translate([flat[j]])[0]
        except ToolDown:
            raise
        except FatalError:
            continue
        if new and not segment_issues(flat[j], new, lang):
            done[j] = keep_dash(flat[j], new)
    print(tr('    naprawa: {0} podejrzanych kwestii (+{1} sąsiednich) — tłumaczę pojedynczo', len(bad), len(neighbours)))

    def attempt(eng, i, context=()):
        try:
            new = eng.translate([flat[i]], context)[0]
        except ToolDown:
            raise
        except FatalError:
            return False
        if new and not segment_issues(flat[i], new, lang):
            done[i] = keep_dash(flat[i], new)
            return True
        return False

    left = [i for i in bad if not attempt(engine, i)]
    helper = repair_engine(engine) if left else None
    if helper:
        helper.lang = lang
        print(tr('    naprawa: {0} nadal źle — poprawia {1} (z kontekstem)', len(left), helper.model))
        left = [i for i in left if not attempt(helper, i, flat[max(0, i - 4):i])]
    return done, len(bad) - len(left), len(bad)


def shorten_fast(cues, engine, limit=20, target=17):
    """Skraca napisy za szybkie do przeczytania (> limit zn/s) modelem ogólnym, do długości mieszczącej się
    w czasie wyświetlania. Kwestie dialogowe i z formatowaniem zostają bez zmian. Zwraca (kwestie, skrócone)."""
    helper = repair_engine(engine)
    if helper is None:
        return cues, 0
    todo = [i for i, c in enumerate(cues) if cue_cps(c) > limit and not DIALOG_RE.match(c.text)
            and not TAG_RE.search(c.text) and not ASS_RE.search(c.text)]
    if not todo:
        return cues, 0
    print(tr('    naprawa: skracam {0} napisów za szybkich do przeczytania ({1})', len(todo), helper.model))
    cues, changed = list(cues), 0
    for pos in range(0, len(todo), 30):
        idx = todo[pos:pos + 30]
        items = []
        for i in idx:
            text = cues[i].text.replace("\n", " ")
            seconds = srt_seconds(cues[i].end) - srt_seconds(cues[i].start)
            items.append((text, max(8, int(seconds * target))))
        for i, (old, most), new in zip(idx, items, helper.shorten(items)):
            new = " ".join(str(new).split())
            # tylko rzeczywiste skrócenie bez obcego języka; nie krótsze niż 40% oryginału, chyba że model
            # trafił w zadaną długość (bardzo krótko wyświetlana kwestia musi stracić więcej)
            floor = min(0.4 * len(old), 0.6 * most)
            if new and floor <= len(new) < len(old) and not cyrillic_ratio(new) > 0.1:
                cues[i] = Cue(cues[i].start, cues[i].end, cues[i].extra, wrap(new))
                changed += 1
    return cues, changed


SPEECH_SOURCE = "słuch"                       # kolumna "źródło" w raporcie: "słuch (silnik)" albo "napisy"


def report_quality(video, verdict, summary, count, source=""):
    """Dopisuje wynik do zbiorczego raportu jakości (CSV otwierany w Excelu).
    Ostatnia kolumna — źródło tekstu (wiersze ze starszych wersji jej nie mają)."""
    import csv
    try:
        new = not QUALITY_REPORT.exists()
        with open(QUALITY_REPORT, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow([tr('data'), tr('ocena'), tr('kwestii'), tr('uwagi'), tr('plik'), tr('źródło')])
            w.writerow([time.strftime("%Y-%m-%d %H:%M"), verdict, count, summary, str(video), source])
    except OSError:
        pass


def speech_translated(report=None, log=None):
    """Pliki przetłumaczone z rozpoznanej mowy -> data ostatniego tłumaczenia.

    Źródło: kolumna "źródło" raportu jakości; dla wierszy ze starszych wersji (bez tej kolumny) — dziennik
    programu, w którym przy każdym pliku jest wpis "rozpoznaję mowę z dźwięku" (łączony po nazwie pliku).
    Plik przetłumaczony później z napisów wypada z listy."""
    import csv
    report = Path(report or QUALITY_REPORT)
    log = Path(log or APP_DIR / "dziennik.txt")
    heard = set()                             # nazwy plików, przy których dziennik zapisał rozpoznawanie mowy
    try:
        name = None
        with open(log, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\[\d\d:\d\d\] (.+)$", line.rstrip())
                if m:
                    name = m.group(1).strip()
                elif name and ("rozpoznaję mowę" in line or "recognizing speech" in line):
                    heard.add(name)
    except OSError:
        pass
    found = {}
    try:
        with open(report, encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f, delimiter=";"):
                if len(row) < 5 or not re.match(r"\d{4}-\d\d-\d\d", row[0]):
                    continue                  # nagłówek albo uszkodzony wiersz
                path = row[4]
                if len(row) > 5 and row[5]:
                    speech = row[5].startswith(SPEECH_SOURCE)
                else:
                    speech = Path(path).name in heard
                if speech:
                    found[path] = row[0]
                else:
                    found.pop(path, None)
    except OSError:
        pass
    return found


CONTEXT_NEXT, CONTEXT_PAIRS = 4, 5          # kwestie po partii i gotowe pary tłumaczeń w zapytaniu
SUMMARY_MIN_CUES = 60                         # krótsze pliki tłumaczą się bez streszczenia


TIME_RE = re.compile(r"(?<![\d:.])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
QUOTE_RE = re.compile(r'"([^"\n]+)"|«([^»\n]+)»|“([^”\n]+)”')
DIALOG_DASH_RE = re.compile(r"^\s*[—–]\s*", re.M)


def polish_norms(cues):
    """C4: zapis wg polskich norm napisów (Netflix PL): wielokropek jednym znakiem, cudzysłowy „…”, myślnik
    dialogowy "- " na początku wypowiedzi, godziny z kropką (14.15). Zwraca (kwestie, liczba poprawionych)."""
    out, changed = [], 0
    for cue in cues:
        text = cue.text.replace("...", "…")
        text = QUOTE_RE.sub(lambda m: "„" + (m.group(1) or m.group(2) or m.group(3)) + "”", text)
        text = DIALOG_DASH_RE.sub("- ", text)
        text = TIME_RE.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text)
        if text != cue.text:
            changed += 1
            cue = Cue(cue.start, cue.end, cue.extra, text)
        out.append(cue)
    return out, changed


QE_BAD = 8.0                                  # wynik MetricX (0–25), od którego kwestia idzie do naprawy


def qe_score(pairs):
    """C5: wyniki MetricX-24 dla par (oryginał, tłumaczenie) albo None, gdy ocena niedostępna (brak audio_env,
    MKV_QE=0, błąd). Na czas oceny Ollama zwalnia kartę, a po niej model oceny jest zwalniany."""
    if not pairs or os.environ.get("MKV_QE", "1") == "0":
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src, out = Path(tmp) / "pary.json", Path(tmp) / "oceny.json"
        src.write_text(json.dumps(pairs, ensure_ascii=False), encoding="utf-8")
        try:
            ollama_unload()
            run_helper(["qe", src, out])
            free_helper()
            scores = json.loads(out.read_text(encoding="utf-8"))
        except (FatalError, ToolDown, OSError, ValueError) as e:
            print(tr('    ocena jakości pominięta ({0})', e))
            return None
    return scores if len(scores) == len(pairs) else None


SENTENCE_END = (".", "!", "?", "…")
SENTENCE_MAX_CUES, SENTENCE_MAX_CHARS = 4, 220


def cue_seconds(stamp):
    h, m, s = stamp.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def sentence_groups(cues):
    """C7: kolejne kwestie składające się na jedno zdanie (rozpoznana mowa tnie zdania między kwestie).
    Kwestie z myślnikami dialogowymi i kwestie po dłuższej pauzie nie są łączone."""
    groups, cur = [], []
    for k, cue in enumerate(cues):
        text = cue.text.strip()
        if "\n" in text or DIALOG_RE.match(text):
            if cur:
                groups.append(cur)
            groups.append([k])
            cur = []
            continue
        cur.append(k)
        pause = k + 1 < len(cues) and cue_seconds(cues[k + 1].start) - cue_seconds(cue.end) > 1.5
        if text.endswith(SENTENCE_END) or pause or len(cur) >= SENTENCE_MAX_CUES \
                or sum(len(cues[j].text) for j in cur) >= SENTENCE_MAX_CHARS:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def split_translation(text, weights):
    """Dzieli przetłumaczone zdanie na len(weights) części proporcjonalnie do długości oryginalnych kwestii;
    tnie między słowami, chętniej po przecinku, kropce lub myślniku."""
    words = text.split()
    if len(weights) == 1 or len(words) < len(weights):
        return [text] + [""] * (len(weights) - 1) if len(words) < len(weights) else [text]
    ends, pos = [], 0
    for w in words:
        pos += len(w) + 1
        ends.append(pos)
    total, cuts, acc, last = sum(weights), [], 0, 0
    for w in weights[:-1]:
        acc += w
        target = acc / total * ends[-1]
        options = range(last + 1, len(words) - (len(weights) - len(cuts) - 1) + 1)
        best = min(options, key=lambda i: abs(ends[i - 1] - target)
                   - (0.12 * ends[-1] if words[i - 1].endswith((",", ".", "!", "?", "…", ";", ":", "—")) else 0))
        cuts.append(best)
        last = best
    bounds = [0, *cuts, len(words)]
    return [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]


def translate_sentences(cues, engine, checkpoint=None, resume=True, glossary=None, quality=None, speakers=None):
    """C7: tłumaczenie rozpoznanej mowy całymi zdaniami — zdanie rozcięte między kwestie idzie do modelu w całości,
    a tłumaczenie wraca do tych samych kwestii i czasów. Mniej przesunięć i urwanych zdań niż kwestia po kwestii."""
    groups = sentence_groups(cues)
    if all(len(g) == 1 for g in groups):
        return translate_cues(cues, engine, checkpoint, resume, glossary, quality, speakers)
    merged = [Cue(cues[g[0]].start, cues[g[-1]].end, cues[g[0]].extra,
                  " ".join(" ".join(cues[k].text.split()) for k in g) if len(g) > 1 else cues[g[0]].text) for g in groups]
    spk = None
    if speakers and len(speakers) == len(cues):
        spk = [speakers[g[0]] if all(speakers[k] == speakers[g[0]] for k in g) else [] for g in groups]
    print(tr('    tłumaczenie całymi zdaniami: {0} kwestii w {1} zdaniach', len(cues), len(groups)))
    done = translate_cues(merged, engine, checkpoint, resume, glossary, quality, spk)
    result = []
    for g, unit in zip(groups, done):
        if len(g) == 1:
            result.append(Cue(cues[g[0]].start, cues[g[0]].end, cues[g[0]].extra, unit.text))
            continue
        parts = split_translation(" ".join(unit.text.split()), [max(1, len(cues[k].text)) for k in g])
        result += [Cue(cues[k].start, cues[k].end, cues[k].extra, wrap(p) if p else "…") for k, p in zip(g, parts)]
    result, _ = retime(result)
    return result


def translate_cues(cues, engine, checkpoint=None, resume=True, glossary=None, quality=None, speakers=None):
    """Tłumaczy kwestie; postęp zapisuje po każdej partii w pliku checkpoint, żeby dało się wznowić.

    glossary: plik słowniczka imion serialu; quality: słownik, do którego trafia ocena jakości tłumaczenia.
    """
    parts = [split_cue(c) for c in cues]
    flat = [seg for _, _, segs in parts for seg in segs]
    # B12: płeć mówiącego dla każdego segmentu (kwestia z myślnikami = dwie osoby, po jednej na wypowiedź)
    gender = []
    for k, (_, _, segs) in enumerate(parts):
        group = speakers[k] if speakers and len(speakers) == len(cues) else []
        for j in range(len(segs)):
            who = group[j] if len(group) == len(segs) else group[0] if len(group) == 1 else {}
            gender.append(who.get("p"))
    # kwestie bez liter (np. "...", "♪") zostają bez zmian
    todo = [i for i, t in enumerate(flat) if any(c.isalpha() for c in t)]
    todo_all = list(todo)                     # wszystkie kwestie do tłumaczenia (todo maleje przy wznowieniu)
    done = list(flat)
    saved = load_checkpoint(checkpoint, flat) if checkpoint and resume else {}
    if saved:
        for i, t in saved.items():
            done[i] = t
        todo = [i for i in todo if i not in saved]
        print(tr('    wznawiam tłumaczenie: {0} kwestii już gotowych, zostało {1}', len(saved), len(todo)))
    step = engine.batch_size
    workers = max(1, getattr(engine, "parallel", 1))
    lang = getattr(engine, "lang", "ru")
    engine.names = glossary_prompt_names(glossary) if lang in ("ru", "uk") else None
    engine.summary = None
    # streszczenie odcinka (C2): raz na plik, jako tło w każdej partii; TranslateGemma ma stały format zapytania
    if (hasattr(engine, "summarize") and not getattr(engine, "line_mode", False) and not getattr(engine, "raw_mode", False)
            and len(todo) >= SUMMARY_MIN_CUES
            and os.environ.get("MKV_SUMMARY", "1") != "0"):
        engine.summary = engine.summarize([flat[i] for i in todo])
        if engine.summary:
            print(tr('    streszczenie odcinka dla tłumacza: {0} znaków', len(engine.summary)))

    def translate_batch(pos):
        idx = todo[pos:pos + step]
        # kontekst: kwestie przed partią, 4 kwestie po niej (zdania rozcięte między kwestie) i ostatnie
        # gotowe pary oryginał -> tłumaczenie (forma "ty/Pan", rodzaj mówiących, pisownia); przy partiach
        # tłumaczonych równolegle pary pochodzą z poprzedniej grupy — bieżąca jeszcze nie jest gotowa
        earlier = [i for i in todo[:pos] if i in saved][-CONTEXT_PAIRS:]
        context = {"kontekst": [flat[i] for i in todo[max(0, pos - 8):pos]],
                   "przetlumaczone": [[flat[i], saved[i]] for i in earlier],
                   "dalej": [flat[i] for i in todo[pos + step:pos + step + CONTEXT_NEXT]],
                   "plec": {str(n): gender[i] for n, i in enumerate(idx, 1) if gender[i]}}
        out = engine.translate([flat[i] for i in idx], context)
        for n, i in enumerate(idx):           # kwestia wróciła nieprzetłumaczona — przetłumacz ją osobno
            if n < len(out) and still_source(out[n], lang):
                out[n] = engine.translate([flat[i]])[0]
        return idx, out

    # kilka partii naraz: Ollama liczy równoległe zapytania wspólnie, więc idzie to szybciej niż po kolei
    starts = list(range(0, len(todo), step))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for group in range(0, len(starts), workers):
            if cancel.is_set():
                raise Cancelled
            for idx, out in pool.map(translate_batch, starts[group:group + workers]):
                for i, t in zip(idx, out):
                    if t:                     # pusta odpowiedź nie trafia do punktu kontrolnego — po wznowieniu wróci
                        done[i] = saved[i] = keep_dash(flat[i], t)
            if checkpoint:
                save_checkpoint(checkpoint, flat, saved)
            finished = min((group + workers) * step, len(todo))
            print(tr('\r    przetłumaczono {0}/{1} kwestii', finished, len(todo)), end="", flush=True)
    print()
    if checkpoint:
        checkpoint.unlink(missing_ok=True)
    repaired = shortened = 0
    qe_bad, qe_scores = [], None
    if quality is not None:                   # C5: ocena każdej kwestii bez wzorca (MetricX-24)
        qe_scores = qe_score([(flat[i], done[i]) for i in todo_all])
        if qe_scores:
            qe_bad = [i for i, sc in zip(todo_all, qe_scores) if sc >= QE_BAD]
            print(tr('    ocena jakości (MetricX): średnio {0:.1f} (0 = idealnie, 25 = źle), słabych kwestii: {1}',
                     sum(qe_scores) / len(qe_scores), len(qe_bad)))
    if quality is not None:                   # automatyczna naprawa podejrzanych kwestii
        done, repaired, suspicious = repair_segments(flat, done, engine, lang, qe_bad)
        if suspicious:
            print(tr('    naprawa: poprawiono {0} z {1} podejrzanych kwestii', repaired, suspicious))
    left = sum(still_source(done[i], lang) for i in range(len(flat)) if any(c.isalpha() for c in flat[i]))
    if left > max(3, 0.2 * len(flat)):
        raise FatalError(tr('tłumaczenie nie powiodło się: {0} z {1} kwestii zostało nieprzetłumaczonych', left, len(flat)))
    if glossary is not None and lang in ("ru", "uk"):   # jedna pisownia imion (transkrypcja z cyrylicy)
        done, fixed = unify_names(flat, done, glossary)
        if fixed:
            print(tr('    słowniczek ({0}): ujednolicono zapis imion w {1} miejscach', glossary.name, fixed))
    result, k = [], 0
    for cue, (prefix, italic, segs) in zip(cues, parts):
        translated = done[k:k + len(segs)]
        k += len(segs)
        result.append(Cue(cue.start, cue.end, cue.extra, join_cue(prefix, italic, translated)))
    if OUT["code"] == "pl":                  # C4: polskie normy zapisu napisów
        result, normed = polish_norms(result)
        if normed:
            print(tr('    zapis wg polskich norm: poprawiono {0} kwestii (wielokropek, cudzysłowy, myślniki, godziny)', normed))
    result, longer = retime(result)
    if longer:
        print(tr('    czas wyświetlania: wydłużono {0} kwestii za krótkich do przeczytania', longer))
    if quality is not None and sum(cue_cps(c) > 20 for c in result) > 0.25 * max(1, len(result)):
        result, shortened = shorten_fast(result, engine)   # tylko gdy za szybkich jest wyraźnie za dużo
    if quality is not None:
        verdict, summary, examples = quality_check(flat, done, result, lang)
        if qe_scores:
            summary += tr(', MetricX średnio {0:.1f}', sum(qe_scores) / len(qe_scores))
        if repaired or shortened:
            summary += tr(' (naprawiono automatycznie: {0} kwestii, skrócono {1})', repaired, shortened)
        quality.update(verdict=verdict, summary=summary)
        print(tr('    jakość: {0}', summary) + (tr(' — DO SPRAWDZENIA') if verdict != "Gotowe" else ""))
        for example in examples:
            print(tr('      np.: {0}', example[:110]))
    return result


TELETEXT = "dvb_teletext"   # napisy telegazety w nagraniach TS z telewizji: tekst, nie obraz
TELETEXT_INPUT = ["-txt_format", "text", "-txt_page", "*"]   # dekoder libzvbi: tekst ze wszystkich stron


def sentence_case(text):
    """"ТИХО ПЛЕЩЕТСЯ ВОДА. ГДЕ ОН?" -> "Тихо плещется вода. Где он?" (napisy telegazety są wersalikami)."""
    out = re.sub(r"(^|[.!?…]\s+|\n-?\s*)(\w)", lambda m: m.group(1) + m.group(2).upper(), text.lower())
    return out[:1].upper() + out[1:]


def clean_teletext(cues):
    """Telegazeta nie podaje końca kwestii (wychodzi np. "1193:02:52"): koniec liczymy z długości tekstu, najpóźniej
    tuż przed następną kwestią; tekst pisany wersalikami zamieniamy na zwykły zapis zdań."""
    out = []
    for i, cue in enumerate(cues):
        start = srt_seconds(cue.start)
        text = cue.text.strip()
        letters = [c for c in text if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) > 0.8 * len(letters):
            text = sentence_case(text)
        end = start + min(MAX_CUE_SECONDS, max(1.5, len(text) / 12))
        if i + 1 < len(cues):
            end = min(end, srt_seconds(cues[i + 1].start) - 0.08)
        if text and end > start:
            out.append(Cue(cue.start, srt_stamp(end), "", text))
    return out


def teletext_srt(src, stream_index, out_srt):
    """Zamienia ścieżkę telegazety na zwykłe napisy SRT; zwraca liczbę kwestii (0 = brak tekstu)."""
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "teletekst.srt"
        run(["ffmpeg", "-v", "error", "-y", *TELETEXT_INPUT, "-i", str(src), "-map", f"0:{stream_index}",
             "-c:s", "srt", str(raw)])
        cues = clean_teletext(parse_srt(raw.read_text(encoding="utf-8", errors="replace"))) if raw.exists() else []
    if cues:
        write_srt(cues, out_srt)
    return len(cues)


PGS = "hdmv_pgs_subtitle"   # napisy graficzne z Blu-ray — odczytywane przez OCR (EasyOCR w audio_env)


def ocr_cues(mkv, pgs, tmp, source):
    """(kwestie, język) z napisów graficznych PGS przez OCR. Wybór ścieżki: język źródłowy według znacznika,
    a w nim ta z największą liczbą plansz (żeby nie wziąć szczątkowej "Forced")."""
    order = list(LANGS) if source == "auto" else [source]

    def tag_lang(s):
        return next((code for code in LANGS if lang_of(s) in LANGS[code][2]), None)

    def frames(s):
        tags = s.get("tags", {})
        try:
            return int(tags.get("NUMBER_OF_FRAMES") or tags.get("NUMBER_OF_FRAMES-eng") or 0)
        except ValueError:
            return 0

    tagged = [s for s in pgs if tag_lang(s) in order]
    pool = sorted(tagged or pgs, key=lambda s: (order.index(tag_lang(s)) if tag_lang(s) in order else 9, -frames(s)))
    s = pool[0]
    guess = tag_lang(s) or ("ru" if source == "auto" else source)   # czytnik "ru" zna też alfabet łaciński
    sup, out = Path(tmp) / "napisy.sup", Path(tmp) / "ocr.srt"
    res = run(["ffmpeg", "-v", "error", "-y", "-i", str(mkv), "-map", f"0:{s['index']}", "-c", "copy", str(sup)])
    if res.returncode != 0:
        raise FatalError(tr('nie udało się wyodrębnić napisów graficznych: {0}', res.stderr.strip()[-200:]))
    print(tr('    napisy graficzne [{0}] — odczytuję tekst (OCR)', lang_of(s) or '?'))
    run_helper(["ocr", sup, out, "--language", guess])
    cues = parse_srt(out.read_text(encoding="utf-8", errors="replace")) if out.exists() else []
    lang = detect_lang([c.text for c in cues]) or guess
    if not cues or lang not in order:
        raise FatalError(tr('OCR nie odczytał napisów w obsługiwanym języku ({0} kwestii)', len(cues)))
    return cues, lang


def pick_source_cues(mkv, streams, track, tmp, source="ru"):
    """Zwraca (kwestie, język) ze ścieżki napisów w języku źródłowym (source: kod języka albo "auto").

    "auto": pierwszeństwo ma rosyjski, potem angielski i ukraiński — wybór po treści, nie tylko po znaczniku.
    """
    text_streams = [s for s in streams if s.get("codec_name") in TEXT_CODECS | {TELETEXT}]
    if track is not None:
        if not 1 <= track <= len(streams):
            raise FatalError(tr('--track {0}: plik ma {1} ścieżek napisów', track, len(streams)))
        chosen = streams[track - 1]
        if chosen not in text_streams:
            raise FatalError(tr('ścieżka {0} to napisy graficzne ({1}) — wymagają OCR', track, chosen.get('codec_name')))
        candidates = [chosen]
    else:
        if not text_streams:
            pgs = [s for s in streams if s.get("codec_name") == PGS and lang_of(s) not in target_tags()]
            if pgs:
                return ocr_cues(mkv, pgs, tmp, source)
            if streams:
                kinds = ", ".join(sorted({s.get("codec_name", "?") for s in streams}))
                raise FatalError(tr('tylko napisy graficzne ({0}) — wymagają OCR, nie da się ich przetłumaczyć', kinds))
            raise FatalError(tr('brak wbudowanych napisów'))
        # kandydaci: ścieżki oznaczone wybranym językiem, a gdy takich nie ma — wszystkie tekstowe niepolskie
        tags = set().union(*(LANGS[c][2] for c in LANGS)) if source == "auto" else LANGS[source][2]
        candidates = [s for s in text_streams if lang_of(s) in tags] or \
            [s for s in text_streams if lang_of(s) not in target_tags()]
        if not candidates:
            # tekstowe są tylko polskie (np. z wcześniejszego tłumaczenia) — źródłem mogą być napisy graficzne
            pgs = [s for s in streams if s.get("codec_name") == PGS and lang_of(s) not in target_tags()]
            if pgs:
                return ocr_cues(mkv, pgs, tmp, source)
            raise FatalError(tr('brak napisów do tłumaczenia (są tylko {0})', target_adj()))
    # wszystkie kandydatki wyciągamy jednym przebiegiem i bierzemy tę z największą liczbą kwestii:
    # pliki miewają obok pełnych napisów krótką ścieżkę "Forced" (kilka kwestii), której nie wolno wybrać
    teletext = any(s.get("codec_name") == TELETEXT for s in candidates)
    cmd = ["ffmpeg", "-v", "error", "-y", *(TELETEXT_INPUT if teletext else []), "-i", str(mkv)]
    for s in candidates:
        cmd += ["-map", f"0:{s['index']}", "-c:s", "srt", str(Path(tmp) / f"track{s['index']}.srt")]
    res = run(cmd)
    if res.returncode != 0:
        raise FatalError(tr('ffmpeg nie wyodrębnił napisów: {0}', res.stderr.strip()))
    order = source_order(source)
    if not order:
        raise FatalError(tr('język źródłowy jest taki sam jak docelowy'))
    found = []                                # (ścieżka, kwestie, język)
    for s in candidates:
        path = Path(tmp) / f"track{s['index']}.srt"
        cues = parse_srt(path.read_text(encoding="utf-8", errors="replace")) if path.exists() else []
        if s.get("codec_name") == TELETEXT:
            cues = clean_teletext(cues)
        lang = detect_lang([c.text for c in cues])
        if track is not None and cues:
            lang = lang if lang in order else (order[0] if order else lang)
        if cues and lang in order:
            found.append((s, cues, lang))
    # szczątkowe ścieżki ("Forced", kilka kwestii) nie wygrywają z pełnymi napisami w innym języku
    most = max((len(c) for _, c, _ in found), default=0)
    found = [f for f in found if len(f[1]) >= 0.3 * most]
    best = min(found, key=lambda f: (order.index(f[2]), -len(f[1])), default=None)
    if best:
        s, cues, lang = best
        title = s.get("tags", {}).get("title", "")
        print(tr('    ścieżka napisów #{0} [{1}] {2} — {3} kwestii, język: {4}', streams.index(s) + 1, lang_of(s) or '?', title, len(cues), LANGS[lang][0]))
        return cues, lang
    wanted = tr('w żadnym obsługiwanym języku') if source == "auto" else tr('z {0}', tr(LANGS[source][1]))
    raise FatalError(tr('nie znaleziono napisów tekstowych {0} (możesz wskazać ścieżkę opcją --track N)', wanted))


def duration(path):
    res = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


def chapter_count(path):
    res = run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(path)])
    try:
        return len(json.loads(res.stdout or "{}").get("chapters", []))
    except ValueError:
        return None


def kept_flags(path):
    """Liczba flag ścieżek, których program nie może zgubić: wymuszone, dla niesłyszących, komentarz."""
    res = run(["ffprobe", "-v", "error", "-show_entries",
               "stream_disposition=forced,hearing_impaired,comment", "-of", "json", str(path)])
    try:
        return sum(v for st in json.loads(res.stdout or "{}").get("streams", [])
                   for v in st.get("disposition", {}).values())
    except (ValueError, TypeError):
        return 0


def attachment_count(path):
    return len(probe_streams(path, "t"))


BACKUP_LOG = APP_DIR / "kopie_oryginalow.json"   # kopie .orig.mkv zrobione przez program (tylko te są kasowane)
_backup_lock = threading.Lock()


def keep_backup(mkv, days):
    """Przed podmianą: oryginał zostaje jako <nazwa>.orig.mkv (zmiana nazwy — bez kopiowania, natychmiast).
    Program kasuje go sam po `days` dniach; days = 0 wyłącza kopię."""
    if not days:
        return
    backup = mkv.with_suffix(".orig.mkv")
    if backup.exists():                       # starsza kopia tego samego pliku — zostaje najstarszy oryginał
        return
    os.replace(mkv, backup)
    with _backup_lock:
        log = _read_backups()
        log[str(backup)] = time.time() + days * 86400
        _write_backups(log)
    print(tr('    oryginał zachowany jako {0} (zostanie usunięty po {1} dniach)', backup.name, days))


def prune_backups():
    """Kasuje kopie .orig.mkv, którym minął termin (tylko te zapisane w kopie_oryginalow.json)."""
    with _backup_lock:
        log, now, removed = _read_backups(), time.time(), 0
        for path, until in list(log.items()):
            if until <= now:
                try:
                    Path(path).unlink(missing_ok=True)
                    removed += 1
                except OSError:
                    continue                  # np. dysk sieciowy wyłączony — spróbujemy przy następnym starcie
                del log[path]
        if removed:
            _write_backups(log)
            print(tr('Usunięto {0} starych kopii oryginałów (.orig.mkv)', removed))


def _read_backups():
    try:
        return json.loads(BACKUP_LOG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_backups(log):
    try:
        tmp = BACKUP_LOG.with_name(BACKUP_LOG.name + ".tmp")
        tmp.write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, BACKUP_LOG)
    except OSError:
        pass


def check_remux(mkv, new, audio=None, subs=None):
    """Zanim nowy plik zastąpi oryginał: musi dać się odczytać, mieć tę samą długość, te same ścieżki obrazu
    i rozdziały oraz oczekiwaną liczbę ścieżek dźwięku i napisów (audio/subs; None = bez sprawdzania)."""
    problems = []
    old_len, new_len = duration(mkv), duration(new)
    if new_len is None or old_len is not None and abs(old_len - new_len) > 2:
        problems.append(tr('długość {0} zamiast {1}', new_len, old_len))
    else:
        for kind, want in (("v", len(probe_streams(mkv, "v"))), ("a", audio), ("s", subs)):
            got = len(probe_streams(new, kind))
            if want is not None and got != want:
                problems.append(tr('ścieżek {0}: {1} zamiast {2}', kind, got, want))
        if chapter_count(new) != chapter_count(mkv):
            problems.append(tr('brak rozdziałów'))
        if kept_flags(new) < kept_flags(mkv):
            problems.append(tr('utracone flagi ścieżek (wymuszone / dla niesłyszących)'))
        if attachment_count(new) != attachment_count(mkv):
            problems.append(tr('brak załączników (czcionek napisów)'))
    if problems:
        new.unlink(missing_ok=True)
        raise FatalError(tr('złożony plik nie przeszedł kontroli ({0}) — oryginał pozostał bez zmian', '; '.join(problems)))


def read_text_any(path):
    """Czyta plik napisów w UTF-8, UTF-16 albo (typowe dla rosyjskich napisów) Windows-1251."""
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


# słowa dozwolone między nazwą filmu a rozszerzeniem pliku towarzyszącego ("Film.rus.forced.srt", "Film.pl.part.json")
COMPANION_TAGS = {"pl", "pol", "polish", "polski", "ru", "rus", "russian", "рус", "русский", "en", "eng", "english",
                  "forced", "full", "sdh", "sub", "subs", "napisy", "bak", "rozpoznane", "part", "lektor", "tmp"}


def is_companion(f, stem):
    """Czy plik należy do filmu o nazwie stem: napisy, pliki pośrednie programu, .nfo itp.

    "Film 2.pl.srt" ani "Film.Part2.mkv" nie należą do filmu "Film" — inne filmy nigdy nie są plikami towarzyszącymi.
    """
    name = f.name
    if not name.lower().startswith(stem.lower()) or f.suffix.lower() in VIDEO_EXTS and f.is_file():
        return False
    rest = name[len(stem):]
    if not rest or rest[0] not in "._- [(":
        return False
    middle = rest[:-len(f.suffix)] if f.suffix else rest
    return all(tok in COMPANION_TAGS for tok in re.findall(r"[^\W_]+", middle.lower()))


def external_subs(mkv):
    """Pliki napisów należące do filmu: o tej samej nazwie (także w podfolderach) albo w folderze o nazwie filmu."""
    stem = mkv.stem.lower()
    found = []
    for f in mkv.parent.rglob("*"):
        if f.suffix.lower() not in SUB_EXTS or not f.is_file():
            continue
        name = f.stem.lower()
        own = is_companion(f, mkv.stem)       # nie "odc 1" -> "odc 10" ani "Film" -> "Film 2"
        if not (own or f.parent.name.lower() == stem):
            continue
        tags = set(re.split(r"[^a-zа-я]+", name[len(stem):] if own else name))
        if tags & {"pl", "pol", "polish", "polski"}:
            continue                          # polskie napisy, w tym wyniki tego programu
        tagged = bool(tags & set().union(*(LANGS[c][3] for c in LANGS)))   # "Film.rus.srt", "Film.en.srt"
        found.append((not tagged, len(f.parts), f.name.lower(), f))
    return [f for *_, f in sorted(found)]


def load_sub_cues(f, tmp):
    """Kwestie z pliku napisów w dowolnym obsługiwanym formacie; None, gdy pliku nie da się odczytać."""
    try:
        text = read_text_any(f)
    except OSError:
        return None
    if f.suffix.lower() != ".srt":            # ASS, VTT itd. — konwersja przez ffmpeg
        src = Path(tmp) / f"napisy{f.suffix.lower()}"
        out = Path(tmp) / "napisy.srt"
        src.write_text(text, encoding="utf-8")
        if run(["ffmpeg", "-v", "error", "-y", "-i", str(src), str(out)]).returncode != 0:
            return None                       # np. graficzny .sub (VobSub)
        text = out.read_text(encoding="utf-8", errors="replace")
    return parse_srt(text) or None


# słowa częste w polskich dialogach, a rzadkie w innych językach (bez "i", "to", "na" — są też w angielskim)
POLISH_WORDS = {"nie", "się", "jest", "że", "już", "tak", "ale", "czy", "jak", "mnie", "mi", "ty", "jestem",
                "tylko", "dobrze", "wiem", "może", "był", "było", "tu", "tego", "teraz", "bo", "gdzie", "coś"}
POLISH_TITLE_RE = re.compile(r"\b(pl|pol|polish|polski|polskie)\b", re.IGNORECASE)
UNTAGGED = {"", "und", "unk", "mis"}


def is_polish(cues):
    """Czy napisy są w języku docelowym (nazwa historyczna: dla celu "en" sprawdza angielski)."""
    if OUT["code"] != "pl":
        return detect_lang([c.text for c in cues[:300]]) == OUT["code"]
    text = " ".join(c.text for c in cues[:300]).lower()
    words = re.findall(r"[a-ząćęłńóśźż]+", text)
    if len(words) < 20 or cyrillic_ratio(text) > 0.1:
        return False
    letters = sum(ch.isalpha() for ch in text)
    marks = sum(ch in "ąćęłńśźż" for ch in text)
    return sum(w in POLISH_WORDS for w in words) / len(words) > 0.06 and marks / letters > 0.005


def polish_embedded(mkv, streams):
    """Opis wbudowanej polskiej ścieżki napisów albo None.

    Liczy się znacznik języka, tytuł ścieżki ("Polski", "Polish", "PL"), a dla ścieżek tekstowych bez
    oznaczenia języka — treść pierwszych 15 minut.
    """
    for n, s in enumerate(streams, 1):
        title = s.get("tags", {}).get("title") or ""
        if lang_of(s) in target_tags():
            return tr('ścieżka #{0} [{1}]', n, lang_of(s))
        if set(re.findall(r"\w+", title.lower())) & target_words():
            return tr('ścieżka #{0} „{1}”', n, title)
    unknown = [(n, s) for n, s in enumerate(streams, 1)
               if lang_of(s) in UNTAGGED and s.get("codec_name") in TEXT_CODECS]
    if not unknown:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["ffmpeg", "-v", "error", "-y", "-t", "900", "-i", str(mkv)]
        for n, s in unknown:
            cmd += ["-map", f"0:{s['index']}", "-c:s", "srt", str(Path(tmp) / f"t{n}.srt")]
        run(cmd)
        for n, s in unknown:
            out = Path(tmp) / f"t{n}.srt"
            if out.exists() and is_polish(parse_srt(out.read_text(encoding="utf-8", errors="replace"))):
                return tr('ścieżka #{0} bez oznaczenia języka (treść: {1})', n, target_adj())
    return None


def external_polish(mkv):
    """Polski plik napisów leżący obok filmu (po nazwie albo po treści) — inny niż wynik programu; None, gdy brak."""
    stem = mkv.stem.lower()
    own_result = mkv.with_suffix(f".{OUT['code']}.srt")
    others = set().union(*(LANGS[c][3] for c in LANGS), {"pl", "pol", "polish", "polski"}) - target_words()
    with tempfile.TemporaryDirectory() as tmp:
        for f in sorted(mkv.parent.glob("*")):
            if f.suffix.lower() not in SUB_EXTS or not f.is_file() or f == own_result:
                continue
            name = f.stem.lower()
            if not is_companion(f, mkv.stem):
                continue
            tags = set(re.split(r"[^a-zа-я]+", name[len(stem):]))
            if tags & target_words():
                return f
            if not tags & others:
                cues = load_sub_cues(f, tmp)
                if cues and is_polish(cues):
                    return f
    return None


def external_cues(mkv, tmp, source="ru"):
    """(kwestie, język) z pliku napisów leżącego obok filmu w języku źródłowym; None, gdy takiego pliku nie ma."""
    order = source_order(source)
    found = []
    for f in external_subs(mkv):
        cues = load_sub_cues(f, tmp)
        lang = detect_lang([c.text for c in cues]) if cues else None
        if lang in order:
            found.append((order.index(lang), f, cues, lang))
    if not found:
        return None
    _, f, cues, lang = min(found, key=lambda x: x[0])
    print(tr('    napisy z pliku {0} — {1} kwestii, język: {2}', f.relative_to(mkv.parent), len(cues), LANGS[lang][0]))
    return cues, lang


def sub_output(sub):
    """Nazwa polskiego pliku dla samodzielnych napisów: "odcinek_rus.srt" -> "odcinek.pl.srt"."""
    stem = re.sub(r"[._ -](rus|ru|russian|рус|eng|en|english|ukr|uk)$", "", sub.stem, flags=re.IGNORECASE)
    return sub.with_name(f"{stem}.{OUT['code']}.srt")


def process_subtitle(sub, args, engine):
    """Tłumaczy samodzielny plik napisów (bez filmu obok); wynik: <nazwa>.pl.srt w tym samym folderze."""
    print(f"\n[{time.strftime('%H:%M')}] {sub.name}")
    OUT["code"] = getattr(args, "target", "pl")
    out = sub_output(sub)
    part = out.with_name(out.name[:-len(".srt")] + ".part.json")
    if args.restart:
        part.unlink(missing_ok=True)
    if out.exists() and not (args.force or args.resume):
        print(tr('    pomijam: {0} już istnieje (--force wymusza)', out.name))
        return "Pominięto"
    with tempfile.TemporaryDirectory() as tmp:
        cues = load_sub_cues(sub, tmp)
    if not cues:
        raise FatalError(tr('nie udało się odczytać napisów z pliku'))
    source = getattr(args, "source", "ru")
    lang = detect_lang([c.text for c in cues])
    if lang not in source_order(source):
        wanted = tr('w obsługiwanym języku') if source == "auto" else tr('z {0}', tr(LANGS[source][1]))
        raise FatalError(tr('to nie są napisy {0} (rozpoznano: {1})', wanted, LANGS[lang][0] if lang else tr('inny język')))
    engine.lang = lang
    print(tr('    {0} kwestii, język: {1}', len(cues), LANGS[lang][0]))
    quality = {}
    write_srt(translate_cues(cues, engine, part, args.resume or not args.force, glossary_file(sub), quality), out)
    report_quality(sub, quality["verdict"], quality["summary"], len(cues), "napisy")
    print(tr('    zapisano {0}', out.name))
    return quality["verdict"]


def is_standalone_sub(f, videos):
    """Czy plik napisów trzeba tłumaczyć osobno: nie jest polski, nie jest wynikiem programu i nie ma filmu obok."""
    if f.suffix.lower() not in SUB_EXTS:
        return False
    name = f.stem.lower()
    if re.search(r"(^|[._ -])(pl|pol|polish|polski|en|eng|english)$", name) or name.endswith(".rozpoznane.ru"):
        return False
    return not any(name.startswith(v) for v in videos)


LANG_WINDOWS = 8                              # tyle 30-sekundowych próbek głosuje o języku mowy


def detect_speech_language(mkv, audio_index, tmp, center=False):
    """Język mowy ścieżki: Whisper głosuje na LANG_WINDOWS próbkach rozłożonych po filmie (10–90% długości).

    Jedna próbka zawodziła: piosenka albo rosyjski lektor na angielskim oryginale dawały zły język.
    Przy 5.1 próbki są z kanału centralnego (dialogi), tak jak samo rozpoznawanie.
    Film niemy albo sama muzyka (niska pewność) i języki nieobsługiwane kończą się błędem zamiast tłumaczenia.
    """
    sample, result = Path(tmp) / "jezyk.wav", Path(tmp) / "jezyk.txt"
    length = duration(mkv) or 0
    if length > 300:
        starts = [length * (0.1 + 0.8 * k / (LANG_WINDOWS - 1)) for k in range(LANG_WINDOWS)]
        # jeden przebieg ffmpeg: z każdego miejsca 30 s, sklejone w jedną próbkę (detect dzieli ją po 30 s)
        cmd = ["ffmpeg", "-v", "error", "-y"]
        for t in starts:
            cmd += ["-ss", f"{t:.0f}", "-t", "30", "-i", str(mkv)]
        pan = ",pan=mono|c0=FC" if center else ""
        chains = "".join(f"[{k}:a:{audio_index}]aresample=16000{pan},aformat=channel_layouts=mono[a{k}];"
                         for k in range(len(starts)))
        cmd += ["-filter_complex", chains + "".join(f"[a{k}]" for k in range(len(starts)))
                + f"concat=n={len(starts)}:v=0:a=1[out]", "-map", "[out]", "-ar", "16000", "-ac", "1", str(sample)]
        res = run(cmd)
        if (res.returncode != 0 or not sample.exists()) and center:
            return detect_speech_language(mkv, audio_index, tmp)   # układ kanałów bez FC — zwykły miks
    else:
        res = run(["ffmpeg", "-v", "error", "-y", "-i", str(mkv), "-map", f"0:a:{audio_index}",
                   "-ar", "16000", "-ac", "1", str(sample)])
    if res.returncode != 0 or not sample.exists() or sample.stat().st_size < 16000 * 2 * 20:
        raise FatalError(tr('nie udało się wyodrębnić dźwięku do rozpoznania języka'))
    run_helper(["detect", sample, result, "--windows", str(LANG_WINDOWS)])
    code, prob = result.read_text(encoding="utf-8").split()[:2]
    if float(prob) < 0.5:
        raise FatalError(tr('nie da się pewnie rozpoznać języka mowy ({0}, {1:.0%}) — film niemy, sama muzyka albo kilka języków; wybierz język ręcznie w ustawieniach', code, float(prob)))
    if code not in LANGS:
        raise FatalError(tr('mowa w języku „{0}” — obsługiwane: {1}', code, ', '.join(LANGS)))
    return code


ASR_PICK_PROMPT = """Dwa systemy rozpoznawania mowy zapisały te same kwestie z filmu inaczej (wersje "A" i "B").
Wersja A ma dokładne granice kwestii; w B słowa z początku albo końca bywają przesunięte do sąsiedniej kwestii,
więc brak słów w B na brzegach nie jest błędem A. Dla każdej kwestii zwróć poprawny tekst w języku oryginału:
zostaw A, chyba że B wyraźnie poprawia źle usłyszane słowo (wtedy popraw tylko to słowo w A). Nie usuwaj słów
z A, nie dodawaj słów, których nie ma w żadnej wersji, nie streszczaj i nie tłumacz. Pole "kontekst" to sąsiednie
kwestie tylko do wglądu. Zwróć wyłącznie obiekt JSON: te same klucze co w "kwestie", wartości to tekst."""
ASR_PICK_BATCH = 40


ASR_NAMES = {"gigaam": "GigaAM", "whisper": "Whisper", "gigaam+whisper": "GigaAM + Whisper",
             "gigaam-rnnt": "GigaAM RNNT + interpunkcja z modelu językowego"}
ASR_ENGINES = {"gigaam+whisper": "ensemble", "gigaam-rnnt": "gigaam"}   # nazwa w programie -> silnik audio_tools.py


PUNCT_PROMPT = """Poniżej są kwestie z automatycznego rozpoznawania mowy z filmu, bez interpunkcji i wielkich liter.
Dodaj interpunkcję (kropki, przecinki, znaki zapytania i wykrzykniki, wielokropki, myślniki dialogowe) i wielkie
litery (początek zdania, imiona, nazwy). NIE zmieniaj, nie dodawaj, nie usuwaj i nie przestawiaj żadnych słów.
Pole "kontekst" to sąsiednie kwestie tylko do wglądu. Zwróć wyłącznie obiekt JSON: te same klucze co w "kwestie"."""
PUNCT_BATCH = 60


def punctuate_asr(srt_path, engine):
    """B9: interpunkcja i wielkie litery dla GigaAM RNNT (dokładniejsze słowa, ale bez interpunkcji).
    Model językowy wstawia tylko znaki — kwestia, w której zmieniłby choć jedno słowo, zostaje bez zmian."""
    model = engine if isinstance(engine, OllamaEngine) and not engine.line_mode else repair_engine(engine)
    if model is None:
        return
    words = lambda t: re.findall(r"[\w']+", t.lower().replace("ё", "е"))
    cues = parse_srt(Path(srt_path).read_text(encoding="utf-8", errors="replace"))
    fixed = 0
    for pos in range(0, len(cues), PUNCT_BATCH):
        if cancel.is_set():
            raise Cancelled
        part = range(pos, min(pos + PUNCT_BATCH, len(cues)))
        payload = {"kontekst": [cues[j].text for j in range(max(0, pos - 3), pos)],
                   "kwestie": {str(k + 1): cues[k].text for k in part}}
        schema = {"type": "object", "properties": {str(k + 1): {"type": "string"} for k in part},
                  "required": [str(k + 1) for k in part]}
        try:
            raw = model._chat([{"role": "system", "content": PUNCT_PROMPT},
                               {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], fmt=schema)
            answer = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        except (ValueError, FatalError):
            continue
        for k in part:
            text = str(answer.get(str(k + 1), "")).strip()
            if text and words(text) == words(cues[k].text) and text != cues[k].text:
                cues[k].text = text
                fixed += 1
    write_srt(cues, srt_path)
    print(tr('    interpunkcja dodana w {0} z {1} kwestii', fixed, len(cues)))


WINDOW_PROMPT = """Dwa systemy rozpoznawania mowy zapisały te same fragmenty filmu (każdy ok. minuty) — wersje "A" i "B".
Dla każdego fragmentu oceń, który zapis jest wierniejszy mowie: spójne, sensowne zdania, poprawne słowa i imiona,
brak bełkotu, urwanych wyrazów i powtórzeń. Różnice w interpunkcji nie mają znaczenia. Odpowiedz wyłącznie obiektem
JSON: klucze jak w "fragmenty", wartości "A" albo "B"."""
WINDOW_SECONDS = 60


def resolve_windows(srt_path, engine):
    """B8 (wersja 2): dla każdej minuty filmu wybiera cały zapis GigaAM (A) albo Whispera (B, plik <srt>.whisper.srt).
    Pomiar B1 pokazał, że modele wygrywają całymi fragmentami (piosenka, hałas, szybki dialog), nie pojedynczymi kwestiami."""
    other = Path(str(srt_path) + ".whisper.srt")
    if not other.exists():
        return
    model = engine if isinstance(engine, OllamaEngine) and not engine.line_mode else repair_engine(engine)
    a = parse_srt(Path(srt_path).read_text(encoding="utf-8", errors="replace"))
    b = parse_srt(other.read_text(encoding="utf-8", errors="replace"))
    if model is None or not a or not b:
        other.unlink(missing_ok=True)
        return
    sec = lambda c: sum(float(x) * m for x, m in zip(c.start.replace(",", ".").split(":"), (3600, 60, 1)))
    window = lambda c: int(sec(c) // WINDOW_SECONDS)
    keys = sorted({window(c) for c in a} | {window(c) for c in b})
    texts = {k: (" ".join(c.text for c in a if window(c) == k), " ".join(c.text for c in b if window(c) == k)) for k in keys}
    disputed = [k for k in keys if texts[k][0] != texts[k][1] and texts[k][0] and texts[k][1]]
    pick = {k: "A" if texts[k][0] else "B" for k in keys}
    for pos in range(0, len(disputed), 10):
        if cancel.is_set():
            raise Cancelled
        part = disputed[pos:pos + 10]
        payload = {"fragmenty": {str(k): {"A": texts[k][0], "B": texts[k][1]} for k in part}}
        schema = {"type": "object", "properties": {str(k): {"type": "string", "enum": ["A", "B"]} for k in part},
                  "required": [str(k) for k in part]}
        try:
            raw = model._chat([{"role": "system", "content": WINDOW_PROMPT},
                               {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], fmt=schema)
            answer = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            pick.update({k: answer.get(str(k), "A") for k in part})
        except (ValueError, FatalError):
            continue                          # ta partia zostaje z zapisem GigaAM
    merged = [c for c in a if pick.get(window(c)) == "A"] + [c for c in b if pick.get(window(c)) == "B"]
    merged.sort(key=sec)
    write_srt(merged, srt_path)
    other.unlink(missing_ok=True)
    chosen_b = sum(pick[k] == "B" for k in disputed)
    print(tr('    dwa modele: {0} minut porównanych, Whisper wybrany w {1}', len(disputed), chosen_b))


def resolve_asr_variants(srt_path, engine):
    """B8: rozstrzyga kwestie, w których GigaAM i Whisper usłyszeli co innego (plik <srt>.warianty.json).
    Model językowy wybiera wersję albo minimalnie je łączy; odpowiedź niepodobna do żadnej wersji jest odrzucana."""
    import difflib
    var_file = Path(str(srt_path) + ".warianty.json")
    try:
        variants = {int(k): v for k, v in json.loads(var_file.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError):
        return
    model = engine if isinstance(engine, OllamaEngine) and not engine.line_mode else repair_engine(engine)
    if variants and model is not None:
        cues = parse_srt(Path(srt_path).read_text(encoding="utf-8", errors="replace"))
        ids = sorted(k for k in variants if 1 <= k <= len(cues))
        picked = {"A": 0, "B": 0, "łączone": 0}
        for pos in range(0, len(ids), ASR_PICK_BATCH):
            if cancel.is_set():
                raise Cancelled
            part = ids[pos:pos + ASR_PICK_BATCH]
            near = sorted({j for k in part for j in (k - 2, k - 1, k + 1) if 1 <= j <= len(cues) and j not in variants})
            payload = {"kontekst": {str(j): cues[j - 1].text for j in near},
                       "kwestie": {str(k): {"A": variants[k]["gigaam"], "B": variants[k]["whisper"]} for k in part}}
            schema = {"type": "object", "properties": {str(k): {"type": "string"} for k in part},
                      "required": [str(k) for k in part]}
            try:
                raw = model._chat([{"role": "system", "content": ASR_PICK_PROMPT},
                                   {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], fmt=schema)
                answer = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
            except (ValueError, FatalError):
                continue                          # ta partia zostaje z wersją GigaAM
            for k in part:
                text = str(answer.get(str(k), "")).strip()
                a, b = variants[k]["gigaam"], variants[k]["whisper"]
                sim = [difflib.SequenceMatcher(None, text.lower(), x.lower()).ratio() for x in (a, b)]
                if not text or max(sim) < 0.5:
                    continue                      # model wymyślił coś trzeciego — zostaje GigaAM
                if len(text.split()) < 0.8 * len(a.split()):
                    continue                      # wybór gubi słowa, które GigaAM usłyszał — zostaje GigaAM
                cues[k - 1].text = text
                picked["A" if sim[0] >= 0.99 else "B" if sim[1] >= 0.99 else "łączone"] += 1
        write_srt(cues, srt_path)
        print(tr('    rozbieżności dwóch modeli: {0}; wybrano GigaAM {1}, Whisper {2}, połączono {3}',
                 len(ids), picked["A"], picked["B"], picked["łączone"]))
    var_file.unlink(missing_ok=True)


ASR_UNSURE = 0.6                             # pewność rozpoznania kwestii, poniżej której warto ją odsłuchać
ASR_UNSURE_SHARE = 0.15                      # przy takim udziale niepewnych kwestii plik idzie "Do sprawdzenia"


def transcribe(mkv, audio, args, tmp, engine=None, info=None):
    """Rozpoznaje mowę ze ścieżki dźwiękowej; zwraca (kwestie, język). Wynik zostaje w <nazwa>.rozpoznane.ru.srt
    (nazwa historyczna — plik może zawierać też inny język).

    GigaAM zna tylko rosyjski: dla innych języków mowę rozpoznaje Whisper. Nieoznaczona ścieżka przy "auto"
    jest traktowana jak rosyjska (tak jest w większości biblioteki); dla angielskiej wybierz język w ustawieniach.
    info: słownik pliku, do którego trafia ścieżka audio, mówcy i pewność rozpoznania (D3).
    """
    info = {} if info is None else info
    ru_srt = mkv.with_suffix(".rozpoznane.ru.srt")
    meta_file = ru_srt.with_name(ru_srt.name[:-len(".srt")] + ".json")   # czym i w jakim języku powstał
    source = getattr(args, "source", "ru")
    reuse = ru_srt.exists() and (args.resume or not args.force)
    if reuse:
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = None                       # plik ze starszej wersji programu — używany jak dotąd
        if meta and (meta.get("asr") != args.asr and meta.get("lang") == "ru"
                     or getattr(args, "diarize", False) and not meta.get("diarize")
                     or source != "auto" and meta.get("lang") != source):
            print(tr('    {0} powstał innym silnikiem albo w innym języku — rozpoznaję mowę od nowa', ru_srt.name))
            reuse = False
    if reuse:
        print(tr('    używam istniejącego {0}', ru_srt.name))
        if meta and meta.get("audio") is not None:
            info["audio_index"] = meta["audio"]
    else:
        if not audio:
            raise FatalError(tr('plik nie ma ścieżki dźwiękowej'))
        wav = Path(tmp) / "audio.wav"
        src, lang = main_audio(audio, source)
        info["audio_index"] = src         # D3: lektor ściszy tę samą ścieżkę, z której rozpoznano mowę
        surround = audio[src].get("channels", 2) >= 6
        print(tr('    dźwięk: ścieżka #{0} [{1}], {2} kan. {3}', src + 1, lang_of(audio[src]) or '?',
                 audio[src].get("channels", '?'), tr('— kanał centralny (dialogi)') if surround else ''))
        ollama_unload()                       # cała pamięć karty dla modeli rozpoznawania mowy
        if source == "auto":
            # znacznik języka bywa błędny (rosyjskie seriale oznaczone "eng") — decyduje próbka mowy
            tagged = lang
            try:
                lang = detect_speech_language(mkv, src, tmp, center=surround)
            except FatalError as e:
                if tagged is None:
                    raise
                print(tr('    nie udało się sprawdzić języka mowy ({0}) — zostaje oznaczenie ścieżki', e))
                lang = tagged
            if tagged and tagged != lang:
                print(tr('    ścieżka oznaczona „{0}”, rozpoznana mowa: {1} — używam rozpoznanego języka',
                         lang_of(audio[src]), LANGS[lang][0]))
        elif lang is None:
            lang = source
        if lang == OUT["code"]:
            raise FatalError(tr('mowa jest już w języku docelowym'))
        asr = args.asr if lang == "ru" else "whisper"
        print(tr('    mowa: {0}, rozpoznawanie: {1}', LANGS[lang][0], ASR_NAMES.get(asr, asr)))
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(mkv), "-map", f"0:a:{src}"]
        res, helper = None, ["transcribe", wav, ru_srt, "--engine", ASR_ENGINES.get(asr, asr), "--language", lang]
        if getattr(args, "diarize", False):   # B12: mówcy, myślniki dialogowe, płeć mówiącego
            helper.append("--diarize")
        if asr == "gigaam-rnnt":              # B9: dokładniejsze słowa, interpunkcję wstawia model językowy
            helper += ["--gigaam-model", "gigaam-v3-rnnt"]
        if asr in ("whisper", "gigaam+whisper"):   # B11/B10: partie (9x szybciej, 12,1% zamiast 12,6%) + dokładne czasy
            helper += ["--whisper-batch", "16"] + (["--align"] if lang in ("ru", "en") else [])
        if asr not in ("gigaam", "gigaam-rnnt") and getattr(args, "whisper_model", None):
            helper += ["--model", args.whisper_model]
        if surround:
            # w dźwięku 5.1/7.1 dialogi są w kanale centralnym — ale bywa w nim też muzyka i efekty,
            # więc przy oddzielaniu głosu Demucs dostaje sam kanał centralny jako stereo
            res = run(cmd + (["-ar", "44100", "-af", "pan=stereo|c0=FC|c1=FC"] if args.separate
                             else ["-ar", "16000", "-af", "pan=mono|c0=FC"]) + [str(wav)])
            if res.returncode != 0:
                print(tr('    układ kanałów bez kanału centralnego — używam całego miksu'))
        if res is None or res.returncode != 0:
            res = run(cmd + (["-ar", "44100", "-ac", "2"] if args.separate else ["-ar", "16000", "-ac", "1"])
                      + [str(wav)])
        if res.returncode != 0:
            raise FatalError(tr('ffmpeg nie wyodrębnił dźwięku: {0}', res.stderr.strip()))
        if args.separate:                     # Demucs oddziela głos — potrzebuje stereo 44,1 kHz
            helper.append("--separate")
        run_helper(helper)
        free_helper()                         # pamięć karty wraca do Ollamy przed tłumaczeniem
        resolve_windows(ru_srt, engine)       # dwa modele: lepszy zapis dla każdej minuty wybiera model językowy
        if asr == "gigaam-rnnt":
            punctuate_asr(ru_srt, engine)
        meta = {"asr": asr, "lang": lang, "diarize": bool(getattr(args, "diarize", False)), "audio": src, "model": getattr(args, "whisper_model", None) if asr == "whisper" else "gigaam"}
        meta_file.write_text(json.dumps(meta), encoding="utf-8")
    cues = parse_srt(ru_srt.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        ru_srt.unlink(missing_ok=True)        # pusty wynik nie może być użyty przy następnej próbie
        meta_file.unlink(missing_ok=True)
        raise FatalError(tr('w dźwięku nie rozpoznano mowy'))
    lang = detect_lang([c.text for c in cues]) or ("ru" if source == "auto" else source)
    # B7: pewność rozpoznania każdej kwestii (z audio_tools) — niepewne kwestie trafiają do raportu jakości
    try:
        conf = json.loads(Path(str(ru_srt) + ".pewnosc.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        conf = []
    try:
        speakers = json.loads(Path(str(ru_srt) + ".mowcy.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        speakers = None
    if speakers and len(speakers) == len(cues):
        info["speakers"] = speakers
    if len(conf) == len(cues):
        low = [c.text for c, p in zip(cues, conf) if p is not None and p < ASR_UNSURE]
        info.update(unsure=len(low), total=len(cues), examples=low[:2])
        if low:
            print(tr('    niepewne rozpoznanie mowy: {0} z {1} kwestii (pewność < {2:.0%})', len(low), len(cues), ASR_UNSURE))
    return cues, lang


def process(mkv, args, engine):
    """Przetwarza jeden plik; zwraca "Gotowe", "Do sprawdzenia" (kontrola jakości znalazła podejrzane miejsca),
    "Ma polskie" (polskie napisy już są) albo "Pominięto"."""
    if mkv.suffix.lower() in SUB_EXTS:
        return process_subtitle(mkv, args, engine)
    print(f"\n[{time.strftime('%H:%M')}] {mkv.name}")
    OUT["code"] = code = getattr(args, "target", "pl")
    if args.lektor and code != "pl":
        print(tr('    lektor jest dostępny tylko dla napisów polskich — pomijam go'))
        args = copy.copy(args)
        args.lektor = None
    sidecar = mkv.suffix.lower() != ".mkv"
    if sidecar:
        # do AVI/MP4/TS nie wbudowujemy napisów ani lektora — wynikiem jest plik .pl.srt obok filmu
        if args.mux or args.lektor:
            print(tr('    plik {0}: napisy zostaną zapisane obok filmu (.{1}.srt); wbudowanie i lektor działają tylko dla MKV (TS można zamienić na MKV)', mkv.suffix.upper()[1:], code))
        args = copy.copy(args)
        args.mux, args.lektor, args.replace = False, None, False
    srt_out = mkv.with_suffix(f".{code}.srt")
    if srt_out.exists() and (args.restart or args.force and not args.resume):
        # tłumaczenie od nowa: wcześniejszy .pl.srt (może być ręcznie poprawiony) zostaje jako kopia
        backup = mkv.with_suffix(f".{code}.bak.srt")
        os.replace(srt_out, backup)
        print(tr('    poprzednie napisy zachowane jako {0}', backup.name))
    # istniejący wcześniej plik .pl.srt może być własnością użytkownika — takiego nie sprzątamy
    own_srt = not srt_out.exists() or args.resume
    # przy podmianie oryginału plik .pl.tmp.mkv istnieje tylko na czas składania
    mkv_out = mkv.with_suffix(f".{code}.tmp.mkv" if args.replace else f".{code}.mkv")
    part = mkv.with_suffix(f".{code}.part.json")   # częściowe tłumaczenie przerwanego pliku
    if args.restart:                          # "od nowa": bez wyników pośrednich przerwanego przebiegu
        for leftover in (part, mkv.with_suffix(".rozpoznane.ru.srt")):
            leftover.unlink(missing_ok=True)
    voice_part = mkv.with_suffix(".lektor.part")   # gotowe kwestie lektora z przerwanego przebiegu
    if args.restart or args.force and not args.resume:
        shutil.rmtree(voice_part, ignore_errors=True)
    streams = subtitle_streams(mkv)
    audio = probe_streams(mkv, "a")
    want_mkv = args.mux or bool(args.lektor)
    pl_subs = [s for s in streams if lang_of(s) in target_tags() and s.get("codec_name") in TEXT_CODECS]
    revoice = bool(getattr(args, "revoice", False) and args.lektor)

    if not args.force:
        # --revoice: lektor nagrywany od nowa (np. Piper -> XTTS) z gotowych polskich napisów
        if args.lektor and not revoice and any(lang_of(a) in POL_TAGS for a in audio):
            print(tr('    pomijam: plik ma już polską ścieżkę dźwiękową (--force wymusza)'))
            return "Pominięto"
        found = None if args.lektor else polish_embedded(mkv, streams)
        if found:
            print(tr('    pomijam: plik ma już wbudowane {0} napisy — {1} (--force wymusza)', target_adj(), found))
            return "Ma polskie"
        if want_mkv and not args.replace and mkv_out.exists():
            print(tr('    pomijam: już przetworzony (--force wymusza)'))
            return "Pominięto"
        if not want_mkv and srt_out.exists():
            print(tr('    pomijam: {0} już istnieje (--force wymusza)', srt_out.name))
            return "Pominięto"
    # polskie napisy w osobnym pliku obok filmu: zamiast tłumaczyć, wbudowujemy je (albo pomijamy film)
    ext_pl = None if args.force or srt_out.exists() or pl_subs else external_polish(mkv)
    if ext_pl and not want_mkv:
        print(tr('    pomijam: obok filmu są już {0} napisy — {1} (--force wymusza)', target_adj(), ext_pl.name))
        return "Ma polskie"

    quality = {"verdict": "Gotowe"}           # ocena tłumaczenia (gdy nic nie tłumaczymy — zostaje "Gotowe")
    asr = {}                                  # D3: wynik rozpoznania mowy tego pliku (ścieżka, mówcy, pewność)
    with tempfile.TemporaryDirectory() as tmp:
        if ext_pl and (cues := load_sub_cues(ext_pl, tmp)):
            write_srt(cues, srt_out)              # kopia robocza; plik użytkownika zostaje nietknięty
            print(tr('    używam gotowych napisów z pliku {0} ({1} kwestii) — bez tłumaczenia', ext_pl.name, len(cues)))
        elif srt_out.exists() and (args.resume or not args.force):
            print(tr('    używam istniejącego {0}', srt_out.name))
        elif pl_subs and not args.force:
            extract_srt(mkv, pl_subs[0]["index"], srt_out)   # lektor z gotowych polskich napisów
            print(tr('    używam gotowych napisów z filmu ({0})', srt_out.name))
        else:
            source = getattr(args, "source", "ru")
            from_speech = False               # C7: kwestie z rozpoznanej mowy tłumaczymy całymi zdaniami
            try:
                cues, lang = pick_source_cues(mkv, streams, args.track, tmp, source)
            except FatalError as e:
                # brak napisów w pliku: najpierw napisy z osobnego pliku obok filmu, potem mowa z dźwięku
                found = None if args.track is not None else external_cues(mkv, tmp, source)
                if found is None:
                    if not args.whisper or args.track is not None:
                        raise
                    print(tr('    {0} — rozpoznaję mowę z dźwięku', e))
                    found = transcribe(mkv, audio, args, tmp, engine, asr)
                    from_speech = True
                cues, lang = found
            else:
                # napisy szczątkowe (np. sama ścieżka "Forced" albo urwane): poniżej 1,5 kwestii na minutę
                # filmu to za mało na pełne dialogi — wtedy lepsze jest rozpoznanie mowy z dźwięku
                minutes = (duration(mkv) or 0) / 60
                if args.whisper and args.track is None and audio and len(cues) < 1.5 * minutes:
                    print(tr('    napisy mają tylko {0} kwestii na {1:.0f} min filmu — rozpoznaję mowę z dźwięku', len(cues), minutes))
                    try:
                        spoken, spoken_lang = transcribe(mkv, audio, args, tmp, engine, asr)
                        if len(spoken) > len(cues):
                            cues, lang = spoken, spoken_lang
                            from_speech = True
                    except FatalError as e:
                        print(tr('    rozpoznawanie mowy nie powiodło się ({0}) — zostają napisy z pliku', e))
            engine.lang = lang
            translate = translate_sentences if from_speech and os.environ.get("MKV_SENTENCES", "1") != "0" \
                else translate_cues
            write_srt(translate(cues, engine, part, args.resume or not args.force,
                                glossary_file(mkv), quality, asr.get("speakers")), srt_out)
            if asr.get("unsure"):        # B7: niepewne rozpoznanie mowy w ocenie pliku
                quality["summary"] += tr(', niepewne rozpoznanie mowy: {0} kwestii', asr["unsure"])
                if asr["unsure"] > ASR_UNSURE_SHARE * asr["total"]:
                    quality["verdict"] = "Do sprawdzenia"
            report_quality(mkv, quality["verdict"], quality["summary"], len(cues),
                           f"{SPEECH_SOURCE} ({args.asr})" if from_speech else "napisy")
            print(tr('    zapisano {0}', srt_out.name))
            if sidecar:
                mkv.with_suffix(".rozpoznane.ru.srt").unlink(missing_ok=True)   # plik pośredni

        voice = None
        if args.lektor:
            if not audio:
                raise FatalError(tr('plik nie ma ścieżki dźwiękowej, do której można dodać lektora'))
            voice = Path(tmp) / "lektor.wav"
            helper = ["tts", srt_out, voice, "--engine", args.lektor, "--cache", voice_part]
            run_helper(helper + (["--voice", args.voice] if args.voice else []))
        if want_mkv:
            print(tr('    składam plik MKV...'))
            # ponowne tłumaczenie zastępuje ścieżki dodane poprzednio przez program
            old_subs = [streams.index(s) for s in pl_subs
                        if s.get("tags", {}).get("title") == TARGETS[code][3]] if args.force else []
            old_audio = [i for i, a in enumerate(audio)
                         if a.get("tags", {}).get("title") == "Polski lektor"] if (args.force or revoice) and voice else []
            new_srt = srt_out if args.force or not pl_subs else None
            mux(mkv, mkv_out, new_srt, len(streams), voice, audio, args.replace_audio, old_subs, old_audio,
                asr.get("audio_index"))
            if args.replace:
                # oczekiwane ścieżki: oryginał minus zastępowane ścieżki programu plus nowe
                want_audio = 1 if voice and args.replace_audio else len(audio) - len(old_audio) + bool(voice)
                want_subs = len(streams) - len(old_subs) + bool(new_srt)
                check_remux(mkv, mkv_out, want_audio, want_subs)
                try:
                    keep_backup(mkv, getattr(args, "backup_days", 0))
                    os.replace(mkv_out, mkv)
                except OSError as e:              # np. film otwarty w odtwarzaczu
                    backup = mkv.with_suffix(".orig.mkv")
                    if not mkv.exists() and backup.exists():
                        os.replace(backup, mkv)       # oryginał wraca pod swoją nazwę
                    mkv_out.unlink(missing_ok=True)
                    raise FatalError(tr('nie da się podmienić oryginału ({0}) — oryginał pozostał bez zmian', e))
                shutil.rmtree(voice_part, ignore_errors=True)
                # napisy są już w pliku — usuwamy tylko pliki pośrednie utworzone przez program
                for leftover in ([srt_out] if own_srt else []) + [mkv.with_suffix(".rozpoznane.ru.srt"),
                                                                    mkv.with_suffix(".rozpoznane.ru.json"),
                                                                    mkv.with_suffix(".rozpoznane.ru.srt.pewnosc.json"),
                                                                    mkv.with_suffix(".rozpoznane.ru.srt.mowcy.json")]:
                    leftover.unlink(missing_ok=True)
                print(tr('    zapisano w oryginalnym pliku: {0}', mkv.name))
            else:
                shutil.rmtree(voice_part, ignore_errors=True)
                print(tr('    zapisano {0}', mkv_out.name))
    return quality["verdict"]


# ---------------------------------------------------------------- nazwy dla Plex

# "Кулинар-2 (01 сер.)", "Бандитский Петербург-2. Адвокат (01 сер.)", "Академия (01 сер.)"
EPISODE_RE = re.compile(r"^(?P<series>.+?)(?:-(?P<season>\d{1,2}))?(?:\.\s+(?P<part>.+?))?"
                        r"\s*\((?P<ep>\d{1,3})\s*сер\.?\)$", re.IGNORECASE)
SEASON_FOLDER_RES = (r"-(\d{1,2})$",                          # "Кулинар-2"
                     r"^(\d{1,2})\.\s",                       # "02. Адвокат"
                     r"(?:season|sezon|сезон)\s*(\d{1,2})",   # "Season 02"
                     r"^s(\d{1,2})$")                         # "S02"


PLEX_RE = re.compile(r"^(?P<series>.+?) - S(?P<season>\d{1,2})E(?P<ep>\d{1,3})$", re.IGNORECASE)


def plex_name(path, folders=False):
    """Docelowa ścieżka w formacie Plex albo None, gdy nazwy nie da się rozpoznać.

    Nazwa: "Serial - S02E01.mkv"; z folders=True także układ folderów "Serial/Season 02/".
    """
    m = PLEX_RE.match(path.stem.strip())      # plik już nazwany przez program — zostaje samo przenoszenie
    if m:
        season = int(m["season"])
    elif re.search(r"[Ss]\d{1,2}[Ee]\d{1,3}", path.stem):
        return None                           # cudzy format z numerem odcinka (np. wydania sceniczne)
    else:
        m = EPISODE_RE.match(path.stem.strip())
        if not m:
            return None
        season = int(m["season"]) if m["season"] else 1
        if not m["season"]:                   # sezon z nazwy folderu, np. "Шифр-3" albo "02. Адвокат"
            for pattern in SEASON_FOLDER_RES:
                found = re.search(pattern, path.parent.name, re.IGNORECASE)
                if found:
                    season = int(found.group(1))
                    break
    series = m["series"].strip()
    name = f"{series} - S{season:02}E{int(m['ep']):02}{path.suffix}"
    if not folders:
        return path.with_name(name)
    # folder serialu: "Кулинар/Кулинар-2/plik" -> "Кулинар", "Академия/plik" -> "Академия"
    parent = path.parent
    if parent.parent.name.lower() == series.lower():
        show = parent.parent
    elif parent.name.lower() == series.lower():
        show = parent
    else:
        show = parent / series
    return show / f"Season {season:02}" / name


def plex_renames(files, folders=False):
    """Plan zmian: lista (stary plik, nowy plik, uwaga); pusta uwaga = można zmienić."""
    plan, targets = [], set()
    for old in files:
        new = plex_name(old, folders)
        if new is None or new == old:
            continue
        note = ""
        if not old.exists():
            note = tr('brak pliku')
        elif new.exists() or str(new).lower() in targets:
            note = tr('konflikt: taka nazwa już istnieje')
        targets.add(str(new).lower())
        plan.append((old, new, note))
    return plan


def plex_rename(old, new):
    """Zmienia nazwę/położenie filmu razem z plikami towarzyszącymi (napisy, pliki pośrednie programu).

    Zwraca listę wykonanych przeniesień (skąd, dokąd) — do dziennika, z którego da się je cofnąć.
    """
    new.parent.mkdir(parents=True, exist_ok=True)
    companions = [f for f in old.parent.iterdir() if f != old and is_companion(f, old.stem)]
    old.rename(new)                           # najpierw film: gdy jest zablokowany, nic się nie rozjeżdża
    moves = [(str(old), str(new))]
    for f in companions:
        target = new.parent / (new.stem + f.name[len(old.stem):])
        if not target.exists():
            try:
                f.rename(target)
                moves.append((str(f), str(target)))
            except OSError as e:
                print(tr('    nie przeniesiono {0}: {1}', f.name, e))
    if old.parent != new.parent:
        try:
            old.parent.rmdir()                # stary folder sezonu znika tylko wtedy, gdy jest już pusty
        except OSError:
            pass
    return moves


PLEX_LOG = Path(os.environ.get("MKV_PLEX_LOG") or APP_DIR / "plex_nazwy.json")   # dziennik zmian nazw — podstawa "Przywróć stare nazwy"


def plex_log_read(log=None):
    try:
        return [tuple(m) for m in json.loads((log or PLEX_LOG).read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def plex_log_write(moves, log=None):
    log = log or PLEX_LOG
    tmp = log.with_name(log.name + ".tmp")
    tmp.write_text(json.dumps(moves, ensure_ascii=False, indent=0), encoding="utf-8")
    os.replace(tmp, log)


def plex_undo(log=None):
    """Cofa zapisane zmiany nazw od najnowszej; zwraca (cofnięte [(obecna, przywrócona)], liczba nieudanych)."""
    restored, left = [], []
    for src, dst in reversed(plex_log_read(log)):
        src, dst = Path(src), Path(dst)
        if not dst.exists():
            continue                          # pliku już tam nie ma (usunięty albo przeniesiony ręcznie)
        if src.exists():
            left.append((str(src), str(dst)))  # stara nazwa jest zajęta — zostaje w dzienniku
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(src)
        except OSError:
            left.append((str(src), str(dst)))
            continue
        restored.append((dst, src))
        try:
            dst.parent.rmdir()                # pusty folder "Season NN" znika
        except OSError:
            pass
    plex_log_write(list(reversed(left)), log)
    return restored, len(left)


def remux_to_mkv(src, keep_teletext=True):
    """Przepakowuje film (np. TS) do MKV bez przekodowania; polskie napisy .pl.srt obok trafiają do środka.

    keep_teletext=False: napisy telegazety nie trafiają do MKV (np. gdy wystarczą już gotowe polskie).

    Oryginał jest usuwany dopiero po sprawdzeniu nowego pliku. Zwraca ścieżkę do MKV.
    """
    dst = src.with_suffix(".mkv")
    if dst.exists():
        raise FatalError(tr('{0} już istnieje', dst.name))
    tmp = src.with_suffix(".pl.tmp.mkv")
    side = src.with_suffix(".pl.srt")
    subs = probe_streams(src, "s")
    # telegazety MKV nie przyjmuje (błąd "incompatible with output codec id 94215") — zamieniamy ją na SRT,
    # dzięki czemu te napisy da się potem przetłumaczyć bez rozpoznawania mowy
    work = Path(tempfile.mkdtemp())
    extra = []                                # (plik SRT, język, tytuł)
    for s in subs:
        if s.get("codec_name") == TELETEXT and keep_teletext:
            out = work / f"teletekst{s['index']}.srt"
            count = teletext_srt(src, s["index"], out)
            if count:
                extra.append((out, lang_of(s) or "und", "teletekst"))
                print(tr('    telegazeta → napisy SRT: {0} kwestii', count))
    if side.exists():
        extra.append((side, "pol", "Polski"))
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src)]
    for path, _, _ in extra:
        cmd += ["-i", str(path)]
    cmd += ["-map", "0:v", "-map", "0:a?"]
    kept = [s for s in subs if s.get("codec_name") != TELETEXT]
    for s in kept:
        cmd += ["-map", f"0:{s['index']}"]
    for k, (_, lang, title) in enumerate(extra, 1):
        n = str(len(kept) + k - 1)
        cmd += ["-map", f"{k}:0", f"-metadata:s:s:{n}", f"language={lang}", f"-metadata:s:s:{n}", f"title={title}"]
    if side.exists():
        n = str(len(kept) + len(extra) - 1)
        cmd += ["-disposition:s", "0", f"-disposition:s:{n}", "default"]
    # setts: w TS z obrazem z przeplotem (1080i) drugie pole nie ma znacznika PTS, czego MKV nie przyjmuje —
    # uzupełniamy go z DTS (dekodowanie daje te same klatki co oryginał)
    cmd += ["-c", "copy", "-bsf:v", "setts=pts=if(eq(PTS\\,NOPTS)\\,DTS\\,PTS)",
            "-max_interleave_delta", "0", "-ignore_unknown", str(tmp)]
    res = run(cmd)
    shutil.rmtree(work, ignore_errors=True)
    if res.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise FatalError(tr('ffmpeg nie przepakował pliku: {0}', res.stderr.strip()[-300:]))
    try:
        old_len, new_len = duration(src), duration(tmp)
        # ostatnia kwestia napisów może kończyć się kilka sekund po obrazie i wydłużyć plik — to nie błąd
        # bez długości oryginału nie da się sprawdzić kopii — wtedy TS zostaje (nic nie kasujemy na ślepo)
        if new_len is None or old_len is None or not -2 <= new_len - old_len <= (30 if extra else 2):
            raise FatalError(tr('złożony plik ma inną długość niż oryginał — oryginał pozostał bez zmian'))
        if len(probe_streams(tmp, "a")) != len(probe_streams(src, "a")) or not probe_streams(tmp, "v"):
            raise FatalError(tr('w nowym pliku brakuje ścieżek obrazu lub dźwięku'))
    except FatalError:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, dst)
    src.unlink()
    if side.exists():
        side.unlink()                         # napisy są już w MKV
    return dst


TOAST_PS = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x = $t.GetElementsByTagName('text')
$x.Item(0).AppendChild($t.CreateTextNode($env:MKV_TOAST_TITLE)) > $null
$x.Item(1).AppendChild($t.CreateTextNode($env:MKV_TOAST_TEXT)) > $null
$id = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($id).Show([Windows.UI.Notifications.ToastNotification]::new($t))
"""


def notify(title, text, ntfy_topic=None, toast=True):
    """Powiadomienie o końcu pracy: dymek Windows i/lub wiadomość na telefon przez ntfy.sh (aplikacja ntfy).

    Błędy powiadomień nie przerywają pracy — to tylko informacja.
    """
    if toast and os.name == "nt":
        try:
            subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
                             env={**os.environ, "MKV_TOAST_TITLE": title, "MKV_TOAST_TEXT": text},
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            pass
    if ntfy_topic:
        server = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
        body = json.dumps({"topic": ntfy_topic.strip(), "title": title, "message": text}).encode("utf-8")
        try:
            urllib.request.urlopen(urllib.request.Request(server, data=body,
                                                          headers={"Content-Type": "application/json"}), timeout=15)
        except (urllib.error.URLError, OSError) as e:
            print(tr('    powiadomienie ntfy nie wysłane: {0}', e))


def fmt_duration(seconds):
    minutes = int(seconds // 60)
    return f"{minutes // 60} h {minutes % 60:02} min" if minutes >= 60 else f"{max(1, minutes)} min"


DONE_DIR = "skonczone"   # tu trafiają gotowe filmy z dysków lokalnych


def is_local(path):
    """Czy plik leży na dysku lokalnym (nie w udziale sieciowym ani na dysku sieciowym z literą)."""
    p = str(path)
    if p.startswith(("\\\\", "//")):
        return False
    if os.name != "nt":
        return True
    import ctypes
    return ctypes.windll.kernel32.GetDriveTypeW(os.path.splitdrive(p)[0] + "\\") in (2, 3)   # wymienny, stały


def done_target(path, root):
    """Miejsce gotowego filmu: <folder nad dodanym>/skonczone/<serial>/...

    "H:/ruscy/A/Serial/odc.mkv" (dodano folder A) -> "H:/ruscy/skonczone/Serial/odc.mkv";
    film leżący wprost w dodanym folderze zachowuje nazwę tego folderu.
    """
    root = Path(root)
    try:
        rel = path.relative_to(root)
    except ValueError:
        root, rel = path.parent, Path(path.name)
    if len(rel.parts) == 1 and root.name:
        rel = Path(root.name) / rel
    return (root.parent if root.name else root) / DONE_DIR / rel


def locate_done(path):
    """Gdzie jest teraz plik z raportu: na starym miejscu albo przeniesiony do folderu skonczone
    (sprawdza każdy folder nadrzędny jako ten, który był dodany do kolejki). None, gdy go nie ma."""
    path = Path(path)
    if path.exists():
        return path
    for root in path.parents:
        if not root.name:
            break
        candidate = done_target(path, root)
        if candidate.exists():
            return candidate
    return None


def move_done(path, root):
    """Przenosi gotowy film (z plikami towarzyszącymi) do folderu skonczone; zwraca nowe położenie."""
    dst = done_target(path, root)
    if dst.exists():
        raise FatalError(tr('w {0} jest już {1}', DONE_DIR, dst.name))
    plex_rename(path, dst)
    root, folder = Path(root), path.parent
    while folder != root and root in folder.parents:   # puste foldery po przeniesieniu znikają
        if folder.exists():
            try:
                folder.rmdir()
            except OSError:
                break
        folder = folder.parent
    return dst


def collect(paths, recursive):
    """Pliki do kolejki: filmy (MKV, AVI, MP4, TS...) oraz pliki napisów, które nie mają filmu obok."""
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            found = sorted(p.rglob("*") if recursive else p.glob("*"))
            videos = {}                       # folder -> nazwy filmów w nim
            found = [f for f in found if DONE_DIR not in f.relative_to(p).parts[:-1]]   # gotowe już przeniesione
            for f in found:
                if f.suffix.lower() in VIDEO_EXTS and not f.name.lower().endswith(
                        (".pl.mkv", ".pl.tmp.mkv", ".en.mkv", ".en.tmp.mkv", ".orig.mkv")):
                    videos.setdefault(f.parent, []).append(f.stem.lower())
                    files.append(f)
            files += [f for f in found if f.is_file() and is_standalone_sub(f, videos.get(f.parent, ()))]
        elif p.is_file():
            files.append(p)
        else:
            print(tr('Nie znaleziono: {0}', p))
    return files


def physical_path(f):
    """Na Unraid: /mnt/user/... -> /mnt/diskN/... (albo pula cache), gdzie plik leży naprawdę.

    Podmiana oryginału przez /mnt/user mogłaby zostawić dwie wersje filmu na różnych dyskach; na dysku
    fizycznym plik roboczy powstaje obok oryginału, a zamiana jest zwykłą operacją jednego systemu plików.
    """
    try:
        rel = f.relative_to("/mnt/user")
    except ValueError:
        return f
    skip = {"user", "user0", "disks", "remotes", "addons", "rootshare"}
    for branch in sorted(Path("/mnt").iterdir()):
        if branch.name not in skip and (branch / rel).is_file():
            return branch / rel
    return f


def watch(args):
    """Praca ciągła: co args.watch minut tłumaczy pliki, których jeszcze nie obsłużono.

    Obsłużone pliki (także pominięte i błędne) trafiają do pliku stanu, więc kolejne przebiegi nie czytają ich
    ponownie; zmieniony plik (inny rozmiar lub data) jest sprawdzany od nowa. Gdy Ollama nie odpowiada
    (komputer z kartą wyłączony), plik czeka na następny przebieg.
    """
    state_file = Path(args.state or os.environ.get("MKV_STATE") or APP_DIR / "zrobione.json")
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}

    def key(f):
        st = f.stat()
        return f"{st.st_size}|{int(st.st_mtime)}"

    def save():
        tmp = state_file.with_name(state_file.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=0), encoding="utf-8")
        os.replace(tmp, state_file)

    engine = None
    print(tr('Praca ciągła: {0}; co {1:g} min; Ollama: {2}', ', '.join(args.paths), args.watch, OLLAMA_URL), flush=True)
    while True:
        prune_backups()                       # kopie .orig.mkv, którym minął termin
        todo = []
        for f in collect(args.paths, True):
            try:
                # plik zmieniany w ostatnich 10 minutach może się jeszcze kopiować
                if state.get(str(f), {}).get("key") != key(f) and time.time() - f.stat().st_mtime > 600:
                    todo.append(f)
            except OSError:
                continue
        if todo:
            print(tr('\n[{0}] nowych plików: {1}', time.strftime('%Y-%m-%d %H:%M'), len(todo)), flush=True)
        done = []
        for f in todo:
            try:
                if engine is None:
                    engine = make_engine(args.engine, args.model)
                    engine.parallel = args.parallel
                result = process(physical_path(f), args, engine)
            except ToolDown as e:
                print(tr('    {0} — ponowię za {1:g} min', e, args.watch), flush=True)
                engine = None
                break
            except Exception as e:            # także nieprzewidziane — jeden zły plik nie zatrzymuje serwera
                result = f"Błąd: {e}"
                print(tr('    BŁĄD: {0}', e), flush=True)
            try:
                state[str(f)] = {"key": key(f), "wynik": result, "kiedy": time.strftime("%Y-%m-%d %H:%M")}
                save()
            except OSError:
                pass                          # plik zniknął w trakcie (np. przeniesiony)
            done.append(result)
        if done and args.ntfy:                # jedno powiadomienie na przebieg, nie na plik
            summary = ", ".join(f"{k} {done.count(k)}" for k in sorted(set(r.split(":")[0] for r in done)))
            notify(tr('MKV Translator (serwer)'), tr('Przetworzono {0} plików: {1}', len(done), summary), args.ntfy, toast=False)
        time.sleep(args.watch * 60)


def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8", errors="replace")
    if "--ui-lang" in sys.argv[:-1]:          # przed budową pomocy --help, która też jest tłumaczona
        set_ui_lang(sys.argv[sys.argv.index("--ui-lang") + 1])
    ap = argparse.ArgumentParser(description=tr('Tłumaczy napisy (rosyjskie, angielskie, ukraińskie) na polski.'))
    ap.add_argument("--version", action="version", version=f"MKV Translator {VERSION}")
    ap.add_argument("paths", nargs="*", help=tr('pliki MKV lub foldery z plikami MKV'))
    ap.add_argument("--engine", choices=["ollama", "google", "claude"], default="ollama",
                    help=tr('silnik tłumaczenia (domyślnie ollama — lokalnie)'))
    ap.add_argument("--parallel", type=int, default=2,
                    help=tr('ile partii tłumaczyć naraz w Ollamie (domyślnie 2; wymaga OLLAMA_NUM_PARALLEL >= tej liczby)'))
    ap.add_argument("--model", help=tr('model Ollamy, np. gemma4:31b (domyślnie {0}; dla --engine claude: {1})', OLLAMA_MODEL, CLAUDE_MODEL))
    ap.add_argument("--mux", action="store_true", help=tr('dodaj polskie napisy wprost do pliku MKV'))
    ap.add_argument("--backup-days", type=int, default=0, metavar="DNI",
                    help=tr('przy podmianie oryginału zostaw go jako <nazwa>.orig.mkv przez DNI dni (0 = bez kopii)'))
    ap.add_argument("--keep-original", action="store_true",
                    help=tr('z --mux/--lektor: nie zmieniaj oryginału, zapisz kopię <nazwa>.pl.mkv'))
    ap.add_argument("--whisper", action="store_true",
                    help=tr('gdy plik nie ma rosyjskich napisów, rozpoznaj mowę z dźwięku'))
    ap.add_argument("--asr", choices=["gigaam", "gigaam-rnnt", "whisper", "gigaam+whisper"], default="gigaam+whisper",
                    help=tr('silnik rozpoznawania mowy dla --whisper (domyślnie gigaam — model tylko dla rosyjskiego)'))
    ap.add_argument("--diarize", action="store_true",
                    help=tr('z --whisper: rozpoznawanie mówców (pyannote) — myślniki dialogowe i płeć mówiącego; wymaga HF_TOKEN'))
    ap.add_argument("--separate", action="store_true",
                    help=tr('z --whisper: przed rozpoznawaniem oddziel głos od muzyki i efektów (Demucs)'))
    ap.add_argument("--lektor", choices=["piper", "xtts"],
                    help=tr('dodaj ścieżkę polskiego lektora (wynik: <nazwa>.pl.mkv)'))
    ap.add_argument("--revoice", action="store_true",
                    help=tr('z --lektor: nagraj lektora od nowa (np. inny silnik) z polskich napisów zapisanych w pliku, bez ponownego tłumaczenia; poprzednia ścieżka "Polski lektor" zostaje zastąpiona'))
    ap.add_argument("--voice", help=tr('głos lektora: nazwa głosu Piper/XTTS albo plik WAV z próbką (XTTS)'))
    ap.add_argument("--replace-audio", action="store_true",
                    help=tr('z --lektor: usuń oryginalne ścieżki dźwiękowe zamiast dodawać nową'))
    ap.add_argument("--plex-names", choices=["preview", "apply", "undo"],
                    help=tr('tylko zmiana nazw na format Plex ("Serial - S02E01.mkv"): preview pokazuje plan, apply zmienia nazwy, undo przywraca poprzednie; nic nie tłumaczy'))
    ap.add_argument("--to-mkv", action="store_true",
                    help=tr('tylko zamień pliki TS na MKV (bez przekodowania, z wbudowaniem napisów .pl.srt)'))
    ap.add_argument("--no-teletext", action="store_true",
                    help=tr('z --to-mkv: nie przenoś napisów telegazety do MKV'))
    ap.add_argument("--check", action="store_true",
                    help=tr('sprawdź instalację (ffmpeg, Ollama, modele audio) i pobierz brakujące pliki modeli'))
    ap.add_argument("--plex-folders", action="store_true",
                    help=tr('z --plex-names: przenieś też pliki do folderów "Serial/Season 02/"'))
    ap.add_argument("--target", choices=list(TARGETS), default="pl",
                    help=tr('język napisów wynikowych (domyślnie pl; en: angielski, bez lektora)'))
    ap.add_argument("--repair-model", help=tr('model Ollamy do poprawiania podejrzanych kwestii (domyślnie automatycznie)'))
    ap.add_argument("--whisper-model", choices=WHISPER_MODELS,
                    help=tr('model Whisper do rozpoznawania mowy innej niż rosyjska (domyślnie large-v3)'))
    ap.add_argument("--ui-lang", choices=list(UI_LANGS), help=tr('język komunikatów programu (pl albo en)'))
    ap.add_argument("--source", choices=["auto", *LANGS], default="auto",
                    help=tr('język źródłowy napisów/mowy (domyślnie auto: rosyjski, potem angielski i ukraiński)'))
    ap.add_argument("--ntfy", metavar="TEMAT",
                    help=tr('po zakończeniu wyślij powiadomienie na telefon przez ntfy.sh (aplikacja ntfy, ten temat)'))
    ap.add_argument("--move-done", action="store_true",
                    help=tr('gotowe filmy z dysków lokalnych przenieś do folderu "{0}" obok dodanego folderu', DONE_DIR))
    ap.add_argument("--watch", type=float, metavar="MINUTY",
                    help=tr('praca ciągła (np. na serwerze): co MINUTY minut sprawdzaj foldery i tłumacz nowe pliki'))
    ap.add_argument("--state", help=tr('z --watch: plik z listą obsłużonych plików (domyślnie zrobione.json obok programu)'))
    ap.add_argument("--track", type=int, help=tr('numer ścieżki napisów (od 1), gdy automatyczny wybór zawodzi'))
    ap.add_argument("-r", "--recursive", action="store_true", help=tr('przeszukuj podfoldery'))
    ap.add_argument("--force", action="store_true", help=tr('tłumacz ponownie, nawet jeśli wynik już istnieje'))
    args = ap.parse_args()
    args.replace = not args.keep_original
    set_repair_model(args.repair_model)
    prune_backups()
    args.resume = args.restart = False        # przerwane tłumaczenie wznawia się samo, chyba że --force
    if args.watch:
        return watch(args)

    files = collect(args.paths, args.recursive)
    if not files and args.plex_names != "undo" and not args.check:
        print(tr('Brak plików MKV do przetworzenia.'))
        return 1
    if args.check:
        return 1 if check_install(args.model) else 0
    if args.to_mkv:
        targets = [f for f in files if f.suffix.lower() in (".ts", ".m2ts")]
        failed = 0
        for f in targets:
            try:
                print(f"{f.name} -> {remux_to_mkv(f, not args.no_teletext).name}", flush=True)
            except FatalError as e:
                failed += 1
                print(tr('{0}: BŁĄD: {1}', f.name, e), flush=True)
        print(tr('\nZamieniono na MKV: {0}, błędów: {1}', len(targets) - failed, failed))
        return 1 if failed else 0
    if args.plex_names == "undo":
        restored, failed = plex_undo()
        print(tr('Przywrócono stare nazwy: {0} plików, nie udało się: {1}', len(restored), failed))
        return 0
    if args.plex_names:
        plan = plex_renames(files, args.plex_folders)
        done, journal = 0, plex_log_read()
        for old, new, note in plan:
            shown = new if args.plex_folders else new.name
            print(f"{old.parent}\n    {old.name}\n -> {shown}" + (f"   [{note}]" if note else ""))
            if args.plex_names == "apply" and not note:
                journal += plex_rename(old, new)
                plex_log_write(journal)
                done += 1
        print(tr('\nDo zmiany: {0}, pominięte: {1}, bez zmian: {2}', sum(not n for *_, n in plan), sum(bool(n) for *_, n in plan), len(files) - len(plan)) + (tr(', zmieniono: {0}', done) if args.plex_names == "apply" else ""))
        return 0
    try:
        engine = make_engine(args.engine, args.model)
        if args.engine == "ollama":
            engine.parallel = args.parallel
    except FatalError as e:
        print(tr('BŁĄD: {0}', e))
        return 1
    print(f"MKV Translator {VERSION}")
    print(tr('Silnik tłumaczenia: {0}; plików: {1}', engine.name, len(files)))

    failed = 0
    dirs = [Path(p).resolve() for p in args.paths if Path(p).is_dir()]
    for mkv in files:
        try:
            result = process(mkv, args, engine)
            full = mkv.resolve()
            if args.move_done and result in FINISHED_STATES and mkv.suffix.lower() in VIDEO_EXTS \
                    and is_local(full):
                root = next((d for d in dirs if d in full.parents), full.parent)
                print(tr('    przeniesiono do {0}', move_done(full, root).parent))
        except (FatalError, OSError) as e:
            failed += 1
            print(tr('    BŁĄD: {0}', e))
    stop_helper()
    print(tr('\nGotowe. Błędów: {0}/{1}', failed, len(files)))
    if args.ntfy:
        notify("MKV Translator", tr('Zakończono {0} plików, błędów: {1}', len(files), failed), args.ntfy, toast=False)
    return 1 if failed else 0


# ---------------------------------------------------------------- okno kolejki (gui.py)

FINISHED_STATES = ("Gotowe", "Do sprawdzenia", "Ma polskie")   # z tymi stanami film trafia do folderu skonczone


def run_gui(paths):
    """Okno kolejki jest w osobnym module gui.py (wczytywanym tylko wtedy — serwer i wiersz poleceń go nie potrzebują)."""
    import gui
    return gui.run_gui(paths)


def entry():
    # plik .exe to zawsze okno kolejki (przeciągnięte pliki trafiają do kolejki);
    # skrypt otwiera okno bez argumentów albo z --gui, w pozostałych przypadkach działa w konsoli
    if getattr(sys, "frozen", False) or len(sys.argv) == 1 or sys.argv[1] == "--gui":
        return run_gui([a for a in sys.argv[1:] if a != "--gui"])
    return main()


if __name__ == "__main__":
    # moduł wczytany jeszcze raz pod nazwą translate_mkv: gui.py korzysta z tego samego stanu (Stop, język okna...)
    import translate_mkv
    sys.exit(translate_mkv.entry())
