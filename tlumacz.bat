@echo off
rem Przeciagnij pliki MKV lub foldery na ten plik, aby przetlumaczyc napisy RU -> PL.
chcp 65001 >nul
if "%~1"=="" (
    echo Przeciagnij pliki MKV lub folder na ten plik .bat
    pause
    exit /b
)
python "%~dp0translate_mkv.py" %*
pause
