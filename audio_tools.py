#!/usr/bin/env python3
"""Narzędzia audio dla MKV Translator — uruchamiane Pythonem ze środowiska audio_env.

    audio_tools.py transcribe <dźwięk.wav> <wynik.srt> [--engine whisper|gigaam] [--separate]
                                                            rosyjska mowa -> napisy
    audio_tools.py tts <napisy.srt> <wynik.wav> --engine piper|xtts [--voice ...]
                                                            polskie napisy -> ścieżka lektora
    audio_tools.py check                                    kontrola modeli, pobranie brakujących plików
    audio_tools.py serve                                    polecenia z wejścia, modele zostają w pamięci
"""
import argparse
import importlib
import time
import traceback
import hashlib
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent
MODELS = BASE / "audio_models"
# modele trzymamy przy programie, nie w profilu użytkownika
os.environ.setdefault("HF_HOME", str(MODELS / "hf"))
os.environ.setdefault("TTS_HOME", str(MODELS))
os.environ.setdefault("COQUI_TOS_AGREED", "1")   # licencja CPML (niekomercyjna) zaakceptowana przy instalacji
# zmienna ustawiana przez antywirusy (np. Avast) wywraca OpenSSL w Pythonie: "no OPENSSL_Applink"
os.environ.pop("SSLKEYLOGFILE", None)
try:
    # certyfikaty z magazynu Windows — inaczej pobieranie modeli pada, gdy antywirus skanuje HTTPS
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

WHISPER_MODEL = "large-v3"
XTTS_MODEL = "tts_models/multilingual/multi-dataset/xtts_v2"
PIPER_VOICE = "pl_PL-darkman-medium"
XTTS_VOICE = "Damien Black"
MAX_SPEEDUP = 1.4        # o ile najwyżej przyspieszyć kwestię, która nie mieści się w czasie
LAG_SPEEDUP = 1.8        # ...a o ile, gdy lektor jest już spóźniony względem napisów
MAX_CUE_CHARS = 84      # dwie linie po 42 znaki
MAX_CUE_SECONDS = 6.0

# język komunikatów: ten sam słownik co w translate_mkv.py, język przekazuje MKV_UI_LANG
try:
    from ui_en import EN
except ImportError:
    EN = {}
EDGE_RE = re.compile(r"(\s*)(.*?)(\s*)$", re.S)


def tr(text, *args):
    if os.environ.get("MKV_UI_LANG", "pl") != "pl":
        lead, core, trail = EDGE_RE.match(text).groups()
        text = lead + EN.get(core, core) + trail
    return text.format(*args) if args else text


TIMING_RE = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


ERROR_LOG = BASE / "audio_blad.txt"           # pełne opisy błędów narzędzi audio


def log_error(stage):
    """Dopisuje do pliku pełny opis bieżącego wyjątku — sam ostatni wiersz nie mówi, co zawiodło."""
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')}  {stage}  {' '.join(sys.argv[1:])}\n")
            f.write(traceback.format_exc())
    except OSError:
        pass


_models = {}   # wczytane modele; w trybie "serve" zostają w pamięci między kolejnymi plikami
END_MARK = "\x1eKONIEC"                       # w trybie "serve": koniec polecenia, dalej kod wyniku i opis błędu


def cached(key, load):
    """Model z pamięci albo wczytany po raz pierwszy (load() wypisuje, że wczytuje)."""
    if key not in _models:
        _models[key] = load()
    return _models[key]


def progress(label, done, total):
    print(f"\r    {label} {done}/{total}", end="", flush=True)


def srt_time(seconds):
    ms = max(0, round(seconds * 1000))
    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"


# ---------------------------------------------------------------- Whisper

def split_segment(words):
    """Dzieli rozpoznany fragment na kwestie o rozmiarze napisów: (start, koniec, tekst)."""
    cues, current = [], []
    for w in words:
        if current:
            text = "".join(x.word for x in current)
            too_long = len(text) + len(w.word) > MAX_CUE_CHARS or w.end - current[0].start > MAX_CUE_SECONDS
            sentence_end = text.rstrip().endswith((".", "!", "?", "…")) and len(text) > 30
            if too_long or sentence_end or w.start - current[-1].end > 1.0:
                cues.append(current)
                current = []
        current.append(w)
    if current:
        cues.append(current)
    return [(c[0].start, c[-1].end, "".join(x.word for x in c).strip()) for c in cues]


def load_wav(path):
    """Wczytuje 16-bitowy WAV (przygotowany przez ffmpeg) bez dekodera PyAV: (próbki × kanały, częstotliwość)."""
    with wave.open(str(path), "rb") as w:
        if w.getsampwidth() != 2:
            sys.exit("Oczekiwano 16-bitowego pliku WAV.")
        data = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
        return data.reshape(-1, w.getnchannels()), w.getframerate()


def separate_vocals(audio, rate, device):
    """Oddziela głos od muzyki i efektów (Demucs); zwraca sam głos jako mono 16 kHz."""
    import torch
    import torchaudio.functional as F
    from demucs.apply import apply_model
    from demucs.pretrained import get_model

    def load():
        print(tr('    oddzielanie głosu od tła (Demucs) — wczytuję model...'), flush=True)
        return get_model("htdemucs").to(device).eval()

    model = cached(("demucs", device), load)  # wczytanie trwa ok. 35 s — w trybie "serve" tylko raz
    if rate != model.samplerate or audio.shape[1] != 2:
        sys.exit(tr("Demucs wymaga dźwięku stereo {0} Hz.", model.samplerate))
    vocals_at = model.sources.index("vocals")
    block = model.samplerate * 300            # po 5 minut, żeby nie trzymać całego filmu w pamięci
    parts = []
    for pos in range(0, len(audio), block):
        chunk = torch.from_numpy(audio[pos:pos + block].T.copy())
        ref = chunk.mean(0)
        mean, std = ref.mean(), ref.std() + 1e-8
        with torch.inference_mode():
            sources = apply_model(model, ((chunk - mean) / std)[None], device=device, split=True, overlap=0.25)[0]
        parts.append((sources[vocals_at] * std + mean).mean(0).cpu())
        done = min(100, round(100 * (pos + block) / len(audio)))
        print(tr('\r    oddzielanie głosu od tła: {0}%', done), end="", flush=True)
    print()
    torch.cuda.empty_cache()                  # bufory pośrednie; sam model zostaje w pamięci
    return F.resample(torch.cat(parts), rate, 16000).numpy()


def use_torch_cuda_libs():
    """Udostępnia biblioteki CUDA dołączone do PyTorcha (dla ctranslate2 i onnxruntime); zwraca "cuda"/"cpu"."""
    import torch
    lib = Path(torch.__file__).parent / "lib"
    if str(lib) not in os.environ["PATH"]:    # w trybie "serve" wywoływane przy każdym pliku
        os.environ["PATH"] = f"{lib}{os.pathsep}{os.environ['PATH']}"
        os.add_dll_directory(str(lib))
    return "cuda" if torch.cuda.is_available() else "cpu"


def whisper_model(name, device):
    from faster_whisper import WhisperModel

    def load():
        print(tr('    Whisper {0} ({1}) — wczytuję model...', name, device), flush=True)
        return WhisperModel(name, device=device, compute_type="float16" if device == "cuda" else "int8")

    return cached(("whisper", name, device), load)


def whisper_cues(audio, args, device):
    model = whisper_model(args.model, device)
    # ustawienia nastawione na to, żeby nie gubić kwestii: czuły wykrywacz mowy (ciche dialogi, mowa
    # na tle muzyki), zachowywanie fragmentów o niskiej pewności i brak "ciągnięcia" poprzedniego tekstu,
    # przez które Whisper potrafi przeskoczyć dłuższy fragment
    segments, info = model.transcribe(
        audio, language=args.language, word_timestamps=True, beam_size=8,
        vad_filter=True, vad_parameters={"threshold": 0.25, "min_silence_duration_ms": 700, "speech_pad_ms": 500},
        condition_on_previous_text=False, no_speech_threshold=0.85, log_prob_threshold=-1.5,
        hallucination_silence_threshold=2.0)
    cues = []
    for seg in segments:
        cues += split_segment(seg.words) if seg.words else [(seg.start, seg.end, seg.text.strip())]
        print(tr('\r    rozpoznawanie mowy: {0}%', min(100, round(100 * seg.end / info.duration))), end="", flush=True)
    print()
    return cues


class Word:
    def __init__(self, word, start, end):
        self.word, self.start, self.end = word, start, end


def gigaam_cues(audio, device):
    """GigaAM v3 (model tylko dla rosyjskiego) z podziałem na fragmenty mowy przez Silero VAD."""
    import onnx_asr

    def load():
        print(tr('    GigaAM v3 ({0}) — wczytuję model...', device), flush=True)
        cpu = ["CPUExecutionProvider"]
        # DEFAULT: bez tego cuDNN dobiera algorytm od nowa dla każdej długości fragmentu (kilkanaście razy wolniej)
        gpu = [("CUDAExecutionProvider", {"cudnn_conv_algo_search": "DEFAULT"})] if device == "cuda" else []
        return onnx_asr.load_model("gigaam-v3-e2e-rnnt", providers=gpu + cpu), onnx_asr.load_vad("silero", providers=cpu)

    model, vad = cached(("gigaam", device), load)
    segments = model.with_vad(vad, threshold=0.25, min_silence_duration_ms=250, speech_pad_ms=150,
                              max_speech_duration_s=20).with_timestamps().recognize(audio, sample_rate=16000)
    total = len(audio) / 16000
    cues = []
    for seg in segments:
        words = []
        for token, at in zip(seg.tokens or [], seg.timestamps or []):
            if token.startswith(" ") or not words:
                words.append(Word(token, seg.start + at, seg.end))
            else:
                words[-1].word += token       # dalszy kawałek tego samego słowa albo znak interpunkcyjny
        for prev, nxt in zip(words, words[1:]):
            prev.end = nxt.start
        cues += split_segment(words) if words else [(seg.start, seg.end, seg.text.strip())]
        print(tr('\r    rozpoznawanie mowy: {0}%', min(100, round(100 * seg.end / total))), end="", flush=True)
    print()
    return merge_short(cues)


def merge_short(cues, gap=0.35, max_chars=60, max_seconds=5.0):
    """Skleja sąsiednie króciutkie kwestie (VAD tnie mowę na każdej pauzie) w czytelne napisy."""
    out = []
    for start, end, text in cues:
        if out and start - out[-1][1] <= gap and len(out[-1][2]) + len(text) < max_chars \
                and end - out[-1][0] <= max_seconds:
            out[-1] = (out[-1][0], end, f"{out[-1][2]} {text}")
        else:
            out.append((start, end, text))
    return out


def transcribe(args):
    device = use_torch_cuda_libs()
    audio, rate = load_wav(args.audio)
    if args.separate:
        audio = separate_vocals(audio, rate, device)
    elif rate != 16000 or audio.shape[1] != 1:
        sys.exit("Oczekiwano pliku WAV 16 kHz mono.")
    else:
        audio = audio[:, 0]
    cues = None
    if args.engine == "gigaam" and args.language == "ru":   # GigaAM zna tylko rosyjski
        # GigaAM potrafi sporadycznie paść przy wczytywaniu; ponawiamy, zamiast tracić gotowe oddzielenie głosu
        for attempt in range(1, 4):
            try:
                cues = gigaam_cues(audio, device)
                break
            except Exception as e:
                log_error(f"GigaAM, próba {attempt}")
                traceback.print_exc()
                _models.pop(("gigaam", device), None)   # następna próba wczytuje model od nowa
                print(tr('\n    GigaAM: błąd ({0}), próba {1} z 3', type(e).__name__, attempt), flush=True)
                importlib.invalidate_caches()
                time.sleep(5)
        if cues is None:
            print(tr('    GigaAM nie działa — rozpoznaję mowę Whisperem'), flush=True)
    if cues is None:
        cues = whisper_cues(audio, args, device)
    cues = [c for c in cues if c[2]]
    with open(args.out, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(cues, 1):
            f.write(f"{i}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n\n")
    print(tr('    rozpoznano {0} kwestii', len(cues)))


def detect(args):
    """Język mowy (Whisper, tylko fragmenty z mową); wynik "kod prawdopodobieństwo" w pliku args.out."""
    device = use_torch_cuda_libs()
    audio, rate = load_wav(args.audio)
    if rate != 16000 or audio.shape[1] != 1:
        sys.exit("Oczekiwano pliku WAV 16 kHz mono.")
    model = whisper_model(args.model, device)
    lang, prob, _ = model.detect_language(audio[:, 0], vad_filter=True, language_detection_segments=4)
    Path(args.out).write_text(f"{lang} {prob:.3f}", encoding="utf-8")
    print(tr('    rozpoznany język mowy: {0} (pewność {1:.0%})', lang, prob))


# ---------------------------------------------------------------- OCR napisów graficznych (PGS z Blu-ray)

def pgs_rle(data, width, height):
    """Dekoduje obraz PGS (RLE) do tablicy indeksów palety height × width."""
    img = np.zeros((height, width), np.uint8)
    x = y = i = 0
    n = len(data)
    while i < n and y < height:
        b = data[i]; i += 1
        if b:
            count, color = 1, b
        else:
            f = data[i]; i += 1
            if f == 0:                        # koniec linii
                x, y = 0, y + 1
                continue
            if f & 0x40:
                count = ((f & 0x3F) << 8) | data[i]; i += 1
            else:
                count = f & 0x3F
            if f & 0x80:
                color = data[i]; i += 1
            else:
                color = 0
        img[y, x:x + count] = color
        x += count
    return img


def pgs_frames(path):
    """Plansze napisów PGS: lista (start s, koniec s albo None, obraz w skali szarości: ciemny tekst na białym)."""
    import struct
    raw = Path(path).read_bytes()
    pos, palettes, objects, frames, current = 0, {}, {}, [], None
    while pos + 13 <= len(raw) and raw[pos:pos + 2] == b"PG":
        pts = struct.unpack(">I", raw[pos + 2:pos + 6])[0] / 90000
        kind, size = raw[pos + 10], struct.unpack(">H", raw[pos + 11:pos + 13])[0]
        seg = raw[pos + 13:pos + 13 + size]
        pos += 13 + size
        if kind == 0x16:                      # kompozycja: co i gdzie pokazać
            width, height = struct.unpack(">HH", seg[0:4])
            palette_id, count = seg[9], seg[10]
            placed, p = [], 11
            for _ in range(count):
                obj_id, _, flag, ox, oy = struct.unpack(">HBBHH", seg[p:p + 8])
                placed.append((obj_id, ox, oy))
                p += 16 if flag & 0x80 else 8
            if frames and frames[-1][1] is None:
                frames[-1][1] = pts           # poprzednia plansza znika (także przy pustej kompozycji)
            current = (pts, width, height, palette_id, placed) if placed else None
        elif kind == 0x14:                    # paleta: indeks -> (jasność, przezroczystość)
            pal = palettes.setdefault(seg[0], {})
            for p in range(2, len(seg) - 4, 5):
                pal[seg[p]] = (seg[p + 1], seg[p + 4])
        elif kind == 0x15:                    # obraz (może być w kilku kawałkach)
            obj_id, flags = struct.unpack(">H", seg[0:2])[0], seg[3]
            if flags & 0x80:
                w, h = struct.unpack(">HH", seg[7:11])
                objects[obj_id] = [w, h, bytearray(seg[11:])]
            elif obj_id in objects:
                objects[obj_id][2] += seg[4:]
        elif kind == 0x80 and current is not None:   # koniec zestawu: składamy planszę
            pts0, width, height, palette_id, placed = current
            pal = palettes.get(palette_id, {})
            lut_y = np.zeros(256, np.float32)
            lut_a = np.zeros(256, np.float32)
            for idx, (yy, aa) in pal.items():
                lut_y[idx], lut_a[idx] = yy, aa / 255
            parts = []
            for obj_id, ox, oy in placed:
                if obj_id in objects:
                    w, h, data = objects[obj_id]
                    parts.append((ox, oy, pgs_rle(bytes(data), w, h)))
            if parts:
                x0 = min(p[0] for p in parts); y0 = min(p[1] for p in parts)
                x1 = max(p[0] + p[2].shape[1] for p in parts); y1 = max(p[1] + p[2].shape[0] for p in parts)
                canvas = np.full((y1 - y0 + 20, x1 - x0 + 20), 255, np.uint8)
                for ox, oy, idx in parts:
                    # jasny tekst z ciemnym obrysem na przezroczystym tle -> ciemny tekst na białym
                    value = 255 - lut_y[idx] * lut_a[idx]
                    region = canvas[oy - y0 + 10:oy - y0 + 10 + idx.shape[0], ox - x0 + 10:ox - x0 + 10 + idx.shape[1]]
                    np.minimum(region, value.astype(np.uint8), out=region)
                frames.append([pts0, None, canvas])
            current = None
    return frames


OCR_CONTRACTIONS = {"Im": "I'm", "Ive": "I've", "Id": "I'd", "Youre": "You're", "youre": "you're", "Dont": "Don't",
                    "dont": "don't", "Cant": "Can't", "cant": "can't", "Wont": "Won't", "wont": "won't",
                    "Isnt": "Isn't", "isnt": "isn't", "Didnt": "Didn't", "didnt": "didn't", "Doesnt": "Doesn't",
                    "doesnt": "doesn't", "Thats": "That's", "thats": "that's", "Whats": "What's", "whats": "what's",
                    "Theyre": "They're", "theyre": "they're", "Hes": "He's", "Shes": "She's", "Youve": "You've",
                    "youve": "you've", "Wasnt": "Wasn't", "wasnt": "wasn't", "Couldnt": "Couldn't",
                    "couldnt": "couldn't", "Wouldnt": "Wouldn't", "wouldnt": "wouldn't", "Ill": "I'll"}


def ocr_cleanup(text, language):
    """Typowe pomyłki OCR w napisach: ';' zamiast ',', ':' zamiast '.', zgubione apostrofy (angielski)."""
    text = re.sub(r";(?=\s|$)", ",", text)
    text = re.sub(r"(?<=\w):(?=\s*$|\s*\n)", ".", text, flags=re.MULTILINE)
    text = re.sub(r"(?<=\w): (?=[A-ZА-ЯЁ])", ". ", text)       # "waste them: It's" -> "waste them. It's"
    text = re.sub(r"(?<![^\s\"(])\|(?![^\s,.!?'])", "I", text)  # pionowa kreska zamiast "I"
    text = re.sub(r":\.", ".", text).replace("_", ".")
    if language == "en":
        text = re.sub(r"\b[Il]Il\b|\bIIl\b", "I'll", text)
        text = re.sub(r"\b(go|no|Oh|oh|Hey|hey|Yes|yes|Come|come)l(?=\s|$)", r"\1!", text)   # "l" zamiast "!"
    if language == "en":
        text = re.sub(r"\b\w+\b", lambda m: OCR_CONTRACTIONS.get(m.group(), m.group()), text)
    return text


def ocr(args):
    """Napisy graficzne PGS (.sup) -> SRT przez EasyOCR na karcie graficznej."""
    import easyocr
    use_torch_cuda_libs()
    frames = pgs_frames(args.sup)
    if not frames:
        sys.exit(tr("w pliku nie ma plansz napisów PGS"))
    langs = {"ru": ["ru", "en"], "uk": ["uk", "en"]}.get(args.language, ["en"])

    def load():
        print(tr('    OCR (EasyOCR {0}) — wczytuję model...', '+'.join(langs)), flush=True)
        return easyocr.Reader(langs, gpu=True, model_storage_directory=str(MODELS / "easyocr"), verbose=False)

    reader = cached(("ocr", tuple(langs)), load)
    cues = []
    for n, (start, end, img) in enumerate(frames, 1):
        if img.shape[0] < 60:                 # małe litery czyta się lepiej po powiększeniu
            img = np.kron(img, np.ones((2, 2), np.uint8))
        found = reader.readtext(img, detail=1, paragraph=False)
        # wiersze: fragment należy do linii, gdy jego środek leży w jej pasie wysokości; w linii od lewej
        rows = []
        for box, text, _ in sorted(found, key=lambda r: min(p[1] for p in r[0])):
            top, bottom = min(p[1] for p in box), max(p[1] for p in box)
            mid = (top + bottom) / 2
            row = next((r for r in rows if r[0] <= mid <= r[1]), None)
            if row is None:
                rows.append([top, bottom, []])
                row = rows[-1]
            row[2].append((min(p[0] for p in box), text))
        lines = [" ".join(t for _, t in sorted(r[2])) for r in rows]
        text = ocr_cleanup("\n".join(l.strip() for l in lines if l.strip()), args.language)
        if text:
            if cues and cues[-1][2] == text and start - cues[-1][1] < 0.5:
                cues[-1][1] = end or start + 3    # ta sama plansza pokazana ponownie (np. zanikanie)
            else:
                cues.append([start, end or start + 3, text])
        progress(tr("OCR napisów: plansza"), n, len(frames))
    print()
    with open(args.out, "w", encoding="utf-8") as f:
        for i, (start, end, text) in enumerate(cues, 1):
            f.write(f"{i}\n{srt_time(start)} --> {srt_time(max(end, start + 0.5))}\n{text}\n\n")
    print(tr('    OCR: odczytano {0} kwestii z {1} plansz', len(cues), len(frames)))


# ---------------------------------------------------------------- lektor

def read_cues(path):
    """Zwraca listę (start w sekundach, tekst do przeczytania)."""
    cues = []
    text = Path(path).read_text(encoding="utf-8-sig", errors="replace").replace("\r\n", "\n")
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines[:2]):
            m = TIMING_RE.search(line)
            if m:
                h, mi, s, ms = (int(x) for x in m.groups()[:4])
                start = h * 3600 + mi * 60 + s + ms / 10 ** len(m.group(4))
                spoken = clean_for_speech(lines[i + 1:])
                if spoken:
                    cues.append((start, spoken))
                break
    return cues


def clean_for_speech(lines):
    out = []
    for line in lines:
        line = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", line)          # znaczniki formatowania
        line = re.sub(r"[\[(][^\])]*[\])]", "", line)            # opisy dźwięków: [muzyka], (śmiech)
        line = re.sub(r"^\s*[-–—]\s*", "", line).strip(" ♪#")    # myślnik dialogowy
        if line:
            out.append(line)
    text = " ".join(out)
    return text if any(c.isalpha() for c in text) else ""


PIPER_VOICES = ["pl_PL-darkman-medium", "pl_PL-gosia-medium", "pl_PL-mc_speech-medium"]
GIGAAM_FILES = ["config.json", "v3_e2e_rnnt_encoder.onnx", "v3_e2e_rnnt_decoder.onnx", "v3_e2e_rnnt_joint.onnx",
                "v3_e2e_rnnt_vocab.txt"]


def piper_model(voice):
    """Ścieżka do głosu Piper; brakujący głos (model albo jego opis .json) jest pobierany."""
    model = Path(voice) if voice.endswith(".onnx") else MODELS / "piper" / f"{voice}.onnx"
    if not model.exists() or not Path(str(model) + ".json").exists():
        print(tr('    pobieram głos Piper: {0}', voice), flush=True)
        model.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "piper.download_voices", voice, "--data-dir", str(model.parent)],
                       check=True)
    return model


def check(args):
    """Sprawdza każdy składnik audio i dociąga brakujące pliki modeli; kod wyjścia 1, gdy coś nie działa."""
    failed = []

    def step(name, action):
        try:
            detail = action()
            print(tr('    ok    {0}{1}', name, ' — ' + detail if detail else ''), flush=True)
        except Exception as e:
            log_error(f"kontrola: {name}")
            failed.append(name)
            print(tr('    BŁĄD  {0}: {1}: {2}', name, type(e).__name__, str(e)[:200]), flush=True)

    def gpu():
        import torch
        device = use_torch_cuda_libs()
        if device != "cuda":
            raise RuntimeError(tr("PyTorch nie widzi karty graficznej (CUDA)"))
        import onnxruntime
        if "CUDAExecutionProvider" not in onnxruntime.get_available_providers():
            raise RuntimeError(tr("onnxruntime bez obsługi CUDA — GigaAM działałby na procesorze"))
        return torch.cuda.get_device_name(0)

    def whisper():
        from faster_whisper.utils import download_model
        folder = Path(download_model(WHISPER_MODEL))
        missing = [f for f in ("model.bin", "config.json", "tokenizer.json", "vocabulary.json")
                   if not (folder / f).exists()]
        if missing:
            raise FileNotFoundError(tr("brak plików: {0}", ", ".join(missing)))

    def gigaam():
        from huggingface_hub import snapshot_download
        folder = Path(snapshot_download("istupakov/gigaam-v3-onnx", allow_patterns=GIGAAM_FILES))
        snapshot_download("istupakov/silero-vad-onnx", allow_patterns=["config.json", "silero_vad.onnx"])
        missing = [f for f in GIGAAM_FILES if not (folder / f).exists()]
        if missing:
            raise FileNotFoundError(tr("brak plików: {0}", ", ".join(missing)))
        import onnx_asr
        onnx_asr.load_vad("silero", providers=["CPUExecutionProvider"])

    def demucs():
        from demucs.pretrained import get_model
        get_model("htdemucs")

    def piper():
        from piper import PiperVoice
        for voice in PIPER_VOICES:
            PiperVoice.load(str(piper_model(voice)))
        return tr('{0} głosy', len(PIPER_VOICES))

    def xtts():
        from TTS.utils.manage import ModelManager
        folder = Path(ModelManager(progress_bar=False).download_model(XTTS_MODEL)[0] or MODELS / "tts")
        folder = folder if folder.is_dir() else folder.parent
        missing = [f for f in ("model.pth", "config.json", "vocab.json", "speakers_xtts.pth")
                   if not (folder / f).exists() or not (folder / f).stat().st_size]
        if missing:
            raise FileNotFoundError(tr("brak plików: {0}", ", ".join(missing)))

    for name, action in (("karta graficzna", gpu), ("Whisper large-v3", whisper), ("GigaAM v3 + wykrywanie mowy", gigaam),
                         (tr("Demucs (oddzielanie głosu)"), demucs), ("Piper", piper), ("XTTS-v2", xtts)):
        step(name, action)
    if failed:
        sys.exit(1)


class PiperTTS:
    def __init__(self, voice):
        from piper import PiperVoice
        model = piper_model(voice or PIPER_VOICE)
        self.voice = PiperVoice.load(str(model))
        self.rate = self.voice.config.sample_rate

    def speak(self, text):
        chunks = [c.audio_float_array for c in self.voice.synthesize(text)]
        return np.concatenate(chunks) if chunks else np.zeros(0, np.float32)


class XttsTTS:
    rate = 24000

    def __init__(self, voice):
        import torch
        from TTS.api import TTS
        self.torch = torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(tr('    XTTS-v2 ({0}) — wczytuję model...', device), flush=True)
        self.model = TTS(XTTS_MODEL, progress_bar=False).to(device).synthesizer.tts_model
        voice = voice or XTTS_VOICE
        if Path(voice).is_file():                 # próbka głosu do naśladowania
            self.latent, self.embedding = self.model.get_conditioning_latents(audio_path=[voice])
        else:
            speakers = self.model.speaker_manager.speakers
            if voice not in speakers:
                sys.exit(tr("Nieznany głos XTTS '{0}'. Dostępne: {1}", voice, ', '.join(sorted(speakers))))
            self.latent, self.embedding = speakers[voice].values()

    def speak(self, text):
        if text[-1] not in ".!?…":
            text += "."                           # bez kropki XTTS potrafi dopowiadać śmieci na końcu
        # XTTS czasem "halucynuje" dodatkowe dźwięki, zwłaszcza po krótkich kwestiach: gdy nagranie
        # jest podejrzanie długie, generujemy je ponownie i bierzemy najkrótszą wersję
        limit = (0.1 * len(text) + 1.0) * self.rate
        best = None
        for _ in range(3):
            with self.torch.inference_mode():
                out = self.model.inference(text, "pl", self.latent, self.embedding, temperature=0.6,
                                           repetition_penalty=8.0, top_k=40, top_p=0.8,
                                           enable_text_splitting=True)
            wav = out["wav"]
            wav = (wav.cpu().numpy() if hasattr(wav, "cpu") else np.asarray(wav)).astype(np.float32)
            wav = trim_silence(wav, self.rate)
            if best is None or len(wav) < len(best):
                best = wav
            if len(best) <= limit:
                break
        return best


def trim_silence(audio, rate, threshold=0.01):
    loud = np.flatnonzero(np.abs(audio) > threshold)
    if not len(loud):
        return audio[:0]
    pad = int(0.03 * rate)
    return audio[max(0, loud[0] - pad):loud[-1] + pad]


def speed_up(audio, rate, factor):
    res = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                          "-filter:a", f"atempo={factor:.3f}", "-f", "f32le", "pipe:1"],
                         input=audio.astype(np.float32).tobytes(), capture_output=True)
    return np.frombuffer(res.stdout, np.float32) if res.returncode == 0 and res.stdout else audio


def clip_name(args, text):
    """Nazwa pliku z nagraną kwestią — zależy od silnika, głosu i tekstu."""
    key = f"{args.engine}|{args.voice}|{text}".encode("utf-8")
    return hashlib.sha1(key).hexdigest()[:20] + ".npy"


def tts(args):
    cues = read_cues(args.srt)
    if not cues:
        sys.exit(tr("Brak kwestii do przeczytania."))
    engine = cached(("tts", args.engine, args.voice),
                    lambda: XttsTTS(args.voice) if args.engine == "xtts" else PiperTTS(args.voice))
    rate = engine.rate
    # punkt kontrolny: każda wygenerowana kwestia trafia do pliku, więc przerwany lektor
    # (XTTS to nawet ponad godzina na film) po wznowieniu generuje tylko brakujące
    cache = Path(args.cache) if args.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
        ready = sum((cache / clip_name(args, text)).exists() for _, text in cues)
        if ready:
            print(tr('    wznawiam lektora: {0} z {1} kwestii już gotowych', ready, len(cues)), flush=True)
    clips, cursor, sped = [], 0.0, 0
    for i, (start, text) in enumerate(cues):
        saved = cache / clip_name(args, text) if cache else None
        if saved and saved.exists():
            audio = np.load(saved).astype(np.float32) / 32767
        else:
            audio = trim_silence(engine.speak(text), rate)
            if saved:
                tmp = saved.with_suffix(".tmp.npy")
                np.save(tmp, (np.clip(audio, -1, 1) * 32767).astype(np.int16))
                os.replace(tmp, saved)
        begin = max(start, cursor + 0.08)         # nie wchodź na poprzednią kwestię
        if i + 1 < len(cues):
            slot = max(0.4, cues[i + 1][0] - begin)
            ratio = len(audio) / rate / slot
            if ratio > 1.03:
                # opóźniony lektor może mówić szybciej, żeby dogonić obraz
                limit = LAG_SPEEDUP if begin - start > 0.5 else MAX_SPEEDUP
                audio = speed_up(audio, rate, min(limit, ratio))
                sped += 1
        clips.append((int(begin * rate), audio))
        cursor = begin + len(audio) / rate
        progress(tr("lektor: kwestia"), i + 1, len(cues))
    print()
    track = np.zeros(max(pos + len(a) for pos, a in clips) + rate, np.float32)
    for pos, audio in clips:
        track[pos:pos + len(audio)] += audio
    peak = float(np.abs(track).max()) or 1.0
    pcm = (track / peak * 0.9 * 32767).astype(np.int16)
    with wave.open(args.out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    print(tr('    lektor: {0} kwestii, przyspieszono {1}', len(cues), sped))


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("transcribe")
    t.add_argument("audio")
    t.add_argument("out")
    t.add_argument("--engine", choices=["whisper", "gigaam"], default="whisper")
    t.add_argument("--model", default=WHISPER_MODEL, help=tr('model Whisper'))
    t.add_argument("--language", default="ru", help=tr('język mowy (ru, en, uk...); GigaAM obsługuje tylko ru'))
    t.add_argument("--separate", action="store_true",
                   help=tr('najpierw oddziel głos od muzyki (wejście: WAV stereo 44,1 kHz)'))
    s = sub.add_parser("tts")
    s.add_argument("srt")
    s.add_argument("out")
    s.add_argument("--engine", choices=["piper", "xtts"], default="piper")
    s.add_argument("--voice", help=tr('Piper: nazwa głosu; XTTS: nazwa wbudowanego głosu albo plik WAV z próbką'))
    s.add_argument("--cache", help=tr('folder na gotowe kwestie — pozwala wznowić przerwane generowanie'))
    o = sub.add_parser("ocr")
    o.add_argument("sup")
    o.add_argument("out")
    o.add_argument("--language", default="en", help=tr('język napisów: ru, uk albo en'))
    d = sub.add_parser("detect")
    d.add_argument("audio")
    d.add_argument("out")
    d.add_argument("--model", default=WHISPER_MODEL)
    sub.add_parser("check")
    sub.add_parser("serve", help=tr('praca ciągła: polecenia (JSON, po jednym w wierszu) czytane z wejścia, modele zostają w pamięci między plikami'))
    args = ap.parse_args()
    if args.cmd == "serve":
        return serve(ap)
    try:
        {"transcribe": transcribe, "tts": tts, "check": check, "detect": detect, "ocr": ocr}[args.cmd](args)
    except Exception:
        log_error(args.cmd)
        raise


def serve(ap):
    """Wykonuje kolejne polecenia transcribe/tts bez ponownego wczytywania modeli (oszczędza ~40 s na plik).

    Po każdym poleceniu wypisuje wiersz END_MARK z kodem 0 (sukces) albo 1 i opisem błędu.
    """
    for line in sys.stdin:
        if not line.strip():
            continue
        code, message = 0, ""
        try:
            argv = json.loads(line)
            sys.argv = [sys.argv[0], *argv]   # do opisu w audio_blad.txt
            args = ap.parse_args(argv)
            if args.cmd not in ("transcribe", "tts", "detect", "ocr"):
                raise ValueError(tr('polecenie {0} nie działa w trybie serve', args.cmd))
            {"transcribe": transcribe, "tts": tts, "detect": detect, "ocr": ocr}[args.cmd](args)
        except SystemExit as e:               # sys.exit("opis") w poleceniach i błędy argparse
            if e.code not in (None, 0):
                code, message = 1, str(e.code)
        except OSError as e:
            try:                              # program nadrzędny zamknięty (np. Stop) — wyjście jest martwe
                sys.stdout.write("\n")
                sys.stdout.flush()
            except OSError:
                return                        # kończymy po cichu, bez wpisu w audio_blad.txt
            log_error(line.strip()[:60])
            code, message = 1, f"{type(e).__name__}: {e}"
        except Exception as e:
            log_error(line.strip()[:60])
            traceback.print_exc()
            code, message = 1, f"{type(e).__name__}: {e}"
        message = " ".join(message.split())
        print(f"\n{END_MARK} {code} {message}", flush=True)


if __name__ == "__main__":
    main()
