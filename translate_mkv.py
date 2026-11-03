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
- zachowaj myślnik na początku kwestii dialogowej, jeśli jest w oryginale,
- każda kwestia to osobna wartość: nie łącz, nie dziel i nie pomijaj kwestii,
- pole "kontekst" (jeśli występuje) to wcześniejsze kwestie tylko do wglądu — nie tłumacz go i nie zwracaj.
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
- keep a leading dash of a dialogue line if the original has one,
- every cue is a separate value: do not merge, split or skip cues,
- the "kontekst" field (if present) holds earlier cues for reference only — do not translate or return it.
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


def system_prompt(lang):
    if OUT["code"] == "pl":
        return SYSTEM_PROMPT.format(source=LANGS[lang][1])
    return SYSTEM_PROMPT_EN.format(source=LANGS[lang][0])


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

def run(cmd):
    # CREATE_NO_WINDOW: w wersji okienkowej ffmpeg nie może otwierać własnych konsol
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


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


def run_helper(args):
    """Uruchamia polecenie audio_tools.py i przekazuje jego komunikaty na bieżąco.

    Rozpoznawanie mowy i lektor idą przez stały proces (tryb "serve"): Demucs, GigaAM i głosy wczytują się
    raz na całą kolejkę zamiast przy każdym pliku. MKV_AUDIO_PERSIST=0 wyłącza ten tryb.
    """
    if args[0] in ("transcribe", "tts", "detect", "ocr") and os.environ.get("MKV_AUDIO_PERSIST", "1") != "0":
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
        _helper.update(proc=proc, errors=errors)
    _helper["errors"].clear()                 # opis błędu ma dotyczyć tego polecenia, nie poprzednich
    try:
        proc.stdin.write((json.dumps([str(a) for a in args]) + "\n").encode("utf-8"))
        proc.stdin.flush()
    except OSError:
        stop_helper()
        raise ToolDown(tr('proces narzędzi audio przestał działać — spróbuj ponownie'))
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    while True:
        chunk = proc.stdout.read1(4096)
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
        drop_subs=(), drop_audio=()):
    """Tworzy kopię MKV z dodanymi polskimi napisami (srt) i/lub ścieżką lektora (voice).

    drop_subs / drop_audio: numery ścieżek z poprzedniego przebiegu, które nowe mają zastąpić.
    """
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
        opts += ["-disposition:s", "0", f"-disposition:s:{n}", "0" if voice else "default",
                 f"-metadata:s:s:{n}", f"language={TARGETS[OUT['code']][4]}",
                 f"-metadata:s:s:{n}", f"title={TARGETS[OUT['code']][3]}"]
        inputs += 1
    if voice:
        cmd += ["-i", str(voice)]
        # lektor na przyciszonym oryginale: oryginał cichnie, gdy lektor mówi (sidechain)
        cmd += ["-filter_complex",
                f"[{inputs}:a]aresample=48000,volume=1.5,asplit=2[v1][v2];"
                f"[0:a:{main_audio(audio, 'auto')[0]}]aresample=48000,aformat=channel_layouts=stereo[orig];"
                "[orig][v1]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=500[duck];"
                "[duck][v2]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[lektor]"]
        if replace_audio:
            maps += ["-map", "-0:a"]
        n = "0" if replace_audio else str(len(audio) - len(drop_audio))
        maps += ["-map", "[lektor]"]
        opts += [f"-c:a:{n}", "aac", f"-b:a:{n}", "192k", "-disposition:a", "0", f"-disposition:a:{n}", "default",
                 f"-metadata:s:a:{n}", "language=pol", f"-metadata:s:a:{n}", "title=Polski lektor"]
    res = run(cmd + maps + opts + [str(out_path)])
    if res.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise FatalError(tr('ffmpeg nie złożył pliku MKV: {0}', res.stderr.strip()))


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
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as f:
        for i, cue in enumerate(cues, 1):
            f.write(f"{i}\n{cue.start} --> {cue.end}{cue.extra}\n{cue.text}\n\n")


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
    payload = {"kwestie": {str(i): t for i, t in enumerate(texts, 1)}}
    if context:
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

    def __init__(self, model):
        self.model = model
        self.name = f"Ollama ({model})"
        self.line_mode = "translategemma" in model.lower()
        self.think_off = not self.line_mode
        names = ensure_ollama()
        if model not in names and f"{model}:latest" not in names:
            raise OllamaDown(tr("Ollama nie ma modelu '{0}'. Dostępne: {1}", model, ', '.join(names)))

    def _chat(self, messages, attempt=1):
        body = {"model": self.model, "messages": messages, "stream": False,
                "options": {"num_ctx": 8192}}  # temperatura domyślna: obniżona zapętla gemma4
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

    def translate(self, texts, context=()):
        if self.line_mode:
            return self._marked(texts) if len(texts) > 1 else [" ".join(self._lines(texts[0]))]
        if not self.line_mode:
            messages = [{"role": "system", "content": system_prompt(self.lang)},
                        {"role": "user", "content": json_payload(texts, context)}]
            for _ in range(2):
                try:
                    return parse_json_reply(self._chat(messages), len(texts))
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
            system=system_prompt(self.lang),
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
                    "własny, wpisz go w \"pisownia\" i ustaw \"zablokuj\": true.",
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


def quality_check(sources, outputs, cues, lang="ru"):
    """Liczy podejrzane miejsca tłumaczenia; zwraca (ocena, opis, przykłady)."""
    issues = {"w języku oryginału": [], "nieprzetłumaczone": [], "zapętlenia": [], "długość": []}
    for src, out in zip(sources, outputs):
        for kind in segment_issues(src, out, lang):
            issues[kind].append(f"{src}  →  {out}" if kind == "długość" else out or src)
    fast = sum(cue_cps(c) > 20 for c in cues)
    total = max(1, len(sources))
    bad = (issues["w języku oryginału"] or issues["zapętlenia"] or len(issues["nieprzetłumaczone"]) > 0.02 * total
           or len(issues["długość"]) > 0.05 * total or fast > 0.25 * max(1, len(cues)))
    parts = [f"{len(v)} {tr(k)}" for k, v in issues.items() if v] + ([tr('{0} za szybkich (>20 zn/s)', fast)] if fast else [])
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


def repair_segments(flat, done, engine, lang):
    """Ponownie tłumaczy podejrzane kwestie: najpierw pojedynczo tym samym modelem, potem modelem ogólnym
    z kontekstem poprzednich kwestii. Zwraca (tłumaczenia, poprawione, podejrzane)."""
    done = list(done)
    bad = [i for i in range(len(flat)) if segment_issues(flat[i], done[i], lang)]
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


def report_quality(video, verdict, summary, count):
    """Dopisuje wynik do zbiorczego raportu jakości (CSV otwierany w Excelu)."""
    import csv
    try:
        new = not QUALITY_REPORT.exists()
        with open(QUALITY_REPORT, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow([tr('data'), tr('ocena'), tr('kwestii'), tr('uwagi'), tr('plik')])
            w.writerow([time.strftime("%Y-%m-%d %H:%M"), verdict, count, summary, str(video)])
    except OSError:
        pass


def translate_cues(cues, engine, checkpoint=None, resume=True, glossary=None, quality=None):
    """Tłumaczy kwestie; postęp zapisuje po każdej partii w pliku checkpoint, żeby dało się wznowić.

    glossary: plik słowniczka imion serialu; quality: słownik, do którego trafia ocena jakości tłumaczenia.
    """
    parts = [split_cue(c) for c in cues]
    flat = [seg for _, _, segs in parts for seg in segs]
    # kwestie bez liter (np. "...", "♪") zostają bez zmian
    todo = [i for i, t in enumerate(flat) if any(c.isalpha() for c in t)]
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

    def translate_batch(pos):
        idx = todo[pos:pos + step]
        context = [flat[i] for i in todo[max(0, pos - 8):pos]]
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
    if quality is not None:                   # automatyczna naprawa podejrzanych kwestii
        done, repaired, suspicious = repair_segments(flat, done, engine, lang)
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
    result, longer = retime(result)
    if longer:
        print(tr('    czas wyświetlania: wydłużono {0} kwestii za krótkich do przeczytania', longer))
    if quality is not None and sum(cue_cps(c) > 20 for c in result) > 0.25 * max(1, len(result)):
        result, shortened = shorten_fast(result, engine)   # tylko gdy za szybkich jest wyraźnie za dużo
    if quality is not None:
        verdict, summary, examples = quality_check(flat, done, result, lang)
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
    report_quality(sub, quality["verdict"], quality["summary"], len(cues))
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


def detect_speech_language(mkv, audio_index, tmp):
    """Język mowy nieoznaczonej ścieżki (Whisper na 15 minutach od 5. minuty filmu).

    Bez tego angielski film bez napisów trafiał do GigaAM jako "rosyjski" i dawał bezsensowny tekst.
    Film niemy albo sama muzyka (niska pewność) i języki nieobsługiwane kończą się błędem zamiast tłumaczenia.
    """
    sample, result = Path(tmp) / "jezyk.wav", Path(tmp) / "jezyk.txt"
    for start in ("300", "0"):                # krótki film: od początku
        res = run(["ffmpeg", "-v", "error", "-y", "-ss", start, "-t", "900", "-i", str(mkv),
                   "-map", f"0:a:{audio_index}", "-ar", "16000", "-ac", "1", str(sample)])
        if res.returncode == 0 and sample.exists() and sample.stat().st_size > 16000 * 2 * 20:
            break
    else:
        raise FatalError(tr('nie udało się wyodrębnić dźwięku do rozpoznania języka'))
    run_helper(["detect", sample, result])
    code, prob = result.read_text(encoding="utf-8").split()
    if float(prob) < 0.5:
        raise FatalError(tr('nie da się pewnie rozpoznać języka mowy ({0}, {1:.0%}) — film niemy, sama muzyka albo kilka języków; wybierz język ręcznie w ustawieniach', code, float(prob)))
    if code not in LANGS:
        raise FatalError(tr('mowa w języku „{0}” — obsługiwane: {1}', code, ', '.join(LANGS)))
    return code


def transcribe(mkv, audio, args, tmp):
    """Rozpoznaje mowę ze ścieżki dźwiękowej; zwraca (kwestie, język). Wynik zostaje w <nazwa>.rozpoznane.ru.srt
    (nazwa historyczna — plik może zawierać też inny język).

    GigaAM zna tylko rosyjski: dla innych języków mowę rozpoznaje Whisper. Nieoznaczona ścieżka przy "auto"
    jest traktowana jak rosyjska (tak jest w większości biblioteki); dla angielskiej wybierz język w ustawieniach.
    """
    ru_srt = mkv.with_suffix(".rozpoznane.ru.srt")
    source = getattr(args, "source", "ru")
    if ru_srt.exists() and (args.resume or not args.force):
        print(tr('    używam istniejącego {0}', ru_srt.name))
    else:
        if not audio:
            raise FatalError(tr('plik nie ma ścieżki dźwiękowej'))
        wav = Path(tmp) / "audio.wav"
        src, lang = main_audio(audio, source)
        if lang_of(audio[src]) in target_tags():
            raise FatalError(tr('plik ma dźwięk tylko w języku docelowym — nie ma obcej mowy do rozpoznania'))
        if lang is None and source != "auto":
            lang = source
        if lang is None:
            lang = detect_speech_language(mkv, src, tmp)
        if lang == OUT["code"]:
            raise FatalError(tr('mowa jest już w języku docelowym'))
        asr = args.asr if lang == "ru" else "whisper"
        print(tr('    mowa: {0}, rozpoznawanie: {1}', LANGS[lang][0], 'GigaAM' if asr == 'gigaam' else 'Whisper'))
        cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(mkv), "-map", f"0:a:{src}"]
        res, helper = None, ["transcribe", wav, ru_srt, "--engine", asr, "--language", lang]
        if asr == "whisper" and getattr(args, "whisper_model", None):
            helper += ["--model", args.whisper_model]
        if audio[src].get("channels", 2) >= 6:
            # w dźwięku 5.1/7.1 dialogi są w kanale centralnym — bez muzyki i efektów z pozostałych
            res = run(cmd + ["-ar", "16000", "-af", "pan=mono|c0=FC", str(wav)])
        if res is None or res.returncode != 0:
            if args.separate:                 # Demucs sam oddzieli głos — potrzebuje stereo 44,1 kHz
                res = run(cmd + ["-ar", "44100", "-ac", "2", str(wav)])
                helper.append("--separate")
            else:
                res = run(cmd + ["-ar", "16000", "-ac", "1", str(wav)])
        if res.returncode != 0:
            raise FatalError(tr('ffmpeg nie wyodrębnił dźwięku: {0}', res.stderr.strip()))
        run_helper(helper)
    cues = parse_srt(ru_srt.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        ru_srt.unlink(missing_ok=True)        # pusty wynik nie może być użyty przy następnej próbie
        raise FatalError(tr('w dźwięku nie rozpoznano mowy'))
    lang = detect_lang([c.text for c in cues]) or ("ru" if source == "auto" else source)
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
            try:
                cues, lang = pick_source_cues(mkv, streams, args.track, tmp, source)
            except FatalError as e:
                # brak napisów w pliku: najpierw napisy z osobnego pliku obok filmu, potem mowa z dźwięku
                found = None if args.track is not None else external_cues(mkv, tmp, source)
                if found is None:
                    if not args.whisper or args.track is not None:
                        raise
                    print(tr('    {0} — rozpoznaję mowę z dźwięku', e))
                    found = transcribe(mkv, audio, args, tmp)
                cues, lang = found
            else:
                # napisy szczątkowe (np. sama ścieżka "Forced" albo urwane): poniżej 1,5 kwestii na minutę
                # filmu to za mało na pełne dialogi — wtedy lepsze jest rozpoznanie mowy z dźwięku
                minutes = (duration(mkv) or 0) / 60
                if args.whisper and args.track is None and audio and len(cues) < 1.5 * minutes:
                    print(tr('    napisy mają tylko {0} kwestii na {1:.0f} min filmu — rozpoznaję mowę z dźwięku', len(cues), minutes))
                    try:
                        spoken, spoken_lang = transcribe(mkv, audio, args, tmp)
                        if len(spoken) > len(cues):
                            cues, lang = spoken, spoken_lang
                    except FatalError as e:
                        print(tr('    rozpoznawanie mowy nie powiodło się ({0}) — zostają napisy z pliku', e))
            engine.lang = lang
            write_srt(translate_cues(cues, engine, part, args.resume or not args.force,
                                     glossary_file(mkv), quality), srt_out)
            report_quality(mkv, quality["verdict"], quality["summary"], len(cues))
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
            mux(mkv, mkv_out, new_srt, len(streams), voice, audio, args.replace_audio, old_subs, old_audio)
            if args.replace:
                # oczekiwane ścieżki: oryginał minus zastępowane ścieżki programu plus nowe
                want_audio = 1 if voice and args.replace_audio else len(audio) - len(old_audio) + bool(voice)
                want_subs = len(streams) - len(old_subs) + bool(new_srt)
                check_remux(mkv, mkv_out, want_audio, want_subs)
                try:
                    os.replace(mkv_out, mkv)
                except OSError as e:              # np. film otwarty w odtwarzaczu
                    mkv_out.unlink(missing_ok=True)
                    raise FatalError(tr('nie da się podmienić oryginału ({0}) — oryginał pozostał bez zmian', e))
                shutil.rmtree(voice_part, ignore_errors=True)
                # napisy są już w pliku — usuwamy tylko pliki pośrednie utworzone przez program
                for leftover in ([srt_out] if own_srt else []) + [mkv.with_suffix(".rozpoznane.ru.srt")]:
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
                        (".pl.mkv", ".pl.tmp.mkv", ".en.mkv", ".en.tmp.mkv")):
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
    ap.add_argument("paths", nargs="*", help=tr('pliki MKV lub foldery z plikami MKV'))
    ap.add_argument("--engine", choices=["ollama", "google", "claude"], default="ollama",
                    help=tr('silnik tłumaczenia (domyślnie ollama — lokalnie)'))
    ap.add_argument("--parallel", type=int, default=2,
                    help=tr('ile partii tłumaczyć naraz w Ollamie (domyślnie 2; wymaga OLLAMA_NUM_PARALLEL >= tej liczby)'))
    ap.add_argument("--model", help=tr('model Ollamy, np. gemma4:31b (domyślnie {0}; dla --engine claude: {1})', OLLAMA_MODEL, CLAUDE_MODEL))
    ap.add_argument("--mux", action="store_true", help=tr('dodaj polskie napisy wprost do pliku MKV'))
    ap.add_argument("--keep-original", action="store_true",
                    help=tr('z --mux/--lektor: nie zmieniaj oryginału, zapisz kopię <nazwa>.pl.mkv'))
    ap.add_argument("--whisper", action="store_true",
                    help=tr('gdy plik nie ma rosyjskich napisów, rozpoznaj mowę z dźwięku'))
    ap.add_argument("--asr", choices=["gigaam", "whisper"], default="gigaam",
                    help=tr('silnik rozpoznawania mowy dla --whisper (domyślnie gigaam — model tylko dla rosyjskiego)'))
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
    print(tr('Silnik tłumaczenia: {0}; plików: {1}', engine.name, len(files)))

    failed = 0
    dirs = [Path(p).resolve() for p in args.paths if Path(p).is_dir()]
    for mkv in files:
        try:
            result = process(mkv, args, engine)
            full = mkv.resolve()
            if args.move_done and result in QueueApp.FINISHED and mkv.suffix.lower() in VIDEO_EXTS \
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


# ---------------------------------------------------------------- okno kolejki

class LogWriter:
    """Zastępuje stdout: przekazuje wypisywany tekst do okna i dopisuje go do pliku dziennika."""

    def __init__(self, events, log_file=None):
        self.events = events
        self.log_file = log_file

    def write(self, text):
        if not text:
            return
        self.events.put(("log", text))
        if self.log_file:
            kept = re.sub(r"\r[^\n]*", "", text)    # bez liczników postępu nadpisywanych w miejscu
            if kept:
                try:
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        f.write(kept)
                except OSError:
                    pass

    def flush(self):
        pass


def ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return sorted(m["name"] for m in json.load(r).get("models", []))
    except (urllib.error.URLError, OSError, ValueError):
        return []


def tk_error():
    import tkinter
    return tkinter.TclError


class QueueApp:
    WAITING, RUNNING = "Oczekuje", "W toku..."

    # opcje okna zapisywane razem z kolejką
    SAVED_OPTIONS = ("recursive", "model", "mux", "keep", "force", "whisper", "asr", "separate",
                     "lektor", "voice", "replace_audio", "parallel", "move_done", "notify_toast", "ntfy_topic",
                     "source", "target", "repair_model", "whisper_model")
    # listy wyboru, w których okno pokazuje tekst w swoim języku, a kolejka zapisuje klucz (tekst polski)
    AUTO = "automatycznie"
    CHOICE_OPTIONS = {"source": SOURCE_CHOICES, "target": TARGET_CHOICES, "lektor": ("brak",), "repair_model": (AUTO,)}
    FINISHED = ("Gotowe", "Do sprawdzenia", "Ma polskie")   # z tymi stanami film trafia do folderu skonczone
    # stan "Ma polskie" oznacza dziś napisy w wybranym języku docelowym (klucz zostaje dla zapisanych kolejek)
    STATE_LABELS = {"Ma polskie": "Ma już napisy"}
    STATE_COLORS = {"Gotowe": {"foreground": "#1a7f37"}, "Ma polskie": {"foreground": "#2f6f4f"},
                    "Do sprawdzenia": {"foreground": "#8250df"},
                    "Błąd": {"foreground": "#cf222e"}, "Przerwano": {"foreground": "#9a6700"},
                    "Pominięto": {"foreground": "#6e7781"},
                    "W toku...": {"foreground": "#0b5cad", "background": "#e3f0ff"}}
    PROGRESS_RE = re.compile(r"(przetłumaczono|translated|lektor:?|voice-over:?|kwestia|cue|rozpozna\w*|recogni\w*)"
                             r"\D*(\d+)\s*/\s*(\d+)")

    def __init__(self, root, paths, queue_file=None):
        import queue
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.events = queue.Queue()
        self.engine = None
        self.running = False
        self.current = None
        self.modes = {}                       # przerwane pliki: "resume" albo "restart"
        self.states = {}                      # stan pliku (klucz po polsku); w tabeli widać tr(stan)
        self.tracks = {}                      # ścieżka napisów wybrana ręcznie dla pliku (numer od 1)
        self.alive = True                     # False po przebudowie okna w innym języku
        self.roots = {}                       # folder dodany do kolejki, z którego pochodzi plik
        self.move_flag = False                # stan pola "gotowe → skonczone" odczytany w wątku okna
        self.durations = []                   # czasy plików przetworzonych w tej sesji (do szacowania końca)
        self.avg_seconds = None               # średni czas pliku z poprzednich sesji (zapisany z kolejką)
        self.file_started = self.run_started = None
        self.stop_reason = None               # awaria narzędzia, która zatrzymała kolejkę (do powiadomienia)
        self.queue_file = queue_file          # tu kolejka i jej stan czekają na następne uruchomienie
        self.plex_log = queue_file.with_name("plex_nazwy.json") if queue_file else None
        # dziennik obok programu: zostaje po zamknięciu okna, więc da się sprawdzić przyczynę błędów
        sys.stdout = sys.stderr = LogWriter(self.events, queue_file.with_name("dziennik.txt") if queue_file else None)

        root.title(tr('MKV Translator — tłumaczenie napisów'))
        root.geometry("1320x800")
        root.minsize(1040, 600)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24)
        style.configure("Start.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 4))
        style.configure("Head.TLabelframe.Label", font=("Segoe UI", 9, "bold"))

        # ---- pasek narzędzi: dodawanie i porządkowanie kolejki | narzędzia | start/stop
        bar = ttk.Frame(root, padding=(10, 10, 10, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text=tr('＋ Dodaj pliki...'), command=self.add_files).pack(side="left")
        ttk.Button(bar, text=tr('＋ Dodaj folder...'), command=self.add_folder).pack(side="left", padx=(4, 0))
        self.recursive = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text=tr('z podfolderami'), variable=self.recursive).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        for label, cmd in (("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1)),
                           (tr("↻ Ponów"), self.requeue), (tr("✕ Usuń"), self.remove)):
            ttk.Button(bar, text=label, command=cmd, width=len(label) + 2).pack(side="left", padx=(0, 4))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        tools = ttk.Menubutton(bar, text=tr('Narzędzia'))
        menu = tk.Menu(tools, tearoff=False)
        self.fill_menu(menu, ((tr('Ustaw równoległość w Ollamie teraz (restart Ollamy)'),
                               lambda: self.apply_parallel(then_start=False)),
                              (tr('Popraw oznaczone „Do sprawdzenia”'), self.repair_flagged),
                              (tr('Sprawdź, które pliki mają już napisy'), self.check_polish),
                              (tr('Nazwy Plex...'), self.plex_dialog), (tr('Zamień TS → MKV'), self.ts_to_mkv), None,
                              (tr('Usuń zakończone z kolejki'), self.clear_done), (tr('Wyczyść całą kolejkę'), self.clear),
                              None, (tr('Raport jakości tłumaczeń'), self.open_report), (tr('Otwórz dziennik'), self.open_log),
                              (tr('Sprawdź instalację'), self.check_install), None,
                              (tr('Pobierz model Ollamy...'), self.pull_dialog)))
        tools["menu"] = menu
        tools.pack(side="left")
        self.stop_btn = ttk.Button(bar, text=tr('■ Stop'), command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(bar, text=tr('▶ Start'), command=self.start, style="Start.TButton")
        self.start_btn.pack(side="right", padx=(0, 6))
        # język okna: zmiana przebudowuje okno (kolejka i ustawienia zostają)
        self.ui_lang = tk.StringVar(value=UI_LANGS[ui["lang"]])
        lang_box = ttk.Combobox(bar, textvariable=self.ui_lang, values=list(UI_LANGS.values()), width=8,
                                state="readonly")
        lang_box.pack(side="right", padx=(0, 16))
        lang_box.bind("<<ComboboxSelected>>", lambda e: self.switch_ui_lang())
        ttk.Label(bar, text=tr('Język okna:')).pack(side="right", padx=(0, 4))

        # ---- środek: kolejka nad dziennikiem (lewa strona) i ustawienia (prawa strona)
        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)
        left = ttk.Panedwindow(body, orient="vertical")
        body.add(left, weight=1)

        qframe = ttk.Labelframe(left, text=tr('Kolejka'), padding=4, style="Head.TLabelframe")
        left.add(qframe, weight=3)
        self.tree = ttk.Treeview(qframe, columns=("file", "dir", "track", "state"), show="headings",
                                 selectmode="extended")
        for col, text, width, stretch in (("file", tr("Plik"), 380, True), ("dir", tr("Folder"), 300, True),
                                          ("track", tr("Napisy"), 70, False), ("state", tr("Stan"), 120, False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch)
        for state, colors in self.STATE_COLORS.items():
            self.tree.tag_configure(state, **colors)
        scroll = ttk.Scrollbar(qframe, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.bind("<Button-3>", self.context_menu)
        self.row_menu = tk.Menu(root, tearoff=False)
        self.fill_menu(self.row_menu, ((tr('Otwórz folder'), self.open_folder),
                                       (tr('Ponów (ustaw jako oczekujące)'), self.requeue),
                                       (tr('Wybierz ścieżkę napisów...'), self.track_dialog),
                                       (tr('W górę'), lambda: self.move(-1)), (tr("W dół"), lambda: self.move(1)), None,
                                       (tr('Usuń z kolejki'), self.remove)))

        lframe = ttk.Labelframe(left, text=tr('Dziennik'), padding=4, style="Head.TLabelframe")
        left.add(lframe, weight=1)
        self.log = tk.Text(lframe, height=10, state="disabled", wrap="none", font=("Consolas", 9),
                           relief="flat", background="#fbfbfb")
        log_scroll = ttk.Scrollbar(lframe, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        body.add(side, weight=0)

        def group(title):
            frame = ttk.Labelframe(side, text=title, padding=(10, 6), style="Head.TLabelframe")
            frame.pack(fill="x", pady=(0, 8))
            frame.columnconfigure(1, weight=1)
            return frame

        def check(frame, row, text, var):
            ttk.Checkbutton(frame, text=text, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", pady=1)

        g = group(tr('Tłumaczenie'))
        models = ollama_models()
        self.model = tk.StringVar(value=OLLAMA_MODEL if OLLAMA_MODEL in models or not models else models[0])
        ttk.Label(g, text=tr('Model:')).grid(row=0, column=0, sticky="w")
        row = ttk.Frame(g)
        row.grid(row=0, column=1, sticky="ew", pady=2)
        self.model_box = ttk.Combobox(row, textvariable=self.model, values=models, width=22)
        self.model_box.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="⇩", width=3, command=self.pull_dialog).pack(side="left", padx=(4, 0))
        ttk.Label(g, text=tr('Do poprawek:')).grid(row=1, column=0, sticky="w")
        self.repair_model = tk.StringVar(value=tr(self.AUTO))
        self.repair_box = ttk.Combobox(g, textvariable=self.repair_model, values=[tr(self.AUTO), *models],
                                       width=26, state="readonly")
        self.repair_box.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(g, text=tr('Równolegle:')).grid(row=2, column=0, sticky="w")
        self.parallel = tk.IntVar(value=2)
        par = ttk.Frame(g)
        par.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Spinbox(par, from_=1, to=8, width=4, textvariable=self.parallel).pack(side="left")
        # ile zapytań naraz przyjmuje sama Ollama (OLLAMA_NUM_PARALLEL); przy Starcie wyrównywane do pola obok
        self.server_par = ttk.Label(par, text="", foreground="#57606a")
        self.server_par.pack(side="left", padx=(8, 0))
        self.show_server_parallel()
        ttk.Label(g, text=tr('Z języka:')).grid(row=3, column=0, sticky="w")
        self.source = tk.StringVar(value=tr(self.AUTO))
        ttk.Combobox(g, textvariable=self.source, values=[tr(k) for k in SOURCE_CHOICES], width=14,
                     state="readonly").grid(row=3, column=1, sticky="w", pady=2)
        ttk.Label(g, text=tr('Na język:')).grid(row=4, column=0, sticky="w")
        self.target = tk.StringVar(value=tr("polski"))
        ttk.Combobox(g, textvariable=self.target, values=[tr(k) for k in TARGET_CHOICES], width=14,
                     state="readonly").grid(row=4, column=1, sticky="w", pady=2)
        self.force = tk.BooleanVar(value=False)
        check(g, 5, tr('tłumacz ponownie (nawet gdy napisy już są)'), self.force)

        g = group(tr('Zapis wyniku'))
        self.mux = tk.BooleanVar(value=True)
        self.keep = tk.BooleanVar(value=False)
        self.move_done = tk.BooleanVar(value=True)
        check(g, 0, tr('wbuduj napisy do pliku MKV'), self.mux)
        check(g, 1, tr('zostaw oryginał (kopia .pl.mkv / .en.mkv)'), self.keep)
        check(g, 2, tr('gotowe → folder „{0}” (dyski lokalne)', DONE_DIR), self.move_done)

        g = group(tr('Gdy brak napisów'))
        self.whisper = tk.BooleanVar(value=True)
        self.asr = tk.StringVar(value="GigaAM")
        self.separate = tk.BooleanVar(value=True)
        check(g, 0, tr('rozpoznaj mowę z dźwięku'), self.whisper)
        ttk.Label(g, text=tr('Silnik:')).grid(row=1, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.asr, values=["GigaAM", "Whisper"], width=12,
                     state="readonly").grid(row=1, column=1, sticky="w", pady=2)
        # GigaAM zna tylko rosyjski — inne języki mowy zawsze rozpoznaje Whisper w wybranym rozmiarze
        ttk.Label(g, text=tr('Model Whisper:')).grid(row=2, column=0, sticky="w")
        self.whisper_model = tk.StringVar(value=WHISPER_MODELS[0])
        ttk.Combobox(g, textvariable=self.whisper_model, values=WHISPER_MODELS, width=14,
                     state="readonly").grid(row=2, column=1, sticky="w", pady=2)
        check(g, 3, tr('oddziel głos od muzyki (Demucs)'), self.separate)

        g = group(tr('Lektor'))
        self.lektor = tk.StringVar(value=tr("brak"))
        self.voice = tk.StringVar()
        self.replace_audio = tk.BooleanVar(value=False)
        ttk.Label(g, text=tr('Silnik:')).grid(row=0, column=0, sticky="w")
        lektor_box = ttk.Combobox(g, textvariable=self.lektor, values=[tr("brak"), "Piper", "XTTS-v2"],
                                  width=12, state="readonly")
        lektor_box.grid(row=0, column=1, sticky="w", pady=2)
        lektor_box.bind("<<ComboboxSelected>>", lambda e: self.lektor_changed())
        ttk.Label(g, text=tr('Głos:')).grid(row=1, column=0, sticky="w")
        self.voice_box = ttk.Combobox(g, textvariable=self.voice, width=26, state="disabled")
        self.voice_box.grid(row=1, column=1, sticky="ew", pady=2)
        self.sample_btn = ttk.Button(g, text=tr('Próbka głosu...'), command=self.pick_sample, state="disabled")
        self.sample_btn.grid(row=2, column=1, sticky="w", pady=2)
        check(g, 3, tr('usuń oryginalne audio'), self.replace_audio)

        g = group(tr('Po zakończeniu kolejki'))
        self.notify_toast = tk.BooleanVar(value=True)
        self.ntfy_topic = tk.StringVar()
        check(g, 0, tr('powiadomienie Windows'), self.notify_toast)
        ttk.Label(g, text=tr('Telefon (ntfy):')).grid(row=1, column=0, sticky="w")
        ttk.Entry(g, textvariable=self.ntfy_topic, width=20).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(g, text=tr('Wyślij próbne'), command=self.test_notify).grid(row=2, column=1, sticky="w", pady=2)

        # ---- pasek stanu: postęp całej kolejki i bieżącego pliku
        foot = ttk.Frame(root, padding=(10, 6, 10, 10))
        foot.pack(fill="x")
        self.progress = ttk.Progressbar(foot, length=260, maximum=1)
        self.progress.pack(side="left")
        self.status = ttk.Label(foot, text=tr('Dodaj pliki lub foldery do kolejki.'))
        self.status.pack(side="left", padx=10)
        self.file_progress = ttk.Label(foot, text="", foreground="#0b5cad")
        self.file_progress.pack(side="right")
        self.eta = ttk.Label(foot, text="", foreground="#57606a")
        self.eta.pack(side="right", padx=(0, 16))

        if not models:
            print(tr('UWAGA: Ollama nie odpowiada pod {0} — uruchom ją przed startem.', OLLAMA_URL))
        self.load_queue()
        self.add(paths)
        if os.environ.get("MKV_AUTOSTART") == "1":   # uruchomienie z harmonogramu/skryptu: kolejka rusza sama
            root.after(1500, self.start)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def state_of(self, item):
        return self.states.get(item, self.WAITING)

    def set_state(self, item, state):
        self.states[item] = state
        self.tree.set(item, "state", tr(self.STATE_LABELS.get(state, state)))

    def set_track(self, item, track):
        if track:
            self.tracks[item] = int(track)
        else:
            self.tracks.pop(item, None)
        self.tree.set(item, "track", f"#{track}" if track else tr("auto"))

    def switch_ui_lang(self):
        """Przebudowuje okno w wybranym języku; kolejka i ustawienia przechodzą przez kolejka.json."""
        from tkinter import messagebox
        lang = next((k for k, v in UI_LANGS.items() if v == self.ui_lang.get()), "pl")
        if lang == ui["lang"]:
            return
        if self.running:
            self.ui_lang.set(UI_LANGS[ui["lang"]])
            messagebox.showinfo("MKV Translator", tr('Język okna można zmienić, gdy kolejka nie pracuje.'))
            return
        self.save_queue()
        set_ui_lang(lang)
        stop_helper()                         # proces audio wystartuje z komunikatami w nowym języku
        self.save_queue()
        self.alive = False
        for child in self.root.winfo_children():
            child.destroy()
        QueueApp(self.root, [], self.queue_file)

    def track_dialog(self):
        """Wybór ścieżki napisów do tłumaczenia dla zaznaczonego filmu (zamiast wyboru automatycznego)."""
        from tkinter import messagebox
        selected = [i for i in self.tree.selection() if i != self.current]
        if not selected:
            return
        item = selected[0]
        path = Path(self.tree.set(item, "dir")) / self.tree.set(item, "file")
        if path.suffix.lower() not in VIDEO_EXTS:
            messagebox.showinfo("MKV Translator", tr('Ścieżkę napisów wybiera się tylko dla filmów.'))
            return
        try:
            streams = subtitle_streams(path)
        except Exception as e:                # np. plik usunięty albo nieczytelny
            messagebox.showerror("MKV Translator", tr('Nie da się odczytać ścieżek: {0}', e))
            return
        if not streams:
            messagebox.showinfo("MKV Translator", tr('Plik nie ma wbudowanych napisów.'))
            return
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title(tr('Ścieżka napisów — {0}', path.name))
        win.transient(self.root)
        win.grab_set()
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=tr('Którą ścieżkę tłumaczyć? (dotyczy {0} zaznaczonych plików)', len(selected))
                  ).pack(anchor="w")
        choice = tk.IntVar(value=self.tracks.get(item, 0))
        ttk.Radiobutton(frame, text=tr('automatycznie (najpełniejsza w języku źródłowym)'), value=0,
                        variable=choice).pack(anchor="w", pady=(6, 0))
        for n, st in enumerate(streams, 1):
            tags = st.get("tags", {})
            kind = tr("graficzne — OCR") if st.get("codec_name") == PGS else st.get("codec_name", "?")
            forced = tr(", wymuszone") if st.get("disposition", {}).get("forced") else ""
            text = f"#{n}  [{lang_of(st) or '?'}]  {tags.get('title', '')}  ({kind}{forced})"
            ttk.Radiobutton(frame, text=text, value=n, variable=choice).pack(anchor="w")

        def ok():
            for i in selected:
                self.set_track(i, choice.get() or None)
            self.save_queue()
            win.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=ok).pack(side="left")
        ttk.Button(buttons, text=tr('Anuluj'), command=win.destroy).pack(side="left", padx=6)

    def pull_dialog(self):
        """Pobieranie modelu do Ollamy (ollama pull) z postępem; po pobraniu odświeża listy modeli."""
        tk, ttk = self.tk, self.ttk
        if getattr(self, "pull_win", None) and self.pull_win.winfo_exists():
            self.pull_win.lift()
            return
        win = self.pull_win = tk.Toplevel(self.root)
        win.title(tr('Pobierz model Ollamy'))
        win.transient(self.root)
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=tr('Nazwa modelu (z ollama.com/library), np. gemma4:31b:')).pack(anchor="w")
        name = tk.StringVar(value="translategemma:27b")
        ttk.Combobox(frame, textvariable=name, width=36,
                     values=["gemma4:31b", "translategemma:27b", "gemma4:26b", "qwen3.5:27b", "gemma3:27b",
                             "translategemma:12b", "gemma3:12b"]).pack(fill="x", pady=4)
        self.pull_bar = ttk.Progressbar(frame, length=360, maximum=1)
        self.pull_bar.pack(fill="x", pady=4)
        self.pull_label = ttk.Label(frame, text=tr('Duże modele mają 15–20 GB — pobieranie trwa kilka minut.'),
                                    foreground="#57606a")
        self.pull_label.pack(anchor="w")
        button = ttk.Button(frame, text=tr('Pobierz'))
        button.pack(anchor="e", pady=(8, 0))

        def start():
            model = name.get().strip()
            if not model:
                return
            button.configure(state="disabled")
            threading.Thread(target=self.pull_work, args=(model,), daemon=True).start()

        button.configure(command=start)

    def pull_work(self, model):
        """Wątek pobierania: /api/pull wysyła postęp jako wiersze JSON."""
        body = json.dumps({"model": model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(f"{OLLAMA_URL}/api/pull", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                for line in r:
                    data = json.loads(line or b"{}")
                    if data.get("error"):
                        raise FatalError(data["error"])
                    total, done = data.get("total"), data.get("completed")
                    self.events.put(("pull", data.get("status", ""), done / total if total and done else None,
                                     False))
            print(tr('\nPobrano model Ollamy: {0}', model))
            self.events.put(("pull", tr('Gotowe: {0}', model), 1.0, True))
        except (urllib.error.URLError, OSError, ValueError, FatalError) as e:
            print(tr('\nNie udało się pobrać modelu {0}: {1}', model, e))
            self.events.put(("pull", tr('Błąd: {0}', e), None, True))

    def pull_progress(self, status, fraction, finished):
        win = getattr(self, "pull_win", None)
        if win is not None and win.winfo_exists():
            self.pull_label.configure(text=status + (f"  {fraction:.0%}" if fraction is not None else ""))
            if fraction is not None:
                self.pull_bar.configure(value=fraction)
        if finished:                          # nowy model od razu na listach wyboru
            models = ollama_models()
            self.model_box.configure(values=models)
            self.repair_box.configure(values=[tr(self.AUTO), *models])

    def lektor_changed(self):
        voices = {"Piper": PIPER_VOICES, "XTTS-v2": XTTS_VOICES}.get(self.lektor.get(), [])
        self.voice_box.configure(values=voices, state="normal" if voices else "disabled")
        self.voice.set(voices[0] if voices else "")
        self.sample_btn.configure(state="normal" if self.lektor.get() == "XTTS-v2" else "disabled")

    def pick_sample(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title=tr('Próbka głosu do naśladowania (6–30 s czystej mowy)'),
                                          filetypes=[(tr("Dźwięk"), "*.wav *.flac")])
        if path:
            self.voice.set(path)

    # ---- kolejka

    def add(self, paths):
        queued = {str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")) for i in self.tree.get_children()}
        for p in map(Path, paths):
            root = p.resolve() if p.is_dir() else p.resolve().parent
            for f in collect([p], self.recursive.get()):
                f = f.resolve()
                if str(f) not in queued and f.suffix.lower() in SUB_EXTS | VIDEO_EXTS:
                    queued.add(str(f))
                    item = self.tree.insert("", "end", values=(f.name, str(f.parent), "", ""))
                    self.set_state(item, self.WAITING)
                    self.set_track(item, None)
                    self.roots[item] = str(root)
        self.update_status()

    def finish_move(self, item, path):
        """Po zakończeniu: przeniesienie filmu z dysku lokalnego do folderu skonczone (w wątku roboczym)."""
        if not self.move_flag or path.suffix.lower() not in VIDEO_EXTS or not is_local(path) \
                or DONE_DIR in path.parts:          # już leży w "skonczone" (np. poprawiany ponownie)
            return
        try:
            new = move_done(path, self.roots.get(item) or path.parent)
            print(tr('    przeniesiono do {0}', new.parent))
            self.events.put(("renamed", item, new))
        except (FatalError, OSError) as e:
            print(tr('    nie przeniesiono do „{0}”: {1}', DONE_DIR, e))

    def add_files(self):
        from tkinter import filedialog
        self.add(filedialog.askopenfilenames(
            title=tr('Wybierz filmy MKV lub pliki napisów'),
            filetypes=[(tr("Filmy i napisy"), "*.mkv *.avi *.mp4 *.m4v *.ts *.mov *.wmv *.mpg *.webm *.srt *.ass *.ssa *.vtt *.sub"),
                       ("Matroska", "*.mkv"),
                       (tr("Napisy"), "*.srt *.ass *.ssa *.vtt *.sub")]))

    def add_folder(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title=tr('Wybierz folder z filmami MKV lub napisami'))
        if folder:
            self.add([folder])

    def remove(self):
        for item in self.tree.selection():
            if item != self.current:          # pliku w toku nie da się usunąć
                self.tree.delete(item)
        self.update_status()

    def clear_done(self):
        for item in self.tree.get_children():
            if self.state_of(item) not in (self.WAITING, self.RUNNING):
                self.tree.delete(item)
        self.update_status()

    def clear(self):
        for item in self.tree.get_children():
            if item != self.current:
                self.tree.delete(item)
        self.update_status()

    def move(self, step):
        items = list(self.tree.selection())
        for item in (items if step < 0 else reversed(items)):
            index = self.tree.index(item) + step
            if 0 <= index < len(self.tree.get_children()):
                self.tree.move(item, "", index)
        self.save_queue()

    def requeue(self):
        """Ustawia zaznaczone pliki z powrotem jako oczekujące (np. żeby przetłumaczyć je ponownie)."""
        for item in self.tree.selection():
            if item != self.current:
                self.set_state(item, self.WAITING)
        self.update_status()

    def check_polish(self):
        """Sprawdza oczekujące pliki (w tle) i oznacza jako "Ma polskie" te, które mają już polskie napisy."""
        from tkinter import messagebox
        jobs = [(i, Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")) for i in self.tree.get_children()
                if self.state_of(i) in (self.WAITING, "Błąd", "Pominięto")
                and Path(self.tree.set(i, "file")).suffix.lower() in VIDEO_EXTS]
        if self.running or not jobs:
            if not jobs:
                messagebox.showinfo("MKV Translator", tr('W kolejce nie ma oczekujących filmów do sprawdzenia.'))
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.move_flag = self.move_done.get()
        OUT["code"] = TARGET_CHOICES.get(untr(self.target.get(), TARGET_CHOICES), "pl")

        def work():
            print(tr('\nSprawdzanie, czy są już napisy ({0}): {1} plików', target_adj(), len(jobs)))
            found = 0
            for item, f in jobs:
                try:
                    what = polish_embedded(f, subtitle_streams(f))
                    if what:
                        print(tr('    {0}: wbudowane — {1}', f.name, what))
                    else:
                        own = f.with_suffix(f".{OUT['code']}.srt")
                        side = own if own.exists() else external_polish(f)
                        what = side and f"obok filmu — {side.name}"
                        if what:
                            print(f"    {f.name}: {what}")
                except Exception as e:        # jeden nieczytelny plik nie może zawiesić okna
                    print(tr('    {0}: nie da się sprawdzić ({1})', f.name, e))
                    continue
                if what:
                    found += 1
                    self.events.put(("marked", item, "Ma polskie"))
                    self.finish_move(item, f)
            print(tr('Z gotowymi napisami: {0} z {1} (oznaczone „Ma już napisy” — Start je pomija)', found, len(jobs)))
            self.events.put(("checked",))

        threading.Thread(target=work, daemon=True).start()

    def ts_to_mkv(self):
        """Zamienia pliki TS z kolejki na MKV (w tle) i podmienia ich ścieżki w kolejce."""
        from tkinter import messagebox
        items = [i for i in self.tree.get_children()
                 if Path(self.tree.set(i, "file")).suffix.lower() in (".ts", ".m2ts") and i != self.current]
        if self.running or not items:
            if not items:
                messagebox.showinfo("MKV Translator", tr('W kolejce nie ma plików TS.'))
            return
        if not messagebox.askyesno("MKV Translator", tr('Zamienić {0} plików TS na MKV?\n\nObraz i dźwięk zostają bez zmian (bez przekodowania), polskie napisy .pl.srt trafiają do środka. Pliki TS zostaną usunięte po sprawdzeniu nowych.', len(items))):
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        jobs = [(i, Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")) for i in items]

        def work():
            print(tr('\nZamiana TS na MKV: {0} plików', len(jobs)))
            try:
                for item, src in jobs:
                    try:
                        dst = remux_to_mkv(src)
                        print(f"    {src.name} -> {dst.name}")
                        self.events.put(("renamed", item, dst))
                    except (FatalError, OSError) as e:
                        print(tr('    {0}: BŁĄD: {1}', src.name, e))
                        if src.with_suffix(".mkv").exists() and not src.exists():
                            self.events.put(("renamed", item, src.with_suffix(".mkv")))
            finally:
                self.events.put(("checked",))   # przycisk Start zawsze wraca

        threading.Thread(target=work, daemon=True).start()

    def check_install(self):
        """Kontrola instalacji w tle; wynik trafia do okna logu i dziennika."""
        if self.running:
            return
        cancel.clear()                        # po wcześniejszym Stop sprawdzenie nie może się od razu przerwać
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status.configure(text=tr('Sprawdzam instalację...'))

        def work():
            try:
                check_install(self.model.get().strip())
            except Exception as e:
                print(tr('    BŁĄD: {0}: {1}', type(e).__name__, e))
            self.events.put(("checked",))

        threading.Thread(target=work, daemon=True).start()

    def plex_dialog(self):
        """Podgląd i zmiana nazw (oraz folderów) plików z kolejki na format Plex."""
        if self.running:                      # nie zmieniamy nazw plików, na których właśnie pracuje wątek w tle
            from tkinter import messagebox
            messagebox.showinfo("MKV Translator", tr('Zmiana nazw jest dostępna, gdy kolejka nie pracuje.'))
            return
        tk, ttk = self.tk, self.ttk
        items = {str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")): i
                 for i in self.tree.get_children() if i != self.current}
        win = tk.Toplevel(self.root)
        win.title(tr('Nazwy w formacie Plex'))
        win.geometry("1180x560")
        win.transient(self.root)
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        folders = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text=tr('przenieś do folderów  Serial \\ Season NN'), variable=folders,
                        command=lambda: refresh()).pack(side="left")
        info = ttk.Label(top)
        info.pack(side="left", padx=16)
        frame = ttk.Frame(win, padding=(8, 0))
        frame.pack(fill="both", expand=True)
        table = ttk.Treeview(frame, columns=("old", "new", "note"), show="headings")
        for col, text, width in (("old", tr("Obecnie"), 470), ("new", tr("Po zmianie"), 470), ("note", tr("Uwagi"), 200)):
            table.heading(col, text=text)
            table.column(col, width=width)
        scroll = ttk.Scrollbar(frame, command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        buttons = ttk.Frame(win, padding=8)
        buttons.pack(fill="x")
        state = {"ready": []}

        def short(path):                      # dwa ostatnie foldery i nazwa — tyle wystarcza do oceny
            return str(Path(*path.parts[-3:])) if folders.get() else path.name

        def refresh():
            plan = plex_renames([Path(p) for p in items], folders.get())
            state["ready"] = [(old, new) for old, new, note in plan if not note]
            table.delete(*table.get_children())
            for old, new, note in plan:
                table.insert("", "end", values=(short(old), short(new), note))
            info.configure(text=tr('Do zmiany: {0}    pominięte: {1}    bez zmian: {2}', len(state['ready']), len(plan) - len(state['ready']), len(items) - len(plan)))
            apply_btn.configure(text=tr('Zmień ({0})', len(state['ready'])),
                                state="normal" if state["ready"] else "disabled")

        def apply():
            changed, journal = 0, plex_log_read(self.plex_log)
            for old, new in state["ready"]:
                try:
                    journal += plex_rename(old, new)
                    plex_log_write(journal, self.plex_log)
                except OSError as e:
                    print(tr('Nie zmieniono {0}: {1}', old.name, e))
                    continue
                self.tree.set(items[str(old)], "file", new.name)
                self.tree.set(items[str(old)], "dir", str(new.parent))
                changed += 1
            print(tr('\nZmieniono na format Plex: {0} plików', changed))
            self.save_queue()
            win.destroy()

        def undo(ask=True):
            from tkinter import messagebox
            count = len(plex_log_read(self.plex_log))
            if ask and not messagebox.askyesno(
                    "MKV Translator", tr('Przywrócić poprzednie nazwy i foldery ({0} plików, razem z napisami)?', count),
                    parent=win):
                return
            restored, failed = plex_undo(self.plex_log)
            by_path = {str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")): i
                       for i in self.tree.get_children()}
            for current, original in restored:
                item = by_path.get(str(current))
                if item:
                    self.tree.set(item, "file", original.name)
                    self.tree.set(item, "dir", str(original.parent))
            print(tr('\nPrzywrócono stare nazwy: {0} plików', len(restored)) + (tr(', nie udało się: {0}', failed) if failed else ""))
            self.save_queue()
            win.destroy()

        apply_btn = ttk.Button(buttons, command=apply)
        apply_btn.pack(side="left")
        ttk.Button(buttons, text=tr('Anuluj'), command=win.destroy).pack(side="left", padx=6)
        logged = len(plex_log_read(self.plex_log))
        ttk.Button(buttons, text=tr('Przywróć stare nazwy ({0})', logged), command=undo,
                   state="normal" if logged and not self.running else "disabled").pack(side="right")
        refresh()
        return win, apply, folders, refresh, table, undo

    def load_queue(self):
        try:
            data = json.loads(self.queue_file.read_text(encoding="utf-8"))
        except (AttributeError, OSError, ValueError):
            return
        options = data.get("options", {})
        self.avg_seconds = (data.get("stats") or {}).get("avg_seconds")
        for name in self.SAVED_OPTIONS:
            if name in options and name != "voice":
                value = options[name]
                getattr(self, name).set(tr(value) if name in self.CHOICE_OPTIONS else value)
        self.lektor_changed()                 # lista głosów zależy od wybranego lektora
        if options.get("voice"):
            self.voice.set(options["voice"])
        for entry in data.get("items", []):
            path = Path(entry["path"])
            # plik przetwarzany w chwili zamknięcia programu traktujemy jak przerwany
            state = "Przerwano" if entry.get("state") == self.RUNNING else entry.get("state", self.WAITING)
            item = self.tree.insert("", "end", values=(path.name, str(path.parent), "", ""))
            self.set_state(item, state)
            self.set_track(item, entry.get("track"))
            if entry.get("root"):
                self.roots[item] = entry["root"]

    def save_queue(self):
        if not self.queue_file:
            return
        items = [{"path": str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")),
                  "state": self.state_of(i), "root": self.roots.get(i), "track": self.tracks.get(i)}
                 for i in self.tree.get_children()]
        options = {name: self.safe_parallel() if name == "parallel" else getattr(self, name).get()
                   for name in self.SAVED_OPTIONS}   # puste pole "równolegle" nie może wywrócić zapisu
        for name in self.CHOICE_OPTIONS:
            options[name] = untr(options[name], self.CHOICE_OPTIONS[name])
        options["ui_lang"] = ui["lang"]
        try:
            tmp = self.queue_file.with_name(self.queue_file.name + ".tmp")
            stats = {"avg_seconds": self.average_seconds()}
            tmp.write_text(json.dumps({"items": items, "options": options, "stats": stats}, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            os.replace(tmp, self.queue_file)
        except OSError:
            pass                              # brak zapisu kolejki nie może przerywać pracy

    def update_status(self):
        self.save_queue()
        items = self.tree.get_children()
        for item in items:                    # kolor wiersza według stanu
            self.tree.item(item, tags=(self.state_of(item),))
        states = [self.state_of(i) for i in items]
        counts = {s: states.count(s) for s in set(states)}
        parts = [tr('w kolejce {0}', len(states))]
        for label, state in ((tr('czeka'), self.WAITING), (tr('gotowe'), "Gotowe"), (tr('do sprawdzenia'), "Do sprawdzenia"),
                             (tr('ma już napisy'), "Ma polskie"),
                             (tr("pominięte"), "Pominięto"), (tr("przerwane"), "Przerwano"), (tr("błędy"), "Błąd")):
            if counts.get(state):
                parts.append(f"{label} {counts[state]}")
        self.status.configure(text="  ·  ".join(parts))
        finished = len(states) - counts.get(self.WAITING, 0) - counts.get(self.RUNNING, 0)
        self.progress.configure(maximum=max(1, len(states)), value=finished)

    @staticmethod
    def fill_menu(menu, entries):
        """Pozycje menu: (etykieta, polecenie) albo None jako separator."""
        for entry in entries:
            if entry is None:
                menu.add_separator()
            else:
                menu.add_command(label=entry[0], command=entry[1])

    def show_server_parallel(self):
        if ollama_is_local():
            value = ollama_server_parallel()
            self.server_par.configure(text=tr('Ollama: {0}', value if value is not None else tr('domyślnie')))

    def apply_parallel(self, then_start):
        """Wyrównuje OLLAMA_NUM_PARALLEL do pola "Równolegle" (restart Ollamy w tle); potem ewentualnie kolejka."""
        want = self.safe_parallel()
        current = ollama_server_parallel()
        if not ollama_is_local() or current == want:
            if not then_start:
                print(tr('\nOllama ma już ustawione: równolegle {0}', want) if ollama_is_local()
                      else tr('\nOllama działa na innym komputerze ({0}) — tam trzeba ją ustawić', OLLAMA_URL))
            return False
        if self.running:
            print(tr('\nOllamy nie restartuję w trakcie pracy — zmiana wejdzie przy następnym Starcie'))
            return False
        print(tr('\nOllama: równolegle {0} → {1} — restartuję Ollamę...', current if current is not None else tr('domyślnie'), want))
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status.configure(text=tr('Restart Ollamy z nowym ustawieniem równoległości...'))

        def work():
            try:
                restart_ollama(want)
                print(tr('    Ollama działa z OLLAMA_NUM_PARALLEL={0}', want))
            except Exception as e:
                print(tr('    nie udało się zrestartować Ollamy: {0}', e))
            self.events.put(("ollama_ready", then_start))

        threading.Thread(target=work, daemon=True).start()
        return True

    def repair_flagged(self):
        """Ponowne tłumaczenie (z automatyczną naprawą) plików oznaczonych "Do sprawdzenia" — zaznaczonych,
        a gdy nic nie zaznaczono — wszystkich takich w kolejce."""
        from tkinter import messagebox
        chosen = [i for i in (self.tree.selection() or self.tree.get_children())
                  if self.state_of(i) == "Do sprawdzenia"]
        if not chosen:
            messagebox.showinfo("MKV Translator", tr('W kolejce nie ma plików „Do sprawdzenia”.'))
            return
        if not messagebox.askyesno("MKV Translator", tr('Przetłumaczyć ponownie {0} plików z automatyczną naprawą?\n\nObecne polskie napisy zostaną zastąpione nowymi (kopia .pl.bak.srt zostaje przy plikach AVI). Pliki z rozpoznawaniem mowy przejdą je ponownie.', len(chosen))):
            return
        for item in chosen:
            self.modes[item] = "repair"
            self.set_state(item, self.WAITING)
        self.update_status()
        if not self.running:
            self.start()

    def test_notify(self):
        topic = self.ntfy_topic.get().strip()
        notify("MKV Translator", tr('Próbne powiadomienie — tak będzie wyglądać informacja o końcu kolejki.'),
               topic or None, self.notify_toast.get())
        print(tr('\nWysłano próbne powiadomienie') + (tr(' (ntfy: {0})', topic) if topic else tr(' (tylko Windows)')))

    def average_seconds(self):
        """Średni czas jednego pliku: z tej sesji, a na początku — z poprzednich."""
        if self.durations:
            recent = self.durations[-20:]
            return sum(recent) / len(recent)
        return self.avg_seconds

    def update_eta(self):
        avg = self.average_seconds()
        waiting = sum(self.state_of(i) == self.WAITING for i in self.tree.get_children())
        if not self.running or not self.current or not avg:
            self.eta.configure(text="")
            return
        elapsed = time.time() - (self.file_started or time.time())
        left = avg * waiting + max(0.0, avg - elapsed)
        end = time.strftime("%H:%M", time.localtime(time.time() + left))
        self.eta.configure(text=tr('do końca ok. {0} (ok. {1})', fmt_duration(left), end))

    def queue_finished(self):
        """Powiadomienie po zakończeniu kolejki albo jej zatrzymaniu przez awarię (nie po ręcznym Stop)."""
        if not self.run_started or (cancel.is_set() and not self.stop_reason):
            return
        states = [self.state_of(i) for i in self.tree.get_children()]
        summary = ", ".join(f"{label} {states.count(s)}" for label, s in
                            ((tr('gotowe'), "Gotowe"), (tr('do sprawdzenia'), "Do sprawdzenia"), (tr('ma już napisy'), "Ma polskie"),
                             (tr('błędy'), "Błąd"), (tr('czeka'), self.WAITING)) if states.count(s))
        took = fmt_duration(time.time() - self.run_started)
        if self.stop_reason:
            title, text = tr('MKV Translator: kolejka zatrzymana'), tr('{0}\n{1} (po {2})', self.stop_reason, summary, took)
        else:
            title, text = tr('MKV Translator: kolejka zakończona'), f"{summary} — czas {took}"
        topic = self.ntfy_topic.get().strip()
        if self.notify_toast.get() or topic:
            threading.Thread(target=notify, args=(title, text, topic or None, self.notify_toast.get()),
                             daemon=True).start()

    def open_report(self):
        if QUALITY_REPORT.exists():
            os.startfile(QUALITY_REPORT)
        else:
            from tkinter import messagebox
            messagebox.showinfo("MKV Translator", tr('Raport jakości jeszcze nie istnieje — powstaje przy tłumaczeniu.'))

    def open_log(self):
        log = self.queue_file.with_name("dziennik.txt") if self.queue_file else None
        if log and log.exists():
            os.startfile(log)

    def open_folder(self):
        selected = self.tree.selection()
        if selected:
            folder = Path(self.tree.set(selected[0], "dir"))
            if folder.exists():
                os.startfile(folder)

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.row_menu.tk_popup(event.x_root, event.y_root)

    # ---- przetwarzanie

    def start(self):
        if self.running:
            return
        # Start ponawia przerwane, a gdy nic nie czeka — także błędne i pominięte.
        # Gotowych nie rusza: do ponownego tłumaczenia służy "Ponów zaznaczone".
        items = self.tree.get_children()
        interrupted = [i for i in items if self.state_of(i) == "Przerwano"]
        if interrupted:
            answer = self.ask_resume(len(interrupted))
            if answer is None:
                return
            for item in interrupted:
                self.modes[item] = "resume" if answer else "restart"
                self.set_state(item, self.WAITING)
        if not any(self.state_of(i) == self.WAITING for i in items):
            for item in items:
                if self.state_of(item) in ("Błąd", "Pominięto"):
                    self.set_state(item, self.WAITING)
        if not any(self.state_of(i) == self.WAITING for i in items):
            self.status.configure(text=tr('Nic nie czeka w kolejce — dodaj pliki albo użyj „Ponów zaznaczone”.'))
            return
        cancel.clear()
        self.run_started, self.stop_reason = time.time(), None
        print(time.strftime("\n===== Start %Y-%m-%d %H:%M ====="))
        if self.apply_parallel(then_start=True):   # najpierw restart Ollamy z nową równoległością
            self.stop_btn.configure(state="normal")
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.next()

    def safe_parallel(self):
        try:
            return min(8, max(1, int(self.parallel.get())))
        except (tk_error(), ValueError):
            return 2

    def ask_resume(self, count):
        """True = kontynuuj przerwane pliki, False = zacznij je od nowa, None = anuluj."""
        from tkinter import messagebox
        return messagebox.askyesnocancel(
            "MKV Translator", tr('Przerwane pliki: {0}.\n\nTak — kontynuuj od miejsca przerwania\nNie — zacznij te pliki od nowa\nAnuluj — nie uruchamiaj kolejki', count))

    def stop(self):
        cancel.set()
        self.stop_btn.configure(state="disabled")
        print(tr('\nZatrzymuję po bieżącym fragmencie...'))

    def next(self):
        """Uruchamia pierwszy oczekujący plik — kolejkę można zmieniać w trakcie pracy."""
        item = next((i for i in self.tree.get_children() if self.state_of(i) == self.WAITING), None)
        if item is None or cancel.is_set():
            self.running, self.current = False, None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            print(tr('\nZatrzymano.') if cancel.is_set() else tr('\nKolejka zakończona.'))
            self.file_progress.configure(text="")
            stop_helper()                     # karta graficzna wolna (np. dla gry), gdy kolejka nie pracuje
            self.queue_finished()
            self.update_status()
            self.update_eta()
            return
        self.current = item
        self.file_started = time.time()
        self.file_progress.configure(text=tr('{0} — start', self.tree.set(item, 'file')))
        self.set_state(item, self.RUNNING)
        self.tree.see(item)
        self.update_status()
        mkv = Path(self.tree.set(item, "dir")) / self.tree.set(item, "file")
        lektor = {"Piper": "piper", "XTTS-v2": "xtts"}.get(self.lektor.get())
        args = argparse.Namespace(mux=self.mux.get(), force=self.force.get(), track=self.tracks.get(item),
                                  replace=not self.keep.get(),
                                  whisper=self.whisper.get(), asr=self.asr.get().lower(), separate=self.separate.get(),
                                  lektor=lektor,
                                  voice=self.voice.get().strip() or None, replace_audio=self.replace_audio.get(),
                                  source=SOURCE_CHOICES.get(untr(self.source.get(), SOURCE_CHOICES), "auto"),
                                  target=TARGET_CHOICES.get(untr(self.target.get(), TARGET_CHOICES), "pl"),
                                  whisper_model=self.whisper_model.get())
        repair = untr(self.repair_model.get(), (self.AUTO,))
        set_repair_model(None if repair == self.AUTO else repair)
        mode = self.modes.pop(item, None)
        args.resume, args.restart = mode == "resume", mode == "restart"
        if mode == "repair":                  # "Popraw oznaczone": tłumaczenie od nowa mimo polskich napisów
            args.force = True
        self.move_flag = self.move_done.get()
        threading.Thread(target=self.work, args=(item, mkv, args, self.model.get().strip(), self.safe_parallel()), daemon=True).start()

    def work(self, item, mkv, args, model, parallel):
        try:
            if self.engine is None or self.engine.model != model:
                self.engine = OllamaEngine(model)
            self.engine.parallel = parallel
            state = process(mkv, args, self.engine)
            if state in self.FINISHED:
                self.finish_move(item, mkv)
        except Cancelled:
            state = "Przerwano"
        except ToolDown as e:
            # awaria Ollamy/środowiska audio dotyczy wszystkich plików — kolejka staje, zamiast oznaczać je jako błędne
            print(tr('    BŁĄD: {0}\n    Zatrzymuję kolejkę — po naprawie wystarczy Start (plik zostanie wznowiony).', e))
            self.stop_reason = str(e)
            cancel.set()
            state = "Przerwano"
        except FatalError as e:
            print(tr('    BŁĄD: {0}', e))
            state = "Błąd"
        except Exception as e:  # nieprzewidziany błąd nie może zatrzymać kolejki
            print(tr('    BŁĄD: {0}: {1}', type(e).__name__, e))
            state = "Błąd"
        self.events.put(("done", item, state))

    def poll(self):
        import queue
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.write_log(event[1])
                elif event[0] == "renamed":
                    if self.tree.exists(event[1]):
                        self.tree.set(event[1], "file", event[2].name)
                        self.tree.set(event[1], "dir", str(event[2].parent))
                    self.save_queue()
                elif event[0] == "marked":
                    if self.tree.exists(event[1]):
                        self.set_state(event[1], event[2])
                    self.update_status()
                elif event[0] == "ollama_ready":
                    self.engine = None            # nowy serwer — połączenie zestawiane od nowa
                    self.show_server_parallel()
                    if event[1] and not cancel.is_set():
                        self.next()               # kolejka startuje po restarcie Ollamy
                    else:
                        self.running = False
                        self.start_btn.configure(state="normal")
                        self.stop_btn.configure(state="disabled")
                        self.update_status()
                elif event[0] == "pull":
                    self.pull_progress(*event[1:])
                elif event[0] == "checked":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.update_status()
                else:
                    if self.tree.exists(event[1]):
                        self.set_state(event[1], event[2])
                    took = time.time() - (self.file_started or time.time())
                    if event[2] in self.FINISHED and took > 20:   # szybkie pominięcia zaniżałyby średnią
                        self.durations.append(took)
                    self.save_queue()
                    self.next()
        except queue.Empty:
            pass
        finally:
            # okno przebudowane w innym języku — ta kopia przestaje odświeżać
            if self.alive:
                self.ticks = getattr(self, "ticks", 0) + 1
                if self.ticks % 50 == 0:      # co 5 s odświeżenie szacowanego końca
                    self.update_eta()
                self.root.after(100, self.poll)   # także po nieprzewidzianym błędzie — inaczej okno przestaje reagować

    def write_log(self, text):
        found = self.PROGRESS_RE.findall(text)
        if found and self.current and self.tree.exists(self.current):
            what, done, total = found[-1]
            self.file_progress.configure(text=f"{self.tree.set(self.current, 'file')} — {what} {done}/{total}")
        self.log.configure(state="normal")
        first, *rest = text.split("\r")
        self.log.insert("end", first)
        for part in rest:                     # "\r" = nadpisz bieżącą linię (licznik postępu)
            self.log.delete("end-1c linestart", "end-1c")
            self.log.insert("end", part)
        self.log.see("end")
        self.log.configure(state="disabled")

    def close(self):
        if self.running:
            from tkinter import messagebox
            if not messagebox.askyesno("MKV Translator", tr('Tłumaczenie trwa. Przerwać i zamknąć?')):
                return
        self.save_queue()
        stop_helper()                         # proces audio z modelami w pamięci karty
        self.root.destroy()


def run_gui(paths):
    import tkinter as tk
    queue_file = APP_DIR / "kolejka.json"
    try:                                      # język okna zapisany z kolejką — potrzebny przed budową okna
        set_ui_lang(json.loads(queue_file.read_text(encoding="utf-8")).get("options", {}).get("ui_lang", "pl"))
    except (OSError, ValueError):
        pass
    root = tk.Tk()
    QueueApp(root, paths, queue_file)
    root.mainloop()
    return 0


if __name__ == "__main__":
    # plik .exe to zawsze okno kolejki (przeciągnięte pliki trafiają do kolejki);
    # skrypt otwiera okno bez argumentów albo z --gui, w pozostałych przypadkach działa w konsoli
    if getattr(sys, "frozen", False) or len(sys.argv) == 1 or sys.argv[1] == "--gui":
        sys.exit(run_gui([a for a in sys.argv[1:] if a != "--gui"]))
    sys.exit(main())
