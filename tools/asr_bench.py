#!/usr/bin/env python3
"""Pomiar rozpoznawania mowy (B1): rosyjskie napisy wbudowane w film służą za wzorzec.

Z każdego filmu wycina kilka fragmentów, rozpoznaje mowę wybranymi ustawieniami (te same polecenia co program:
audio_tools.py transcribe) i porównuje z napisami z pliku:
  WER / CER  — błąd słów / znaków po normalizacji (małe litery, ё→е, bez interpunkcji),
  czas       — mediana różnicy początku kwestii rozpoznanej i najbliższej kwestii wzorca (ms),
  widma      — kwestie rozpoznane tam, gdzie wzorzec nie ma żadnej kwestii (± 1 s) — kandydaci na halucynacje.

Napisy filmowe bywają skrócone względem mowy, więc WER nie spada do zera — liczy się porównanie ustawień
na tych samych fragmentach. Wymaga numpy.

Przykład:
  python tools/asr_bench.py film1.mkv film2.mkv --clips 3 --length 240 --configs gigaam,gigaam+demucs,whisper
"""
import argparse, json, re, sys, tempfile, time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import translate_mkv as t                    # noqa: E402

CONFIGS = {                                  # nazwa -> (silnik, oddzielanie głosu, dodatkowe opcje transcribe)
    "gigaam": ("gigaam", False, []), "gigaam+demucs": ("gigaam", True, []),
    "whisper": ("whisper", False, []), "whisper+demucs": ("whisper", True, []),
    # B8: GigaAM na głosie z Demucsa + Whisper na miksie, rozbieżności rozstrzyga model językowy (--llm)
    "gigaam+whisper": ("ensemble", True, []),
    # B2: inne warianty GigaAM (bez interpunkcji / wielojęzyczny)
    "gigaam-rnnt+demucs": ("gigaam", True, ["--gigaam-model", "gigaam-v3-rnnt"]),
    "gigaam-ml+demucs": ("gigaam", True, ["--gigaam-model", "gigaam-multilingual-large-ctc"]),
    # B3: cięcie mowy w dłuższej ciszy i dłuższe fragmenty; B5: Silero VAD v6
    "gigaam-b3+demucs": ("gigaam", True, ["--vad-silence-ms", "600", "--vad-max-s", "27"]),
    "gigaam-v6+demucs": ("gigaam", True, ["--vad", "silero6"]),
    "gigaam-b3v6+demucs": ("gigaam", True, ["--vad", "silero6", "--vad-silence-ms", "600", "--vad-max-s", "27"]),
    # B6: separacja BS-RoFormer zamiast htdemucs; z domieszką oryginału 0,3
    "gigaam+roformer": ("gigaam", True, ["--sep", "roformer"]),
    "gigaam+roformer-mix": ("gigaam", True, ["--sep", "roformer", "--sep-mix", "0.3"]),
    "gigaam+demucs-mix": ("gigaam", True, ["--sep-mix", "0.3"]),
    # B9: RNNT + interpunkcja z modelu językowego (WER bez interpunkcji — sprawdza, że słowa się nie zmieniły)
    "gigaam-rnnt+punct": ("gigaam", True, ["--gigaam-model", "gigaam-v3-rnnt"]),
    # B10: czasy kwestii z wymuszonego wyrównania
    "gigaam+demucs+align": ("gigaam", True, ["--align"]),
    "whisper+align": ("whisper", False, ["--align"]),
    # B11: Whisper w partiach; Parakeet TDT v3
    "whisper-batch": ("whisper", False, ["--whisper-batch", "16"]),
    "parakeet": ("parakeet", False, []),
    "parakeet+demucs": ("parakeet", True, []),
}


def norm(text):
    text = t.TAG_RE.sub(" ", text.lower().replace("ё", "е"))
    return " ".join(re.findall(r"[\w']+", text))


def edit_distance(a, b):
    """Levenshtein dla list (słowa) albo napisów (znaki); wiersz liczony wektorowo."""
    if not a or not b:
        return max(len(a), len(b))
    codes = {x: i for i, x in enumerate(set(a) | set(b))}
    av, bv = np.array([codes[x] for x in a]), np.array([codes[x] for x in b])
    prev = np.arange(len(bv) + 1)
    idx = np.arange(len(bv) + 1)
    for i, x in enumerate(av, 1):
        cand = np.empty_like(prev)
        cand[0] = i
        cand[1:] = np.minimum(prev[1:] + 1, prev[:-1] + (bv != x))
        prev = np.minimum.accumulate(cand - idx) + idx      # wstawienia w obrębie wiersza
    return int(prev[-1])


def seconds(stamp):
    h, m, s = stamp.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def clip_cues(cues, start, length):
    """Kwestie wzorca w oknie [start, start+length), z czasem względem początku fragmentu."""
    out = []
    for c in cues:
        a, b = seconds(c.start), seconds(c.end)
        if a >= start and b <= start + length:
            out.append((a - start, b - start, c.text))
    return out


def extract(mkv, audio, start, length, separate, wav):
    src, _ = t.main_audio(audio, "ru")
    surround = audio[src].get("channels", 2) >= 6
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(length), "-i", str(mkv), "-map", f"0:a:{src}"]
    if surround:
        cmd += ["-af", "pan=stereo|c0=FC|c1=FC" if separate else "pan=mono|c0=FC"]
    cmd += ["-ar", "44100", "-ac", "2"] if separate else ["-ar", "16000", "-ac", "1"]
    res = t.run(cmd + [str(wav)])
    if res.returncode != 0:
        raise t.FatalError(res.stderr.strip())


def score(ref, hyp):
    ref_words, hyp_words = norm(" ".join(x[2] for x in ref)).split(), norm(" ".join(x[2] for x in hyp)).split()
    ref_chars, hyp_chars = " ".join(ref_words), " ".join(hyp_words)
    starts = [r[0] for r in ref]
    shifts, ghosts = [], 0
    for a, b, _ in hyp:
        if not any(ra - 1 <= b and a <= rb + 1 for ra, rb, _ in ref):
            ghosts += 1
        elif starts:
            shifts.append(min(abs(a - s) for s in starts))
    return {"wer": edit_distance(ref_words, hyp_words) / max(1, len(ref_words)),
            "cer": edit_distance(ref_chars, hyp_chars) / max(1, len(ref_chars)),
            "shift_ms": round(1000 * float(np.median(shifts))) if shifts else None,
            "ghosts": ghosts, "ref_cues": len(ref), "hyp_cues": len(hyp), "ref_words": len(ref_words),
            # kwestie łamiące normy napisów (B4): za długie (>84 zn. albo >7 s) i za szybkie do czytania (>20 zn./s)
            "too_long": sum(len(x) > 84 or b - a > 7.05 for a, b, x in hyp),
            "too_fast": sum(len(x) / max(0.1, b - a) > 20 for a, b, x in hyp)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("films", nargs="+", help="filmy z rosyjskimi napisami tekstowymi i rosyjską mową")
    ap.add_argument("--clips", type=int, default=2, help="ile fragmentów z każdego filmu (rozłożone 20–80%%)")
    ap.add_argument("--length", type=int, default=240, help="długość fragmentu w sekundach")
    ap.add_argument("--configs", default="gigaam,gigaam+demucs,whisper", help=", ".join(CONFIGS))
    ap.add_argument("--llm", default=t.OLLAMA_MODEL, help="model Ollamy rozstrzygający rozbieżności (gigaam+whisper)")
    ap.add_argument("--ref", help="plik SRT ze wzorcem zamiast napisów z filmu (tylko przy jednym filmie)")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "reports"),
                    help="folder na raport (JSON i Markdown)")
    args = ap.parse_args()
    configs = [c.strip() for c in args.configs.split(",")]
    results, llm = [], None
    with tempfile.TemporaryDirectory() as tmp:
        for film in map(Path, args.films):
            if args.ref:                      # wzorzec z osobnego pliku (np. ręcznie poprawiony zapis mowy)
                ref_all = t.parse_srt(t.read_text_any(Path(args.ref)))
            else:
                ref_all, _ = t.pick_source_cues(film, t.subtitle_streams(film), None, tmp, "ru")
            audio = t.probe_streams(film, "a")
            total = t.duration(film) or 0
            for k in range(args.clips):
                start = int(total * (0.2 + 0.6 * k / max(1, args.clips - 1)))
                ref = clip_cues(ref_all, start, args.length)
                if len(ref) < 10:
                    print(f"{film.name} @ {start} s: za mało kwestii wzorca ({len(ref)}) — pomijam")
                    continue
                for name in configs:
                    engine, separate, extra = CONFIGS[name]
                    wav, out = Path(tmp) / "clip.wav", Path(tmp) / "hyp.srt"
                    extract(film, audio, start, args.length, separate, wav)
                    t0 = time.time()
                    if engine == "ensemble":
                        t.ollama_unload()     # cała karta dla modeli mowy, jak w programie
                    t.run_helper(["transcribe", wav, out, "--engine", engine, "--language", "ru"]
                                 + (["--separate"] if separate else [])
                                 + extra)
                    if name == "gigaam-rnnt+punct":
                        t.free_helper()
                        llm = llm or t.OllamaEngine(args.llm)
                        t.punctuate_asr(out, llm)
                        print("    próbka:", " | ".join(c.text for c in t.parse_srt(out.read_text(encoding="utf-8-sig"))[5:8]))
                    if engine == "ensemble":
                        raw = [(seconds(c.start), seconds(c.end), c.text)
                               for c in t.parse_srt(out.read_text(encoding="utf-8"))]
                        print(f"    przed rozstrzyganiem: WER {score(ref, raw)['wer']:.1%}", flush=True)
                        t.free_helper()
                        llm = llm or t.OllamaEngine(args.llm)
                        t.resolve_asr_variants(out, llm)
                    took = time.time() - t0
                    hyp = [(seconds(c.start), seconds(c.end), c.text) for c in t.parse_srt(out.read_text(encoding="utf-8"))]
                    row = {"film": film.name, "start": start, "config": name, "seconds": round(took, 1), **score(ref, hyp)}
                    results.append(row)
                    print(f"{film.name[:40]:40} @{start:>5}s {name:15} WER {row['wer']:.1%}  CER {row['cer']:.1%}  "
                          f"przesunięcie {row['shift_ms']} ms  widma {row['ghosts']}  za długie {row['too_long']}  "
                          f"za szybkie {row['too_fast']}  ({took:.0f} s)", flush=True)
    t.stop_helper()
    if not results:
        return 1
    stamp = time.strftime("%Y-%m-%d %H%M")
    out_dir = Path(args.out)
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"asr_bench {stamp}.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Pomiar rozpoznawania mowy {stamp}", "",
             f"Fragmenty: {args.clips} × {args.length} s z każdego filmu; wzorzec: rosyjskie napisy z pliku.", "",
             "| Ustawienie | WER | CER | Przesunięcie (ms) | Widma | Za długie | Za szybkie | Czas (s) |",
             "|---|---|---|---|---|---|---|---|"]
    for name in configs:
        rows = [r for r in results if r["config"] == name]
        if not rows:
            continue
        words = sum(r["ref_words"] for r in rows)
        wer = sum(r["wer"] * r["ref_words"] for r in rows) / max(1, words)
        cer = sum(r["cer"] * r["ref_words"] for r in rows) / max(1, words)
        shifts = [r["shift_ms"] for r in rows if r["shift_ms"] is not None]
        lines.append(f"| {name} | {wer:.1%} | {cer:.1%} | {int(np.median(shifts)) if shifts else '–'} | "
                     f"{sum(r['ghosts'] for r in rows)} | {sum(r.get('too_long', 0) for r in rows)} | "
                     f"{sum(r.get('too_fast', 0) for r in rows)} | {sum(r['seconds'] for r in rows):.0f} |")
    lines += ["", "| Film | Od (s) | Ustawienie | WER | CER | Przesunięcie | Widma |", "|---|---|---|---|---|---|---|"]
    lines += [f"| {r['film']} | {r['start']} | {r['config']} | {r['wer']:.1%} | {r['cer']:.1%} | {r['shift_ms']} | {r['ghosts']} |"
              for r in results]
    report = out_dir / f"asr_bench {stamp}.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[4:4 + len(configs) + 2]))
    print(f"\nRaport: {report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
