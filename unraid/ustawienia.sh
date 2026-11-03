# Ustawienia MKV Translatora na Unraid. Po zmianie uruchom ponownie zainstaluj.sh (leży obok)

# Ollama na komputerze z kartą graficzną (bezpośrednie połączenie 10.10.10.x)
OLLAMA_URL="http://10.10.10.1:11434"

# foldery sprawdzane w poszukiwaniu nowych filmów (razem z podfolderami); każdy w osobnym cudzysłowie
FOLDERY=("/mnt/user/plex/Rosyjskie")

# co ile minut sprawdzać foldery
CO_ILE_MINUT=30

# ile partii tłumaczyć naraz (jak "równolegle" w okienku; Ollama ma OLLAMA_NUM_PARALLEL=4)
ROWNOLEGLE=4

# powiadomienia na telefon (aplikacja ntfy, temat jak w oknie programu); puste = bez powiadomień
NTFY=""

# język źródłowy: auto (rosyjski, potem angielski i ukraiński), ru, en albo uk
JEZYK="auto"

# język napisów wynikowych: pl (polski) albo en (angielski, bez lektora)
CEL="pl"

# model Ollamy
MODEL="gemma4:31b"

# język komunikatów w logu kontenera: pl albo en
JEZYK_KOMUNIKATOW="pl"
