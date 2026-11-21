"""Test całego przebiegu na 30-sekundowym MKV: napisy RU -> tłumaczenie (zastępczy silnik) -> polska ścieżka w pliku.

Wymaga ffmpeg w PATH; bez Ollamy i karty graficznej (MKV_QE=0, silnik tłumaczenia zastępczy)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import translate_mkv as t                    # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="brak ffmpeg")


class FakeEngine:
    name, batch_size, parallel, lang = "zastępczy", 30, 1, "ru"

    def translate(self, texts, context=()):
        return [f"Polska kwestia numer {i}, tak jest." for i, _ in enumerate(texts)]


def make_mkv(folder):
    srt = folder / "ru.srt"
    srt.write_text("".join(f"{i}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i + 1:02d},500\nРусская реплика номер {i}, да.\n\n"
                           for i in range(1, 12)), encoding="utf-8")
    forced = folder / "forced.srt"
    forced.write_text("1\n00:00:05,000 --> 00:00:06,000\nНадпись\n\n", encoding="utf-8")
    mkv = folder / "Serial S01E01.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=d=30:s=320x240", "-f", "lavfi",
                    "-i", "sine=d=30", "-i", str(srt), "-i", str(forced), "-map", "0", "-map", "1", "-map", "2", "-map", "3",
                    "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt", "-metadata:s:s:0", "language=rus",
                    "-metadata:s:s:1", "language=rus", "-disposition:s:0", "default", "-disposition:s:1", "forced",
                    str(mkv)], check=True)
    return mkv


def test_full_pass_embeds_polish_and_keeps_forced(tmp_path, monkeypatch):
    monkeypatch.setenv("MKV_QE", "0")
    monkeypatch.setattr(t, "APP_DIR", tmp_path)              # raport jakości i dziennik w folderze testu
    monkeypatch.setattr(t, "QUALITY_REPORT", tmp_path / "raport.csv", raising=False)
    mkv = make_mkv(tmp_path)
    args = argparse.Namespace(mux=True, force=False, track=None, replace=True, backup_days=0, whisper=False,
                              asr="gigaam", separate=False, diarize=False, lektor=None, voice=None, replace_audio=False,
                              source="auto", target="pl", whisper_model=None, resume=False, restart=False)
    state = t.process(mkv, args, FakeEngine())
    assert state in ("Gotowe", "Do sprawdzenia")
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                                         "stream=index:stream_disposition=default,forced:stream_tags=language",
                                         "-of", "json", str(mkv)], capture_output=True, text=True).stdout)["streams"]
    langs = [s["tags"].get("language") for s in streams]
    assert langs == ["rus", "rus", "pol"]
    assert streams[1]["disposition"]["forced"] == 1               # flaga "wymuszone" przetrwała podmianę
    assert streams[2]["disposition"]["default"] == 1 and streams[0]["disposition"]["default"] == 0
    assert not list(tmp_path.glob("*.pl.srt")) and not list(tmp_path.glob("*.tmp*"))   # bez śmieci obok filmu
    # drugi przebieg: plik ma już polskie napisy
    assert t.process(mkv, args, FakeEngine()) == "Ma polskie"
