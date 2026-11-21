#!/usr/bin/env python3
"""Porównanie modeli tłumaczenia (C6) bez wzorca: te same kwestie z rosyjskich napisów, ocena MetricX-24.

Z każdego filmu bierze N kwestii ze środka napisów, tłumaczy je każdym modelem (tak jak program, z kontekstem,
słowniczkiem nie), potem ocenia wszystkie tłumaczenia MetricX-24 (0 = idealnie, 25 = źle) i liczy usterki
z kontroli jakości programu. Wynik: raport w reports/ (krótkie nazwy filmów) i przykłady obok siebie.

Przykład:
  python tools/mt_bench.py film1.mkv film2.mkv --models gemma4:31b,hf.co/mradermacher/MiLMMT-46-12B-v0.1-GGUF:Q8_0
"""
import argparse, sys, tempfile, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import translate_mkv as t                    # noqa: E402
from asr_bench import short_name             # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("films", nargs="+")
    ap.add_argument("--models", required=True, help="modele Ollamy po przecinku")
    ap.add_argument("--cues", type=int, default=120, help="ile kwestii z każdego filmu")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "reports"))
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",")]
    sources = []
    with tempfile.TemporaryDirectory() as tmp:
        for film in map(Path, args.films):
            cues, lang = t.pick_source_cues(film, t.subtitle_streams(film), None, tmp, "ru")
            mid = max(0, len(cues) // 2 - args.cues // 2)
            sources.append((short_name(film.name), cues[mid:mid + args.cues], lang))
    results = {}
    for model in models:
        engine = t.OllamaEngine(model)
        engine.parallel = 2
        outs, took = [], 0.0
        for name, cues, lang in sources:
            engine.lang = lang
            t0 = time.time()
            outs.append(t.translate_cues(cues, engine))
            took += time.time() - t0
        pairs = [(a.text, b.text) for (_, cues, _), res in zip(sources, outs) for a, b in zip(cues, res)]
        issues = sum(bool(t.segment_issues(a, b, "ru")) for a, b in pairs)
        scores = t.qe_score(pairs) or []
        results[model] = {"qe": sum(scores) / max(1, len(scores)), "bad": sum(s >= t.QE_BAD for s in scores),
                          "issues": issues, "seconds": took, "texts": [b for _, b in pairs], "scores": scores}
        print(f"== {model}: MetricX {results[model]['qe']:.2f}, słabych {results[model]['bad']}, "
              f"usterek {issues}, {took:.0f} s", flush=True)
    t.stop_helper()
    src_texts = [c.text for _, cues, _ in sources for c in cues]
    stamp = time.strftime("%Y-%m-%d %H%M")
    lines = [f"# Porównanie modeli tłumaczenia {stamp}", "",
             f"Filmy: {', '.join(n for n, _, _ in sources)}; po {args.cues} kwestii; ocena MetricX-24 bez wzorca "
             "(0 = idealnie, 25 = źle).", "", "| Model | MetricX | Słabe (≥ 8) | Usterki | Czas (s) |", "|---|---|---|---|---|"]
    lines += [f"| {m} | {r['qe']:.2f} | {r['bad']} | {r['issues']} | {r['seconds']:.0f} |" for m, r in results.items()]
    lines += ["", "## Przykłady (co 15. kwestia)", ""]
    for i in range(0, len(src_texts), 15):
        lines.append(f"- RU: {src_texts[i].replace(chr(10), ' / ')}")
        for m, r in results.items():
            if i < len(r["texts"]):
                sc = r["scores"][i] if i < len(r["scores"]) else float("nan")
                lines.append(f"  - {m.split('/')[-1]} ({sc:.1f}): {r['texts'][i].replace(chr(10), ' / ')}")
    out = Path(args.out) / f"mt_bench {stamp}.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[4:6 + len(results)]))
    print(f"Raport: {out}")


if __name__ == "__main__":
    main()
