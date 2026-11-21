"""Testy czystych funkcji audio_tools.py (podział na kwestie, filtr widm) — bez modeli i karty graficznej."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
np = pytest.importorskip("numpy")
sys.argv = sys.argv[:1]
import audio_tools as a                      # noqa: E402


def words(text, step=0.35):
    return [a.Word(" " + w, i * step, i * step + 0.3) for i, w in enumerate(text.split())]


def test_split_segment_prefers_sentence_end():
    text = ("Я не знаю, что тут происходит. Но мы должны срочно уехать отсюда, пока они не вернулись "
            "и не нашли нас всех здесь, понимаешь?")
    cues = a.split_segment(words(text))
    assert cues[0][2] == "Я не знаю, что тут происходит."
    assert all(len(c[2]) <= a.MAX_CUE_CHARS for c in cues)
    assert all(len(c[2].split()) >= 2 for c in cues)          # bez jednowyrazowych resztek


def test_split_segment_long_pause():
    ws = words("Раз два три") + [a.Word(" четыре", 5.0, 5.3)]
    assert [c[2] for c in a.split_segment(ws)] == ["Раз два три", "четыре"]


def test_drop_hallucinations():
    cues = [(0, 1, "Привет, как дела?", 0.9), (1, 2, "Субтитры сделал DimaTorzok", 0.9),
            (2, 3, "Да.", 0.9), (3, 4, "Да.", 0.9), (4, 5, "Да.", 0.9),
            (5, 6, "Я не знаю, что делать дальше", 0.9), (6, 7, "Я не знаю, что делать дальше", 0.9),
            (7, 8, "Я не знаю, что делать дальше", 0.9), (8, 9, "Thanks for watching!", 0.9), (9, 10, "шум", 0.1)]
    out, dropped = a.drop_hallucinations(cues)
    assert dropped == 4
    assert [c[2] for c in out].count("Да.") == 3                 # krótkie powtórki to dialog


def test_finish_cues_timing():
    out = a.finish_cues([(0, 1, "Да.", None), (1.1, 1.3, "Нет.", None), (5, 5.2, "Ага?", None), (5.3, 6, "Конечно.", None)])
    assert out[0][2] == "Да. Нет."                               # sklejone okruchy
    assert out[1][2] == "Ага?" and out[2][2] == "Конечно."        # po pytaniu bez sklejania
    assert out[1][1] <= out[2][0] - a.CUE_GAP + 1e-6             # przerwa przed następną kwestią


def test_dialog_merge():
    cues = [(0, 1, "Ты где?", None), (1.2, 2, "Дома.", None), (5, 6, "Иду.", None)]
    out, spk = a.dialog_merge(cues, ["A", "B", "B"])
    assert out[0][2] == "- Ты где?\n- Дома." and spk[0] == ["A", "B"] and len(out) == 2
