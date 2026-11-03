@echo off
rem Instaluje srodowisko audio_env: Whisper (napisy z dzwieku), Piper i XTTS-v2 (lektor).
rem Wymaga karty NVIDIA oraz narzedzia uv (https://docs.astral.sh/uv/). Pobiera ok. 6 GB,
rem a modele (kolejne ok. 5 GB) sciagaja sie przy pierwszym uzyciu do folderu audio_models.
rem XTTS-v2 jest na licencji Coqui CPML - wylacznie do uzytku niekomercyjnego.
cd /d "%~dp0"
where uv >nul 2>nul || (echo Brak programu uv - zainstaluj: winget install astral-sh.uv & pause & exit /b 1)
uv venv audio_env --python 3.12 || (pause & exit /b 1)
uv pip install --system-certs --python audio_env\Scripts\python.exe torch torchaudio --index-url https://download.pytorch.org/whl/cu128 || (pause & exit /b 1)
uv pip install --system-certs --python audio_env\Scripts\python.exe "coqui-tts[codec]" "transformers>=4.57,<5" faster-whisper piper-tts truststore demucs "onnx-asr[hub]" || (pause & exit /b 1)
rem GigaAM na karcie graficznej: onnxruntime w wersji GPU zgodnej z bibliotekami CUDA 12 z PyTorcha
uv pip uninstall --python audio_env\Scripts\python.exe onnxruntime
uv pip install --system-certs --python audio_env\Scripts\python.exe "onnxruntime-gpu<1.24" || (pause & exit /b 1)
echo.
echo Gotowe.
pause
