"""Testy czystych funkcji programu (bez Ollamy i karty graficznej): python -m pytest tests"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
import translate_mkv as t                    # noqa: E402


def cue(text, start="00:00:01,000", end="00:00:03,000"):
    return t.Cue(start, end, "", text)


# ---------------------------------------------------------------- SRT

def test_srt_roundtrip(tmp_path):
    cues = [cue("Привет!"), cue("- Да?\n- Нет.", "00:00:04,000", "00:00:06,500")]
    path = tmp_path / "a.srt"
    t.write_srt(cues, path)
    assert not (tmp_path / "a.srt.tmp").exists()          # zapis atomowy
    back = t.parse_srt(path.read_text(encoding="utf-8-sig"))
    assert [(c.start, c.end, c.text) for c in back] == [(c.start, c.end, c.text) for c in cues]


def test_parse_srt_skips_empty_and_bom():
    text = "﻿1\n00:00:01,000 --> 00:00:02,000\nРаз\n\n2\n00:00:03,000 --> 00:00:04,000\n\n"
    assert [c.text for c in t.parse_srt(text)] == ["Раз"]


def test_split_cue_dialog():
    prefix, italic, segs = t.split_cue(cue("- Ты где?\n- Дома."))
    assert segs == ["- Ты где?", "- Дома."] and not italic


def test_wrap_two_lines():
    text = "To jest dość długa kwestia, którą trzeba złamać na dwie linie"
    lines = t.wrap(text).split("\n")
    assert len(lines) == 2 and all(len(line) <= t.LINE_WIDTH + 5 for line in lines)


# ---------------------------------------------------------------- normy i zdania

def test_polish_norms():
    out, changed = t.polish_norms([cue('Mówił "jutro" o 14:30...'), cue("— Idziesz?\n— Nie."), cue("Wynik 2:1")])
    assert [c.text for c in out] == ["Mówił „jutro” o 14.30…", "- Idziesz?\n- Nie.", "Wynik 2:1"]
    assert changed == 2


def test_sentence_groups_and_split():
    cs = [cue("Мы должны срочно уехать отсюда,", "00:00:01,000", "00:00:02,000"),
          cue("пока они не вернулись.", "00:00:02,100", "00:00:04,000"),
          cue("- Да?\n- Нет.", "00:00:04,200", "00:00:05,000")]
    assert t.sentence_groups(cs) == [[0, 1], [2]]
    parts = t.split_translation("Musimy natychmiast stąd wyjechać, zanim oni wrócą.", [31, 22])
    assert parts == ["Musimy natychmiast stąd wyjechać,", "zanim oni wrócą."]


def test_split_translation_short_text():
    assert t.split_translation("Tak.", [10, 10]) == ["Tak.", ""]


# ---------------------------------------------------------------- jakość i języki

def test_detect_lang():
    ru = ["Я не знаю, что делать дальше, понимаешь меня?"] * 5
    en = ["I don't know what to do, you know what I mean?"] * 5
    assert t.detect_lang(ru) == "ru" and t.detect_lang(en) == "en"


def test_segment_issues_untranslated():
    assert t.segment_issues("Где капитан Тенишев?", "Где капитан Тенишев?", "ru")   # zostało po rosyjsku
    assert not t.segment_issues("Где капитан?", "Gdzie jest kapitan?", "ru")


def test_json_payload_and_reply():
    payload = json.loads(t.json_payload(["Раз", "Два"], {"kontekst": ["Ноль"], "dalej": [], "plec": {"1": "K"}}))
    assert payload["kwestie"] == {"1": "Раз", "2": "Два"} and payload["plec"] == {"1": "K"} and "dalej" not in payload
    assert t.parse_json_reply('```json\n{"1": "Raz", "2": "Dwa"}\n```', 2) == ["Raz", "Dwa"]


def test_tr_english():
    t.set_ui_lang("en")
    try:
        assert t.tr("    zapisano {0}", "a.srt") == "    saved a.srt"
    finally:
        t.set_ui_lang("pl")
    assert t.tr("    zapisano {0}", "a.srt") == "    zapisano a.srt"


def test_untr_restricted_to_choices():
    t.set_ui_lang("en")
    try:
        assert t.untr("Polish", t.TARGET_CHOICES) == "polski"
        assert t.untr("English", t.TARGET_CHOICES) == "angielski"
    finally:
        t.set_ui_lang("pl")


def test_target_language_paths():
    t.OUT["code"] = "en"
    try:
        assert t.source_order("auto") == ["ru", "uk"] and "en" not in t.source_order("auto")
        assert t.sub_output(Path("odcinek_rus.srt")).name == "odcinek.en.srt"
    finally:
        t.OUT["code"] = "pl"
    assert t.sub_output(Path("odcinek_rus.srt")).name == "odcinek.pl.srt"


def test_glossary_prompt_names(tmp_path):
    path = tmp_path / "slowniczek.json"
    path.write_text(json.dumps({"imiona": {
        "Серге": {"pisownia": "Siergiej", "warianty": {"Siergiej": 5}, "zablokuj": False, "plec": "M"},
        "Арин": {"pisownia": "Arina", "warianty": {"Arina": 1}, "zablokuj": False}}}), encoding="utf-8")
    assert t.glossary_prompt_names(path) == {"Серге": "Siergiej (M)"}    # pojedyncze wystąpienie nie trafia


def test_short_name():
    from asr_bench import short_name
    assert short_name("Serial.S01.E01.2024.Grupa.mkv") == "Serial S01E01"


def test_transcribe_info_is_per_file(tmp_path):
    """D3: ścieżka audio z rozpoznania trafia do słownika pliku, nie do wspólnego stanu."""
    import argparse
    mkv = tmp_path / "a.mkv"
    srt = tmp_path / "a.rozpoznane.ru.srt"
    t.write_srt([cue("Привет, как дела?")], srt)
    (tmp_path / "a.rozpoznane.ru.json").write_text(json.dumps({"asr": "gigaam", "lang": "ru", "audio": 2}), encoding="utf-8")
    args = argparse.Namespace(resume=False, force=False, asr="gigaam", source="auto", diarize=False)
    first, second = {}, {}
    t.transcribe(mkv, [], args, str(tmp_path), info=first)
    assert first.get("audio_index") == 2 and second == {}
    assert not hasattr(t, "ASR_INFO")
