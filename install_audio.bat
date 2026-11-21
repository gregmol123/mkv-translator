@echo off
rem Instaluje srodowisko audio_env: rozpoznawanie mowy (GigaAM, Whisper), mowcy (pyannote), ocena tlumaczen
rem (MetricX), OCR napisow i lektor (Piper, XTTS-v2). Wymaga karty NVIDIA oraz narzedzia uv (https://docs.astral.sh/uv/).
rem Pobiera ok. 8 GB, a modele (kolejne ok. 20 GB) sciagaja sie przy pierwszym uzyciu do folderu audio_models.
rem Wersje pakietow sa przypiete w requirements-audio.lock (sprawdzony zestaw) - nowa instalacja dostaje dokladnie te same.
rem XTTS-v2 jest na licencji Coqui CPML - wylacznie do uzytku niekomercyjnego.
cd /d "%~dp0"
where uv >nul 2>nul || (echo Brak programu uv - zainstaluj: winget install astral-sh.uv & pause & exit /b 1)
uv venv audio_env --python 3.12 || (pause & exit /b 1)
rem torch z CUDA 12.8 pochodzi z indeksu PyTorcha, reszta z PyPI
uv pip install --system-certs --python audio_env\Scripts\python.exe --no-deps -r requirements-audio.lock --extra-index-url https://download.pytorch.org/whl/cu128 --index-strategy unsafe-best-match || (pause & exit /b 1)
audio_env\Scripts\python.exe audio_tools.py check
echo.
echo Gotowe.
pause
