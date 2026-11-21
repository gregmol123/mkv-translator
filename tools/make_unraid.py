r"""Kopiuje aktualny program do folderu unraid/ (i opcjonalnie na udział serwera): python tools/make_unraid.py [\\SERWER\appdata\mkv-translator]"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["translate_mkv.py", "ui_en.py", "unraid/Dockerfile", "unraid/zainstaluj.sh", "unraid/ustawienia.sh"]


def main():
    for name in ("translate_mkv.py", "ui_en.py"):
        shutil.copy2(ROOT / name, ROOT / "unraid" / name)
        print("unraid/" + name)
    if len(sys.argv) > 1:
        dest = Path(sys.argv[1])
        if not dest.exists():
            sys.exit(f"brak udziału {dest} — serwer wyłączony?")
        for name in FILES:
            src = ROOT / name
            target = dest / Path(name).name
            if target.name == "ustawienia.sh" and target.exists():
                continue                      # nie nadpisuj ustawień użytkownika
            shutil.copy2(src, target)
            print("->", target)


if __name__ == "__main__":
    main()
