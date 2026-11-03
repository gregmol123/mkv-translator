#!/bin/bash
# Instaluje (albo po zmianie ustawień uruchamia ponownie) kontener MKV Translatora.
# Uruchom w terminalu Unraid, np.:  bash /mnt/user/appdata/mkv-translator/zainstaluj.sh
set -e
DIR="$(dirname "$(readlink -f "$0")")"        # folder, w którym leży ten skrypt (zwykle appdata/mkv-translator)
source "$DIR/ustawienia.sh"

echo "== budowanie obrazu (pierwszy raz kilka minut)"
mkdir -p "$DIR/pusty"                         # obraz nie potrzebuje żadnych plików (program jest w /config)
docker build -t mkv-translator -f "$DIR/Dockerfile" "$DIR/pusty"

echo "== test połączenia z Ollamą: $OLLAMA_URL"
if curl -s -m 5 "$OLLAMA_URL/api/tags" | grep -q "$MODEL"; then
    echo "   ok — Ollama odpowiada i ma model $MODEL"
else
    echo "   UWAGA: Ollama nie odpowiada albo nie ma modelu $MODEL — kontener i tak wystartuje"
    echo "   i będzie czekał, aż komputer z Ollamą się pojawi."
fi

docker rm -f mkv-translator >/dev/null 2>&1 || true
# --init: zatrzymanie kontenera kończy program od razu (bez czekania na SIGKILL w trakcie składania)
# --user 99:100: nowe pliki należą do nobody:users jak reszta udziałów Unraid
# /mnt w całości: program podmienia filmy na dysku, na którym naprawdę leżą (/mnt/diskN), a nie przez /mnt/user
docker run -d --name mkv-translator --restart unless-stopped --init --user 99:100 \
    --log-opt max-size=10m --log-opt max-file=2 \
    -e OLLAMA_URL="$OLLAMA_URL" -e TZ=Europe/Warsaw \
    -v /mnt:/mnt -v "$DIR":/config \
    mkv-translator --watch "$CO_ILE_MINUT" --parallel "$ROWNOLEGLE" --model "$MODEL" --source "${JEZYK:-auto}" --target "${CEL:-pl}" --ui-lang "${JEZYK_KOMUNIKATOW:-pl}" --mux ${NTFY:+--ntfy "$NTFY"} "${FOLDERY[@]}"

echo "== gotowe. Podgląd pracy:  docker logs -f mkv-translator   (albo ikona kontenera > Logs w zakładce Docker)"
