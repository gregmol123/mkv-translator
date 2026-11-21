"""Okno kolejki MKV Translatora (tkinter). Logika tłumaczenia jest w translate_mkv.py — ten moduł tylko ją wywołuje.

Uruchamiane przez translate_mkv.run_gui() (skrypt bez argumentów, --gui albo plik .exe).
"""
from translate_mkv import *                  # noqa: F401,F403 — okno korzysta z całej logiki programu
from translate_mkv import FINISHED_STATES    # noqa: F401


# ---------------------------------------------------------------- okno kolejki

class LogWriter:
    """Zastępuje stdout: przekazuje wypisywany tekst do okna i dopisuje go do pliku dziennika."""

    def __init__(self, events, log_file=None):
        self.events = events
        self.log_file = log_file

    def write(self, text):
        if not text:
            return
        self.events.put(("log", text))
        if self.log_file:
            kept = re.sub(r"\r[^\n]*", "", text)    # bez liczników postępu nadpisywanych w miejscu
            if kept:
                try:
                    with open(self.log_file, "a", encoding="utf-8") as f:
                        f.write(kept)
                except OSError:
                    pass

    def flush(self):
        pass


def ollama_models():
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return sorted(m["name"] for m in json.load(r).get("models", []))
    except (urllib.error.URLError, OSError, ValueError):
        return []


def changelog_text():
    """Treść CHANGELOG.md: obok skryptu, a w .exe — dołączona przy budowaniu (sys._MEIPASS)."""
    for folder in (Path(getattr(sys, "_MEIPASS", APP_DIR)), APP_DIR, Path(__file__).resolve().parent):
        try:
            return (folder / "CHANGELOG.md").read_text(encoding="utf-8")
        except OSError:
            continue
    return tr('Historia zmian jest na GitHubie: https://github.com/gregmol123/mkv-translator/blob/main/CHANGELOG.md')


def tk_error():
    import tkinter
    return tkinter.TclError


class QueueApp:
    WAITING, RUNNING = "Oczekuje", "W toku..."

    # opcje okna zapisywane razem z kolejką
    SAVED_OPTIONS = ("recursive", "model", "mux", "keep", "force", "whisper", "asr", "separate",
                     "lektor", "voice", "replace_audio", "parallel", "move_done", "notify_toast", "ntfy_topic",
                     "source", "target", "repair_model", "whisper_model", "backup", "backup_days", "diarize")
    # listy wyboru, w których okno pokazuje tekst w swoim języku, a kolejka zapisuje klucz (tekst polski)
    AUTO = "automatycznie"
    CHOICE_OPTIONS = {"source": SOURCE_CHOICES, "target": TARGET_CHOICES, "lektor": ("brak",), "repair_model": (AUTO,)}
    FINISHED = FINISHED_STATES               # z tymi stanami film trafia do folderu skonczone
    # stan "Ma polskie" oznacza dziś napisy w wybranym języku docelowym (klucz zostaje dla zapisanych kolejek)
    STATE_LABELS = {"Ma polskie": "Ma już napisy"}
    STATE_COLORS = {"Gotowe": {"foreground": "#1a7f37"}, "Ma polskie": {"foreground": "#2f6f4f"},
                    "Do sprawdzenia": {"foreground": "#8250df"},
                    "Błąd": {"foreground": "#cf222e"}, "Przerwano": {"foreground": "#9a6700"},
                    "Pominięto": {"foreground": "#6e7781"},
                    "W toku...": {"foreground": "#0b5cad", "background": "#e3f0ff"}}
    PROGRESS_RE = re.compile(r"(przetłumaczono|translated|lektor:?|voice-over:?|kwestia|cue|rozpozna\w*|recogni\w*)"
                             r"\D*(\d+)\s*/\s*(\d+)")

    def __init__(self, root, paths, queue_file=None):
        import queue
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk, self.root = tk, ttk, root
        self.events = queue.Queue()
        self.engine = None
        self.running = False
        self.current = None
        self.modes = {}                       # przerwane pliki: "resume" albo "restart"
        self.states = {}                      # stan pliku (klucz po polsku); w tabeli widać tr(stan)
        self.tracks = {}                      # ścieżka napisów wybrana ręcznie dla pliku (numer od 1)
        self.alive = True                     # False po przebudowie okna w innym języku
        self.roots = {}                       # folder dodany do kolejki, z którego pochodzi plik
        self.move_flag = False                # stan pola "gotowe → skonczone" odczytany w wątku okna
        self.durations = []                   # czasy plików przetworzonych w tej sesji (do szacowania końca)
        self.avg_seconds = None               # średni czas pliku z poprzednich sesji (zapisany z kolejką)
        self.file_started = self.run_started = None
        self.stop_reason = None               # awaria narzędzia, która zatrzymała kolejkę (do powiadomienia)
        self.queue_file = queue_file          # tu kolejka i jej stan czekają na następne uruchomienie
        self.plex_log = queue_file.with_name("plex_nazwy.json") if queue_file else None
        # dziennik obok programu: zostaje po zamknięciu okna, więc da się sprawdzić przyczynę błędów
        sys.stdout = sys.stderr = LogWriter(self.events, queue_file.with_name("dziennik.txt") if queue_file else None)

        root.title(tr('MKV Translator {0} — tłumaczenie napisów', VERSION))
        root.geometry("1320x800")
        root.minsize(1040, 600)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=24)
        style.configure("Start.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 4))
        style.configure("Head.TLabelframe.Label", font=("Segoe UI", 9, "bold"))

        # ---- pasek narzędzi: dodawanie i porządkowanie kolejki | narzędzia | start/stop
        bar = ttk.Frame(root, padding=(10, 10, 10, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text=tr('＋ Dodaj pliki...'), command=self.add_files).pack(side="left")
        ttk.Button(bar, text=tr('＋ Dodaj folder...'), command=self.add_folder).pack(side="left", padx=(4, 0))
        self.recursive = tk.BooleanVar(value=True)
        ttk.Checkbutton(bar, text=tr('z podfolderami'), variable=self.recursive).pack(side="left", padx=(6, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        for label, cmd in (("▲", lambda: self.move(-1)), ("▼", lambda: self.move(1)),
                           (tr("↻ Ponów"), self.requeue), (tr("✕ Usuń"), self.remove)):
            ttk.Button(bar, text=label, command=cmd, width=len(label) + 2).pack(side="left", padx=(0, 4))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        tools = ttk.Menubutton(bar, text=tr('Narzędzia'))
        menu = tk.Menu(tools, tearoff=False)
        self.fill_menu(menu, ((tr('Ustaw równoległość w Ollamie teraz (restart Ollamy)'),
                               lambda: self.apply_parallel(then_start=False)),
                              (tr('Popraw oznaczone „Do sprawdzenia”'), self.repair_flagged),
                              (tr('Sprawdź, które pliki mają już napisy'), self.check_polish),
                              (tr('Nazwy Plex...'), self.plex_dialog), (tr('Zamień TS → MKV'), self.ts_to_mkv), None,
                              (tr('Usuń zakończone z kolejki'), self.clear_done), (tr('Wyczyść całą kolejkę'), self.clear),
                              None, (tr('Raport jakości tłumaczeń'), self.open_report), (tr('Otwórz dziennik'), self.open_log),
                              (tr('Sprawdź instalację'), self.check_install), None,
                              (tr('Pobierz model Ollamy...'), self.pull_dialog), None,
                              (tr('Co nowego (historia zmian)'), self.changelog_dialog)))
        tools["menu"] = menu
        tools.pack(side="left")
        self.stop_btn = ttk.Button(bar, text=tr('■ Stop'), command=self.stop, state="disabled")
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(bar, text=tr('▶ Start'), command=self.start, style="Start.TButton")
        self.start_btn.pack(side="right", padx=(0, 6))
        # język okna: zmiana przebudowuje okno (kolejka i ustawienia zostają)
        self.ui_lang = tk.StringVar(value=UI_LANGS[ui["lang"]])
        lang_box = ttk.Combobox(bar, textvariable=self.ui_lang, values=list(UI_LANGS.values()), width=8,
                                state="readonly")
        lang_box.pack(side="right", padx=(0, 16))
        lang_box.bind("<<ComboboxSelected>>", lambda e: self.switch_ui_lang())
        ttk.Label(bar, text=tr('Język okna:')).pack(side="right", padx=(0, 4))

        # ---- środek: kolejka nad dziennikiem (lewa strona) i ustawienia (prawa strona)
        body = ttk.Panedwindow(root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10)
        left = ttk.Panedwindow(body, orient="vertical")
        body.add(left, weight=1)

        qframe = ttk.Labelframe(left, text=tr('Kolejka'), padding=4, style="Head.TLabelframe")
        left.add(qframe, weight=3)
        self.tree = ttk.Treeview(qframe, columns=("file", "dir", "track", "state"), show="headings",
                                 selectmode="extended")
        for col, text, width, stretch in (("file", tr("Plik"), 380, True), ("dir", tr("Folder"), 300, True),
                                          ("track", tr("Napisy"), 70, False), ("state", tr("Stan"), 120, False)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, stretch=stretch)
        for state, colors in self.STATE_COLORS.items():
            self.tree.tag_configure(state, **colors)
        scroll = ttk.Scrollbar(qframe, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Delete>", lambda e: self.remove())
        self.tree.bind("<Double-1>", lambda e: self.open_folder())
        self.tree.bind("<Button-3>", self.context_menu)
        self.row_menu = tk.Menu(root, tearoff=False)
        self.fill_menu(self.row_menu, ((tr('Otwórz folder'), self.open_folder),
                                       (tr('Ponów (ustaw jako oczekujące)'), self.requeue),
                                       (tr('Wybierz ścieżkę napisów...'), self.track_dialog),
                                       (tr('W górę'), lambda: self.move(-1)), (tr("W dół"), lambda: self.move(1)), None,
                                       (tr('Usuń z kolejki'), self.remove)))

        lframe = ttk.Labelframe(left, text=tr('Dziennik'), padding=4, style="Head.TLabelframe")
        left.add(lframe, weight=1)
        self.log = tk.Text(lframe, height=10, state="disabled", wrap="none", font=("Consolas", 9),
                           relief="flat", background="#fbfbfb")
        log_scroll = ttk.Scrollbar(lframe, command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log.pack(fill="both", expand=True)

        side = ttk.Frame(body, padding=(10, 0, 0, 0))
        body.add(side, weight=0)

        def group(title):
            frame = ttk.Labelframe(side, text=title, padding=(10, 6), style="Head.TLabelframe")
            frame.pack(fill="x", pady=(0, 8))
            frame.columnconfigure(1, weight=1)
            return frame

        def check(frame, row, text, var):
            ttk.Checkbutton(frame, text=text, variable=var).grid(row=row, column=0, columnspan=2, sticky="w", pady=1)

        g = group(tr('Tłumaczenie'))
        models = ollama_models()
        self.model = tk.StringVar(value=OLLAMA_MODEL if OLLAMA_MODEL in models or not models else models[0])
        ttk.Label(g, text=tr('Model:')).grid(row=0, column=0, sticky="w")
        row = ttk.Frame(g)
        row.grid(row=0, column=1, sticky="ew", pady=2)
        self.model_box = ttk.Combobox(row, textvariable=self.model, values=models, width=22)
        self.model_box.pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="⇩", width=3, command=self.pull_dialog).pack(side="left", padx=(4, 0))
        ttk.Label(g, text=tr('Do poprawek:')).grid(row=1, column=0, sticky="w")
        self.repair_model = tk.StringVar(value=tr(self.AUTO))
        self.repair_box = ttk.Combobox(g, textvariable=self.repair_model, values=[tr(self.AUTO), *models],
                                       width=26, state="readonly")
        self.repair_box.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(g, text=tr('Równolegle:')).grid(row=2, column=0, sticky="w")
        self.parallel = tk.IntVar(value=2)
        par = ttk.Frame(g)
        par.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Spinbox(par, from_=1, to=8, width=4, textvariable=self.parallel).pack(side="left")
        # ile zapytań naraz przyjmuje sama Ollama (OLLAMA_NUM_PARALLEL); przy Starcie wyrównywane do pola obok
        self.server_par = ttk.Label(par, text="", foreground="#57606a")
        self.server_par.pack(side="left", padx=(8, 0))
        self.show_server_parallel()
        ttk.Label(g, text=tr('Z języka:')).grid(row=3, column=0, sticky="w")
        self.source = tk.StringVar(value=tr(self.AUTO))
        ttk.Combobox(g, textvariable=self.source, values=[tr(k) for k in SOURCE_CHOICES], width=14,
                     state="readonly").grid(row=3, column=1, sticky="w", pady=2)
        ttk.Label(g, text=tr('Na język:')).grid(row=4, column=0, sticky="w")
        self.target = tk.StringVar(value=tr("polski"))
        ttk.Combobox(g, textvariable=self.target, values=[tr(k) for k in TARGET_CHOICES], width=14,
                     state="readonly").grid(row=4, column=1, sticky="w", pady=2)
        self.force = tk.BooleanVar(value=False)
        check(g, 5, tr('tłumacz ponownie (nawet gdy napisy już są)'), self.force)

        g = group(tr('Zapis wyniku'))
        self.mux = tk.BooleanVar(value=True)
        self.keep = tk.BooleanVar(value=False)
        self.move_done = tk.BooleanVar(value=True)
        check(g, 0, tr('wbuduj napisy do pliku MKV'), self.mux)
        check(g, 1, tr('zostaw oryginał (kopia .pl.mkv / .en.mkv)'), self.keep)
        check(g, 2, tr('gotowe → folder „{0}” (dyski lokalne)', DONE_DIR), self.move_done)
        # kopia oryginału przy podmianie: zmiana nazwy na .orig.mkv, program kasuje ją po podanej liczbie dni
        self.backup = tk.BooleanVar(value=False)
        self.backup_days = tk.IntVar(value=7)
        row = ttk.Frame(g)
        row.grid(row=3, column=0, columnspan=2, sticky="w", pady=1)
        ttk.Checkbutton(row, text=tr('kopia oryginału .orig.mkv przez'), variable=self.backup).pack(side="left")
        ttk.Spinbox(row, from_=1, to=90, width=4, textvariable=self.backup_days).pack(side="left", padx=(4, 4))
        ttk.Label(row, text=tr('dni')).pack(side="left")

        g = group(tr('Gdy brak napisów'))
        self.whisper = tk.BooleanVar(value=True)
        self.asr = tk.StringVar(value="GigaAM+Whisper")   # B8: najmniej błędów w pomiarze (10,8% wobec 13,2%)
        self.separate = tk.BooleanVar(value=True)
        check(g, 0, tr('rozpoznaj mowę z dźwięku'), self.whisper)
        ttk.Label(g, text=tr('Silnik:')).grid(row=1, column=0, sticky="w")
        ttk.Combobox(g, textvariable=self.asr, values=["GigaAM", "GigaAM-RNNT", "Whisper", "GigaAM+Whisper"], width=16,
                     state="readonly").grid(row=1, column=1, sticky="w", pady=2)
        # GigaAM zna tylko rosyjski — inne języki mowy zawsze rozpoznaje Whisper w wybranym rozmiarze
        ttk.Label(g, text=tr('Model Whisper:')).grid(row=2, column=0, sticky="w")
        self.whisper_model = tk.StringVar(value=WHISPER_MODELS[0])
        ttk.Combobox(g, textvariable=self.whisper_model, values=WHISPER_MODELS, width=14,
                     state="readonly").grid(row=2, column=1, sticky="w", pady=2)
        check(g, 3, tr('oddziel głos od muzyki (Demucs)'), self.separate)
        self.diarize = tk.BooleanVar(value=False)
        check(g, 4, tr('rozpoznawaj mówców (myślniki, płeć; pyannote)'), self.diarize)

        g = group(tr('Lektor'))
        self.lektor = tk.StringVar(value=tr("brak"))
        self.voice = tk.StringVar()
        self.replace_audio = tk.BooleanVar(value=False)
        ttk.Label(g, text=tr('Silnik:')).grid(row=0, column=0, sticky="w")
        lektor_box = ttk.Combobox(g, textvariable=self.lektor, values=[tr("brak"), "Piper", "XTTS-v2"],
                                  width=12, state="readonly")
        lektor_box.grid(row=0, column=1, sticky="w", pady=2)
        lektor_box.bind("<<ComboboxSelected>>", lambda e: self.lektor_changed())
        ttk.Label(g, text=tr('Głos:')).grid(row=1, column=0, sticky="w")
        self.voice_box = ttk.Combobox(g, textvariable=self.voice, width=26, state="disabled")
        self.voice_box.grid(row=1, column=1, sticky="ew", pady=2)
        self.sample_btn = ttk.Button(g, text=tr('Próbka głosu...'), command=self.pick_sample, state="disabled")
        self.sample_btn.grid(row=2, column=1, sticky="w", pady=2)
        check(g, 3, tr('usuń oryginalne audio'), self.replace_audio)

        g = group(tr('Po zakończeniu kolejki'))
        self.notify_toast = tk.BooleanVar(value=True)
        self.ntfy_topic = tk.StringVar()
        check(g, 0, tr('powiadomienie Windows'), self.notify_toast)
        ttk.Label(g, text=tr('Telefon (ntfy):')).grid(row=1, column=0, sticky="w")
        ttk.Entry(g, textvariable=self.ntfy_topic, width=20).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(g, text=tr('Wyślij próbne'), command=self.test_notify).grid(row=2, column=1, sticky="w", pady=2)

        # ---- pasek stanu: postęp całej kolejki i bieżącego pliku
        foot = ttk.Frame(root, padding=(10, 6, 10, 10))
        foot.pack(fill="x")
        self.progress = ttk.Progressbar(foot, length=260, maximum=1)
        self.progress.pack(side="left")
        self.status = ttk.Label(foot, text=tr('Dodaj pliki lub foldery do kolejki.'))
        self.status.pack(side="left", padx=10)
        self.file_progress = ttk.Label(foot, text="", foreground="#0b5cad")
        self.file_progress.pack(side="right")
        self.eta = ttk.Label(foot, text="", foreground="#57606a")
        self.eta.pack(side="right", padx=(0, 16))

        if not models:
            print(tr('UWAGA: Ollama nie odpowiada pod {0} — uruchom ją przed startem.', OLLAMA_URL))
        self.load_queue()
        self.add(paths)
        if os.environ.get("MKV_AUTOSTART") == "1":   # uruchomienie z harmonogramu/skryptu: kolejka rusza sama
            root.after(1500, self.start)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def state_of(self, item):
        return self.states.get(item, self.WAITING)

    def set_state(self, item, state):
        self.states[item] = state
        self.tree.set(item, "state", tr(self.STATE_LABELS.get(state, state)))

    def set_track(self, item, track):
        if track:
            self.tracks[item] = int(track)
        else:
            self.tracks.pop(item, None)
        self.tree.set(item, "track", f"#{track}" if track else tr("auto"))

    def switch_ui_lang(self):
        """Przebudowuje okno w wybranym języku; kolejka i ustawienia przechodzą przez kolejka.json."""
        from tkinter import messagebox
        lang = next((k for k, v in UI_LANGS.items() if v == self.ui_lang.get()), "pl")
        if lang == ui["lang"]:
            return
        if self.running:
            self.ui_lang.set(UI_LANGS[ui["lang"]])
            messagebox.showinfo("MKV Translator", tr('Język okna można zmienić, gdy kolejka nie pracuje.'))
            return
        self.save_queue()
        set_ui_lang(lang)
        stop_helper()                         # proces audio wystartuje z komunikatami w nowym języku
        self.save_queue()
        self.alive = False
        for child in self.root.winfo_children():
            child.destroy()
        QueueApp(self.root, [], self.queue_file)

    def track_dialog(self):
        """Wybór ścieżki napisów do tłumaczenia dla zaznaczonego filmu (zamiast wyboru automatycznego)."""
        from tkinter import messagebox
        selected = [i for i in self.tree.selection() if i != self.current]
        if not selected:
            return
        item = selected[0]
        path = Path(self.tree.set(item, "dir")) / self.tree.set(item, "file")
        if path.suffix.lower() not in VIDEO_EXTS:
            messagebox.showinfo("MKV Translator", tr('Ścieżkę napisów wybiera się tylko dla filmów.'))
            return
        try:
            streams = subtitle_streams(path)
        except Exception as e:                # np. plik usunięty albo nieczytelny
            messagebox.showerror("MKV Translator", tr('Nie da się odczytać ścieżek: {0}', e))
            return
        if not streams:
            messagebox.showinfo("MKV Translator", tr('Plik nie ma wbudowanych napisów.'))
            return
        tk, ttk = self.tk, self.ttk
        win = tk.Toplevel(self.root)
        win.title(tr('Ścieżka napisów — {0}', path.name))
        win.transient(self.root)
        win.grab_set()
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=tr('Którą ścieżkę tłumaczyć? (dotyczy {0} zaznaczonych plików)', len(selected))
                  ).pack(anchor="w")
        choice = tk.IntVar(value=self.tracks.get(item, 0))
        ttk.Radiobutton(frame, text=tr('automatycznie (najpełniejsza w języku źródłowym)'), value=0,
                        variable=choice).pack(anchor="w", pady=(6, 0))
        for n, st in enumerate(streams, 1):
            tags = st.get("tags", {})
            kind = tr("graficzne — OCR") if st.get("codec_name") == PGS else st.get("codec_name", "?")
            forced = tr(", wymuszone") if st.get("disposition", {}).get("forced") else ""
            text = f"#{n}  [{lang_of(st) or '?'}]  {tags.get('title', '')}  ({kind}{forced})"
            ttk.Radiobutton(frame, text=text, value=n, variable=choice).pack(anchor="w")

        def ok():
            for i in selected:
                self.set_track(i, choice.get() or None)
            self.save_queue()
            win.destroy()

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="OK", command=ok).pack(side="left")
        ttk.Button(buttons, text=tr('Anuluj'), command=win.destroy).pack(side="left", padx=6)

    def changelog_dialog(self):
        """Historia zmian (CHANGELOG.md, w .exe dołączony do programu) w osobnym oknie."""
        tk, ttk = self.tk, self.ttk
        if getattr(self, "log_win", None) and self.log_win.winfo_exists():
            self.log_win.lift()
            return
        text = changelog_text()
        win = self.log_win = tk.Toplevel(self.root)
        win.title(tr('Co nowego — MKV Translator {0}', VERSION))
        win.geometry("820x620")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=8)
        frame.pack(fill="both", expand=True)
        box = tk.Text(frame, wrap="word", font=("Segoe UI", 10), relief="flat", padx=10, pady=8)
        scroll = ttk.Scrollbar(frame, command=box.yview)
        box.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        box.pack(fill="both", expand=True)
        box.tag_configure("h1", font=("Segoe UI", 15, "bold"), spacing3=6)
        box.tag_configure("h2", font=("Segoe UI", 12, "bold"), spacing1=12, spacing3=4, foreground="#0b5cad")
        box.tag_configure("h3", font=("Segoe UI", 10, "bold"), spacing1=6)
        for line in text.splitlines():
            clean = re.sub(r"\*\*|`|\[([^\]]+)\]\([^)]+\)", lambda m: m.group(1) or "", line)
            if line.startswith("### "):
                box.insert("end", clean[4:] + "\n", "h3")
            elif line.startswith("## "):
                box.insert("end", clean[3:] + "\n", "h2")
            elif line.startswith("# "):
                box.insert("end", clean[2:] + "\n", "h1")
            else:
                box.insert("end", ("• " + clean[2:] if clean.startswith("- ") else clean) + "\n")
        box.configure(state="disabled")
        ttk.Button(frame, text=tr('Zamknij'), command=win.destroy).pack(side="bottom", anchor="e", pady=(8, 0))

    def pull_dialog(self):
        """Pobieranie modelu do Ollamy (ollama pull) z postępem; po pobraniu odświeża listy modeli."""
        tk, ttk = self.tk, self.ttk
        if getattr(self, "pull_win", None) and self.pull_win.winfo_exists():
            self.pull_win.lift()
            return
        win = self.pull_win = tk.Toplevel(self.root)
        win.title(tr('Pobierz model Ollamy'))
        win.transient(self.root)
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=tr('Nazwa modelu (z ollama.com/library), np. gemma4:31b:')).pack(anchor="w")
        name = tk.StringVar(value="translategemma:27b")
        ttk.Combobox(frame, textvariable=name, width=36,
                     values=["gemma4:31b", "translategemma:27b", "gemma4:26b", "qwen3.5:27b", "gemma3:27b",
                             "translategemma:12b", "gemma3:12b"]).pack(fill="x", pady=4)
        self.pull_bar = ttk.Progressbar(frame, length=360, maximum=1)
        self.pull_bar.pack(fill="x", pady=4)
        self.pull_label = ttk.Label(frame, text=tr('Duże modele mają 15–20 GB — pobieranie trwa kilka minut.'),
                                    foreground="#57606a")
        self.pull_label.pack(anchor="w")
        button = ttk.Button(frame, text=tr('Pobierz'))
        button.pack(anchor="e", pady=(8, 0))

        def start():
            model = name.get().strip()
            if not model:
                return
            button.configure(state="disabled")
            threading.Thread(target=self.pull_work, args=(model,), daemon=True).start()

        button.configure(command=start)

    def pull_work(self, model):
        """Wątek pobierania: /api/pull wysyła postęp jako wiersze JSON."""
        body = json.dumps({"model": model, "stream": True}).encode("utf-8")
        req = urllib.request.Request(f"{OLLAMA_URL}/api/pull", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                for line in r:
                    data = json.loads(line or b"{}")
                    if data.get("error"):
                        raise FatalError(data["error"])
                    total, done = data.get("total"), data.get("completed")
                    self.events.put(("pull", data.get("status", ""), done / total if total and done else None,
                                     False))
            print(tr('\nPobrano model Ollamy: {0}', model))
            self.events.put(("pull", tr('Gotowe: {0}', model), 1.0, True))
        except (urllib.error.URLError, OSError, ValueError, FatalError) as e:
            print(tr('\nNie udało się pobrać modelu {0}: {1}', model, e))
            self.events.put(("pull", tr('Błąd: {0}', e), None, True))

    def pull_progress(self, status, fraction, finished):
        win = getattr(self, "pull_win", None)
        if win is not None and win.winfo_exists():
            self.pull_label.configure(text=status + (f"  {fraction:.0%}" if fraction is not None else ""))
            if fraction is not None:
                self.pull_bar.configure(value=fraction)
        if finished:                          # nowy model od razu na listach wyboru
            models = ollama_models()
            self.model_box.configure(values=models)
            self.repair_box.configure(values=[tr(self.AUTO), *models])

    def lektor_changed(self):
        voices = {"Piper": PIPER_VOICES, "XTTS-v2": XTTS_VOICES}.get(self.lektor.get(), [])
        self.voice_box.configure(values=voices, state="normal" if voices else "disabled")
        self.voice.set(voices[0] if voices else "")
        self.sample_btn.configure(state="normal" if self.lektor.get() == "XTTS-v2" else "disabled")

    def pick_sample(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(title=tr('Próbka głosu do naśladowania (6–30 s czystej mowy)'),
                                          filetypes=[(tr("Dźwięk"), "*.wav *.flac")])
        if path:
            self.voice.set(path)

    # ---- kolejka

    def add(self, paths):
        queued = {str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")) for i in self.tree.get_children()}
        for p in map(Path, paths):
            root = p.resolve() if p.is_dir() else p.resolve().parent
            for f in collect([p], self.recursive.get()):
                f = f.resolve()
                if str(f) not in queued and f.suffix.lower() in SUB_EXTS | VIDEO_EXTS:
                    queued.add(str(f))
                    item = self.tree.insert("", "end", values=(f.name, str(f.parent), "", ""))
                    self.set_state(item, self.WAITING)
                    self.set_track(item, None)
                    self.roots[item] = str(root)
        self.update_status()

    def finish_move(self, item, path):
        """Po zakończeniu: przeniesienie filmu z dysku lokalnego do folderu skonczone (w wątku roboczym)."""
        if not self.move_flag or path.suffix.lower() not in VIDEO_EXTS or not is_local(path) \
                or DONE_DIR in path.parts:          # już leży w "skonczone" (np. poprawiany ponownie)
            return
        try:
            new = move_done(path, self.roots.get(item) or path.parent)
            print(tr('    przeniesiono do {0}', new.parent))
            self.events.put(("renamed", item, new))
        except (FatalError, OSError) as e:
            print(tr('    nie przeniesiono do „{0}”: {1}', DONE_DIR, e))

    def add_files(self):
        from tkinter import filedialog
        self.add(filedialog.askopenfilenames(
            title=tr('Wybierz filmy MKV lub pliki napisów'),
            filetypes=[(tr("Filmy i napisy"), "*.mkv *.avi *.mp4 *.m4v *.ts *.mov *.wmv *.mpg *.webm *.srt *.ass *.ssa *.vtt *.sub"),
                       ("Matroska", "*.mkv"),
                       (tr("Napisy"), "*.srt *.ass *.ssa *.vtt *.sub")]))

    def add_folder(self):
        from tkinter import filedialog
        folder = filedialog.askdirectory(title=tr('Wybierz folder z filmami MKV lub napisami'))
        if folder:
            self.add([folder])

    def remove(self):
        for item in self.tree.selection():
            if item != self.current:          # pliku w toku nie da się usunąć
                self.tree.delete(item)
        self.update_status()

    def clear_done(self):
        for item in self.tree.get_children():
            if self.state_of(item) not in (self.WAITING, self.RUNNING):
                self.tree.delete(item)
        self.update_status()

    def clear(self):
        for item in self.tree.get_children():
            if item != self.current:
                self.tree.delete(item)
        self.update_status()

    def move(self, step):
        items = list(self.tree.selection())
        for item in (items if step < 0 else reversed(items)):
            index = self.tree.index(item) + step
            if 0 <= index < len(self.tree.get_children()):
                self.tree.move(item, "", index)
        self.save_queue()

    def requeue(self):
        """Ustawia zaznaczone pliki z powrotem jako oczekujące (np. żeby przetłumaczyć je ponownie)."""
        for item in self.tree.selection():
            if item != self.current:
                self.set_state(item, self.WAITING)
        self.update_status()

    def check_polish(self):
        """Sprawdza oczekujące pliki (w tle) i oznacza jako "Ma polskie" te, które mają już polskie napisy."""
        from tkinter import messagebox
        jobs = [(i, Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")) for i in self.tree.get_children()
                if self.state_of(i) in (self.WAITING, "Błąd", "Pominięto")
                and Path(self.tree.set(i, "file")).suffix.lower() in VIDEO_EXTS]
        if self.running or not jobs:
            if not jobs:
                messagebox.showinfo("MKV Translator", tr('W kolejce nie ma oczekujących filmów do sprawdzenia.'))
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.move_flag = self.move_done.get()
        OUT["code"] = TARGET_CHOICES.get(untr(self.target.get(), TARGET_CHOICES), "pl")

        def work():
            print(tr('\nSprawdzanie, czy są już napisy ({0}): {1} plików', target_adj(), len(jobs)))
            found = 0
            for item, f in jobs:
                try:
                    what = polish_embedded(f, subtitle_streams(f))
                    if what:
                        print(tr('    {0}: wbudowane — {1}', f.name, what))
                    else:
                        own = f.with_suffix(f".{OUT['code']}.srt")
                        side = own if own.exists() else external_polish(f)
                        what = side and f"obok filmu — {side.name}"
                        if what:
                            print(f"    {f.name}: {what}")
                except Exception as e:        # jeden nieczytelny plik nie może zawiesić okna
                    print(tr('    {0}: nie da się sprawdzić ({1})', f.name, e))
                    continue
                if what:
                    found += 1
                    self.events.put(("marked", item, "Ma polskie"))
                    self.finish_move(item, f)
            print(tr('Z gotowymi napisami: {0} z {1} (oznaczone „Ma już napisy” — Start je pomija)', found, len(jobs)))
            self.events.put(("checked",))

        threading.Thread(target=work, daemon=True).start()

    def ts_to_mkv(self):
        """Zamienia pliki TS z kolejki na MKV (w tle) i podmienia ich ścieżki w kolejce."""
        from tkinter import messagebox
        items = [i for i in self.tree.get_children()
                 if Path(self.tree.set(i, "file")).suffix.lower() in (".ts", ".m2ts") and i != self.current]
        if self.running or not items:
            if not items:
                messagebox.showinfo("MKV Translator", tr('W kolejce nie ma plików TS.'))
            return
        if not messagebox.askyesno("MKV Translator", tr('Zamienić {0} plików TS na MKV?\n\nObraz i dźwięk zostają bez zmian (bez przekodowania), polskie napisy .pl.srt trafiają do środka. Pliki TS zostaną usunięte po sprawdzeniu nowych.', len(items))):
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        jobs = [(i, Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")) for i in items]

        def work():
            print(tr('\nZamiana TS na MKV: {0} plików', len(jobs)))
            try:
                for item, src in jobs:
                    try:
                        dst = remux_to_mkv(src)
                        print(f"    {src.name} -> {dst.name}")
                        self.events.put(("renamed", item, dst))
                    except (FatalError, OSError) as e:
                        print(tr('    {0}: BŁĄD: {1}', src.name, e))
                        if src.with_suffix(".mkv").exists() and not src.exists():
                            self.events.put(("renamed", item, src.with_suffix(".mkv")))
            finally:
                self.events.put(("checked",))   # przycisk Start zawsze wraca

        threading.Thread(target=work, daemon=True).start()

    def check_install(self):
        """Kontrola instalacji w tle; wynik trafia do okna logu i dziennika."""
        if self.running:
            return
        cancel.clear()                        # po wcześniejszym Stop sprawdzenie nie może się od razu przerwać
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status.configure(text=tr('Sprawdzam instalację...'))

        def work():
            try:
                check_install(self.model.get().strip())
            except Exception as e:
                print(tr('    BŁĄD: {0}: {1}', type(e).__name__, e))
            self.events.put(("checked",))

        threading.Thread(target=work, daemon=True).start()

    def plex_dialog(self):
        """Podgląd i zmiana nazw (oraz folderów) plików z kolejki na format Plex."""
        if self.running:                      # nie zmieniamy nazw plików, na których właśnie pracuje wątek w tle
            from tkinter import messagebox
            messagebox.showinfo("MKV Translator", tr('Zmiana nazw jest dostępna, gdy kolejka nie pracuje.'))
            return
        tk, ttk = self.tk, self.ttk
        items = {str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")): i
                 for i in self.tree.get_children() if i != self.current}
        win = tk.Toplevel(self.root)
        win.title(tr('Nazwy w formacie Plex'))
        win.geometry("1180x560")
        win.transient(self.root)
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        folders = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text=tr('przenieś do folderów  Serial \\ Season NN'), variable=folders,
                        command=lambda: refresh()).pack(side="left")
        info = ttk.Label(top)
        info.pack(side="left", padx=16)
        frame = ttk.Frame(win, padding=(8, 0))
        frame.pack(fill="both", expand=True)
        table = ttk.Treeview(frame, columns=("old", "new", "note"), show="headings")
        for col, text, width in (("old", tr("Obecnie"), 470), ("new", tr("Po zmianie"), 470), ("note", tr("Uwagi"), 200)):
            table.heading(col, text=text)
            table.column(col, width=width)
        scroll = ttk.Scrollbar(frame, command=table.yview)
        table.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        table.pack(fill="both", expand=True)
        buttons = ttk.Frame(win, padding=8)
        buttons.pack(fill="x")
        state = {"ready": []}

        def short(path):                      # dwa ostatnie foldery i nazwa — tyle wystarcza do oceny
            return str(Path(*path.parts[-3:])) if folders.get() else path.name

        def refresh():
            plan = plex_renames([Path(p) for p in items], folders.get())
            state["ready"] = [(old, new) for old, new, note in plan if not note]
            table.delete(*table.get_children())
            for old, new, note in plan:
                table.insert("", "end", values=(short(old), short(new), note))
            info.configure(text=tr('Do zmiany: {0}    pominięte: {1}    bez zmian: {2}', len(state['ready']), len(plan) - len(state['ready']), len(items) - len(plan)))
            apply_btn.configure(text=tr('Zmień ({0})', len(state['ready'])),
                                state="normal" if state["ready"] else "disabled")

        def apply():
            changed, journal = 0, plex_log_read(self.plex_log)
            for old, new in state["ready"]:
                try:
                    journal += plex_rename(old, new)
                    plex_log_write(journal, self.plex_log)
                except OSError as e:
                    print(tr('Nie zmieniono {0}: {1}', old.name, e))
                    continue
                self.tree.set(items[str(old)], "file", new.name)
                self.tree.set(items[str(old)], "dir", str(new.parent))
                changed += 1
            print(tr('\nZmieniono na format Plex: {0} plików', changed))
            self.save_queue()
            win.destroy()

        def undo(ask=True):
            from tkinter import messagebox
            count = len(plex_log_read(self.plex_log))
            if ask and not messagebox.askyesno(
                    "MKV Translator", tr('Przywrócić poprzednie nazwy i foldery ({0} plików, razem z napisami)?', count),
                    parent=win):
                return
            restored, failed = plex_undo(self.plex_log)
            by_path = {str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")): i
                       for i in self.tree.get_children()}
            for current, original in restored:
                item = by_path.get(str(current))
                if item:
                    self.tree.set(item, "file", original.name)
                    self.tree.set(item, "dir", str(original.parent))
            print(tr('\nPrzywrócono stare nazwy: {0} plików', len(restored)) + (tr(', nie udało się: {0}', failed) if failed else ""))
            self.save_queue()
            win.destroy()

        apply_btn = ttk.Button(buttons, command=apply)
        apply_btn.pack(side="left")
        ttk.Button(buttons, text=tr('Anuluj'), command=win.destroy).pack(side="left", padx=6)
        logged = len(plex_log_read(self.plex_log))
        ttk.Button(buttons, text=tr('Przywróć stare nazwy ({0})', logged), command=undo,
                   state="normal" if logged and not self.running else "disabled").pack(side="right")
        refresh()
        return win, apply, folders, refresh, table, undo

    def load_queue(self):
        try:
            data = json.loads(self.queue_file.read_text(encoding="utf-8"))
        except (AttributeError, OSError, ValueError):
            return
        options = data.get("options", {})
        if options.get("seen_version") != VERSION:   # nowa wersja programu — pokaż, co się zmieniło
            self.root.after(800, self.changelog_dialog)
        self.avg_seconds = (data.get("stats") or {}).get("avg_seconds")
        for name in self.SAVED_OPTIONS:
            if name in options and name != "voice":
                value = options[name]
                getattr(self, name).set(tr(value) if name in self.CHOICE_OPTIONS else value)
        self.lektor_changed()                 # lista głosów zależy od wybranego lektora
        if options.get("voice"):
            self.voice.set(options["voice"])
        for entry in data.get("items", []):
            path = Path(entry["path"])
            # plik przetwarzany w chwili zamknięcia programu traktujemy jak przerwany
            state = "Przerwano" if entry.get("state") == self.RUNNING else entry.get("state", self.WAITING)
            item = self.tree.insert("", "end", values=(path.name, str(path.parent), "", ""))
            self.set_state(item, state)
            self.set_track(item, entry.get("track"))
            if entry.get("root"):
                self.roots[item] = entry["root"]

    def save_queue(self):
        if not self.queue_file:
            return
        items = [{"path": str(Path(self.tree.set(i, "dir")) / self.tree.set(i, "file")),
                  "state": self.state_of(i), "root": self.roots.get(i), "track": self.tracks.get(i)}
                 for i in self.tree.get_children()]
        options = {name: self.safe_parallel() if name == "parallel" else getattr(self, name).get()
                   for name in self.SAVED_OPTIONS}   # puste pole "równolegle" nie może wywrócić zapisu
        for name in self.CHOICE_OPTIONS:
            options[name] = untr(options[name], self.CHOICE_OPTIONS[name])
        options["ui_lang"] = ui["lang"]
        options["seen_version"] = VERSION       # "Co nowego" pokazuje się raz po aktualizacji
        try:
            tmp = self.queue_file.with_name(self.queue_file.name + ".tmp")
            stats = {"avg_seconds": self.average_seconds()}
            tmp.write_text(json.dumps({"items": items, "options": options, "stats": stats}, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
            os.replace(tmp, self.queue_file)
        except OSError:
            pass                              # brak zapisu kolejki nie może przerywać pracy

    def update_status(self):
        self.save_queue()
        items = self.tree.get_children()
        for item in items:                    # kolor wiersza według stanu
            self.tree.item(item, tags=(self.state_of(item),))
        states = [self.state_of(i) for i in items]
        counts = {s: states.count(s) for s in set(states)}
        parts = [tr('w kolejce {0}', len(states))]
        for label, state in ((tr('czeka'), self.WAITING), (tr('gotowe'), "Gotowe"), (tr('do sprawdzenia'), "Do sprawdzenia"),
                             (tr('ma już napisy'), "Ma polskie"),
                             (tr("pominięte"), "Pominięto"), (tr("przerwane"), "Przerwano"), (tr("błędy"), "Błąd")):
            if counts.get(state):
                parts.append(f"{label} {counts[state]}")
        self.status.configure(text="  ·  ".join(parts))
        finished = len(states) - counts.get(self.WAITING, 0) - counts.get(self.RUNNING, 0)
        self.progress.configure(maximum=max(1, len(states)), value=finished)

    @staticmethod
    def fill_menu(menu, entries):
        """Pozycje menu: (etykieta, polecenie) albo None jako separator."""
        for entry in entries:
            if entry is None:
                menu.add_separator()
            else:
                menu.add_command(label=entry[0], command=entry[1])

    def show_server_parallel(self):
        if ollama_is_local():
            value = ollama_server_parallel()
            self.server_par.configure(text=tr('Ollama: {0}', value if value is not None else tr('domyślnie')))

    def apply_parallel(self, then_start):
        """Wyrównuje OLLAMA_NUM_PARALLEL do pola "Równolegle" (restart Ollamy w tle); potem ewentualnie kolejka."""
        want = self.safe_parallel()
        current = ollama_server_parallel()
        if not ollama_is_local() or current == want:
            if not then_start:
                print(tr('\nOllama ma już ustawione: równolegle {0}', want) if ollama_is_local()
                      else tr('\nOllama działa na innym komputerze ({0}) — tam trzeba ją ustawić', OLLAMA_URL))
            return False
        if self.running:
            print(tr('\nOllamy nie restartuję w trakcie pracy — zmiana wejdzie przy następnym Starcie'))
            return False
        print(tr('\nOllama: równolegle {0} → {1} — restartuję Ollamę...', current if current is not None else tr('domyślnie'), want))
        self.running = True
        self.start_btn.configure(state="disabled")
        self.status.configure(text=tr('Restart Ollamy z nowym ustawieniem równoległości...'))

        def work():
            try:
                restart_ollama(want)
                print(tr('    Ollama działa z OLLAMA_NUM_PARALLEL={0}', want))
            except Exception as e:
                print(tr('    nie udało się zrestartować Ollamy: {0}', e))
            self.events.put(("ollama_ready", then_start))

        threading.Thread(target=work, daemon=True).start()
        return True

    def repair_flagged(self):
        """Ponowne tłumaczenie (z automatyczną naprawą) plików oznaczonych "Do sprawdzenia" — zaznaczonych,
        a gdy nic nie zaznaczono — wszystkich takich w kolejce."""
        from tkinter import messagebox
        chosen = [i for i in (self.tree.selection() or self.tree.get_children())
                  if self.state_of(i) == "Do sprawdzenia"]
        if not chosen:
            messagebox.showinfo("MKV Translator", tr('W kolejce nie ma plików „Do sprawdzenia”.'))
            return
        if not messagebox.askyesno("MKV Translator", tr('Przetłumaczyć ponownie {0} plików z automatyczną naprawą?\n\nObecne polskie napisy zostaną zastąpione nowymi (kopia .pl.bak.srt zostaje przy plikach AVI). Pliki z rozpoznawaniem mowy przejdą je ponownie.', len(chosen))):
            return
        for item in chosen:
            self.modes[item] = "repair"
            self.set_state(item, self.WAITING)
        self.update_status()
        if not self.running:
            self.start()

    def test_notify(self):
        topic = self.ntfy_topic.get().strip()
        notify("MKV Translator", tr('Próbne powiadomienie — tak będzie wyglądać informacja o końcu kolejki.'),
               topic or None, self.notify_toast.get())
        print(tr('\nWysłano próbne powiadomienie') + (tr(' (ntfy: {0})', topic) if topic else tr(' (tylko Windows)')))

    def average_seconds(self):
        """Średni czas jednego pliku: z tej sesji, a na początku — z poprzednich."""
        if self.durations:
            recent = self.durations[-20:]
            return sum(recent) / len(recent)
        return self.avg_seconds

    def update_eta(self):
        avg = self.average_seconds()
        waiting = sum(self.state_of(i) == self.WAITING for i in self.tree.get_children())
        if not self.running or not self.current or not avg:
            self.eta.configure(text="")
            return
        elapsed = time.time() - (self.file_started or time.time())
        left = avg * waiting + max(0.0, avg - elapsed)
        end = time.strftime("%H:%M", time.localtime(time.time() + left))
        self.eta.configure(text=tr('do końca ok. {0} (ok. {1})', fmt_duration(left), end))

    def queue_finished(self):
        """Powiadomienie po zakończeniu kolejki albo jej zatrzymaniu przez awarię (nie po ręcznym Stop)."""
        if not self.run_started or (cancel.is_set() and not self.stop_reason):
            return
        states = [self.state_of(i) for i in self.tree.get_children()]
        summary = ", ".join(f"{label} {states.count(s)}" for label, s in
                            ((tr('gotowe'), "Gotowe"), (tr('do sprawdzenia'), "Do sprawdzenia"), (tr('ma już napisy'), "Ma polskie"),
                             (tr('błędy'), "Błąd"), (tr('czeka'), self.WAITING)) if states.count(s))
        took = fmt_duration(time.time() - self.run_started)
        if self.stop_reason:
            title, text = tr('MKV Translator: kolejka zatrzymana'), tr('{0}\n{1} (po {2})', self.stop_reason, summary, took)
        else:
            title, text = tr('MKV Translator: kolejka zakończona'), f"{summary} — czas {took}"
        topic = self.ntfy_topic.get().strip()
        if self.notify_toast.get() or topic:
            threading.Thread(target=notify, args=(title, text, topic or None, self.notify_toast.get()),
                             daemon=True).start()

    def open_report(self):
        if QUALITY_REPORT.exists():
            os.startfile(QUALITY_REPORT)
        else:
            from tkinter import messagebox
            messagebox.showinfo("MKV Translator", tr('Raport jakości jeszcze nie istnieje — powstaje przy tłumaczeniu.'))

    def open_log(self):
        log = self.queue_file.with_name("dziennik.txt") if self.queue_file else None
        if log and log.exists():
            os.startfile(log)

    def open_folder(self):
        selected = self.tree.selection()
        if selected:
            folder = Path(self.tree.set(selected[0], "dir"))
            if folder.exists():
                os.startfile(folder)

    def context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.row_menu.tk_popup(event.x_root, event.y_root)

    # ---- przetwarzanie

    def start(self):
        if self.running:
            return
        # Start ponawia przerwane, a gdy nic nie czeka — także błędne i pominięte.
        # Gotowych nie rusza: do ponownego tłumaczenia służy "Ponów zaznaczone".
        items = self.tree.get_children()
        interrupted = [i for i in items if self.state_of(i) == "Przerwano"]
        if interrupted:
            answer = self.ask_resume(len(interrupted))
            if answer is None:
                return
            for item in interrupted:
                self.modes[item] = "resume" if answer else "restart"
                self.set_state(item, self.WAITING)
        if not any(self.state_of(i) == self.WAITING for i in items):
            for item in items:
                if self.state_of(item) in ("Błąd", "Pominięto"):
                    self.set_state(item, self.WAITING)
        if not any(self.state_of(i) == self.WAITING for i in items):
            self.status.configure(text=tr('Nic nie czeka w kolejce — dodaj pliki albo użyj „Ponów zaznaczone”.'))
            return
        cancel.clear()
        self.run_started, self.stop_reason = time.time(), None
        threading.Thread(target=prune_backups, daemon=True).start()   # stare kopie .orig.mkv
        print(time.strftime("\n===== Start %Y-%m-%d %H:%M =====") + f"  (MKV Translator {VERSION})")
        if self.apply_parallel(then_start=True):   # najpierw restart Ollamy z nową równoległością
            self.stop_btn.configure(state="normal")
            return
        self.running = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.next()

    def safe_parallel(self):
        try:
            return min(8, max(1, int(self.parallel.get())))
        except (tk_error(), ValueError):
            return 2

    def ask_resume(self, count):
        """True = kontynuuj przerwane pliki, False = zacznij je od nowa, None = anuluj."""
        from tkinter import messagebox
        return messagebox.askyesnocancel(
            "MKV Translator", tr('Przerwane pliki: {0}.\n\nTak — kontynuuj od miejsca przerwania\nNie — zacznij te pliki od nowa\nAnuluj — nie uruchamiaj kolejki', count))

    def stop(self):
        cancel.set()
        self.stop_btn.configure(state="disabled")
        print(tr('\nZatrzymuję po bieżącym fragmencie...'))

    def next(self):
        """Uruchamia pierwszy oczekujący plik — kolejkę można zmieniać w trakcie pracy."""
        item = next((i for i in self.tree.get_children() if self.state_of(i) == self.WAITING), None)
        if item is None or cancel.is_set():
            self.running, self.current = False, None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            print(tr('\nZatrzymano.') if cancel.is_set() else tr('\nKolejka zakończona.'))
            self.file_progress.configure(text="")
            stop_helper()                     # karta graficzna wolna (np. dla gry), gdy kolejka nie pracuje
            self.queue_finished()
            self.update_status()
            self.update_eta()
            return
        self.current = item
        self.file_started = time.time()
        self.file_progress.configure(text=tr('{0} — start', self.tree.set(item, 'file')))
        self.set_state(item, self.RUNNING)
        self.tree.see(item)
        self.update_status()
        mkv = Path(self.tree.set(item, "dir")) / self.tree.set(item, "file")
        lektor = {"Piper": "piper", "XTTS-v2": "xtts"}.get(self.lektor.get())
        try:
            backup_days = max(1, int(self.backup_days.get())) if self.backup.get() else 0
        except (tk_error(), ValueError):
            backup_days = 7 if self.backup.get() else 0
        args = argparse.Namespace(mux=self.mux.get(), force=self.force.get(), track=self.tracks.get(item),
                                  backup_days=backup_days,
                                  replace=not self.keep.get(),
                                  whisper=self.whisper.get(), asr=self.asr.get().lower().replace(" ", ""),
                                  separate=self.separate.get(), diarize=self.diarize.get(),
                                  lektor=lektor,
                                  voice=self.voice.get().strip() or None, replace_audio=self.replace_audio.get(),
                                  source=SOURCE_CHOICES.get(untr(self.source.get(), SOURCE_CHOICES), "auto"),
                                  target=TARGET_CHOICES.get(untr(self.target.get(), TARGET_CHOICES), "pl"),
                                  whisper_model=self.whisper_model.get())
        repair = untr(self.repair_model.get(), (self.AUTO,))
        set_repair_model(None if repair == self.AUTO else repair)
        mode = self.modes.pop(item, None)
        args.resume, args.restart = mode == "resume", mode == "restart"
        if mode == "repair":                  # "Popraw oznaczone": tłumaczenie od nowa mimo polskich napisów
            args.force = True
        self.move_flag = self.move_done.get()
        threading.Thread(target=self.work, args=(item, mkv, args, self.model.get().strip(), self.safe_parallel()), daemon=True).start()

    def work(self, item, mkv, args, model, parallel):
        try:
            if self.engine is None or self.engine.model != model:
                self.engine = OllamaEngine(model)
            self.engine.parallel = parallel
            state = process(mkv, args, self.engine)
            if state in self.FINISHED:
                self.finish_move(item, mkv)
        except Cancelled:
            state = "Przerwano"
        except ToolDown as e:
            # awaria Ollamy/środowiska audio dotyczy wszystkich plików — kolejka staje, zamiast oznaczać je jako błędne
            print(tr('    BŁĄD: {0}\n    Zatrzymuję kolejkę — po naprawie wystarczy Start (plik zostanie wznowiony).', e))
            self.stop_reason = str(e)
            cancel.set()
            state = "Przerwano"
        except FatalError as e:
            print(tr('    BŁĄD: {0}', e))
            state = "Błąd"
        except Exception as e:  # nieprzewidziany błąd nie może zatrzymać kolejki
            import traceback
            print(tr('    BŁĄD: {0}: {1}', type(e).__name__, e))
            print(traceback.format_exc())     # pełny ślad w dzienniku — inaczej nie da się znaleźć przyczyny
            state = "Błąd"
        self.events.put(("done", item, state))

    def poll(self):
        import queue
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "log":
                    self.write_log(event[1])
                elif event[0] == "renamed":
                    if self.tree.exists(event[1]):
                        self.tree.set(event[1], "file", event[2].name)
                        self.tree.set(event[1], "dir", str(event[2].parent))
                    self.save_queue()
                elif event[0] == "marked":
                    if self.tree.exists(event[1]):
                        self.set_state(event[1], event[2])
                    self.update_status()
                elif event[0] == "ollama_ready":
                    self.engine = None            # nowy serwer — połączenie zestawiane od nowa
                    self.show_server_parallel()
                    if event[1] and not cancel.is_set():
                        self.next()               # kolejka startuje po restarcie Ollamy
                    else:
                        self.running = False
                        self.start_btn.configure(state="normal")
                        self.stop_btn.configure(state="disabled")
                        self.update_status()
                elif event[0] == "pull":
                    self.pull_progress(*event[1:])
                elif event[0] == "checked":
                    self.running = False
                    self.start_btn.configure(state="normal")
                    self.update_status()
                else:
                    if self.tree.exists(event[1]):
                        self.set_state(event[1], event[2])
                    took = time.time() - (self.file_started or time.time())
                    if event[2] in self.FINISHED and took > 20:   # szybkie pominięcia zaniżałyby średnią
                        self.durations.append(took)
                    self.save_queue()
                    self.next()
        except queue.Empty:
            pass
        finally:
            # okno przebudowane w innym języku — ta kopia przestaje odświeżać
            if self.alive:
                self.ticks = getattr(self, "ticks", 0) + 1
                if self.ticks % 50 == 0:      # co 5 s odświeżenie szacowanego końca
                    self.update_eta()
                self.root.after(100, self.poll)   # także po nieprzewidzianym błędzie — inaczej okno przestaje reagować

    def write_log(self, text):
        found = self.PROGRESS_RE.findall(text)
        if found and self.current and self.tree.exists(self.current):
            what, done, total = found[-1]
            self.file_progress.configure(text=f"{self.tree.set(self.current, 'file')} — {what} {done}/{total}")
        self.log.configure(state="normal")
        first, *rest = text.split("\r")
        self.log.insert("end", first)
        for part in rest:                     # "\r" = nadpisz bieżącą linię (licznik postępu)
            self.log.delete("end-1c linestart", "end-1c")
            self.log.insert("end", part)
        self.log.see("end")
        self.log.configure(state="disabled")

    def close(self):
        if self.running:
            from tkinter import messagebox
            if not messagebox.askyesno("MKV Translator", tr('Tłumaczenie trwa. Przerwać i zamknąć?')):
                return
        self.save_queue()
        stop_helper()                         # proces audio z modelami w pamięci karty
        self.root.destroy()


def run_gui(paths):
    import tkinter as tk
    queue_file = APP_DIR / "kolejka.json"
    try:                                      # język okna zapisany z kolejką — potrzebny przed budową okna
        set_ui_lang(json.loads(queue_file.read_text(encoding="utf-8")).get("options", {}).get("ui_lang", "pl"))
    except (OSError, ValueError):
        pass
    root = tk.Tk()
    QueueApp(root, paths, queue_file)
    root.mainloop()
    return 0
