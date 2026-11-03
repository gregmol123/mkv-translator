# MKV Translator na Unraid: tylko ffmpeg i Python — tłumaczy Ollama na komputerze z kartą graficzną.
# Program (translate_mkv.py) leży w /config (appdata), więc aktualizacja = podmiana pliku i restart kontenera.
# wersja Debiana przypięta: kolejne budowanie nie zmieni po cichu wersji ffmpeg
FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1 \
    MKV_STATE=/config/zrobione.json \
    MKV_PLEX_LOG=/config/plex_nazwy.json
WORKDIR /config
# umask 000: nowe pliki są do zapisu dla wszystkich (jak w udziałach Unraid)
ENTRYPOINT ["sh", "-c", "umask 000; exec python /config/translate_mkv.py \"$@\"", "--"]
