"""Claude widget - the user's notification center and bridge to their Claude chats.
GPL-3.0-only. Made with AI assistance.

Popups appear without taking focus, click opens that chat in the Claude desktop app, the panel tracks the
bench queue, running tests and chats waiting on the user. Read-only on everything it watches.
"""
import ctypes, json, math, os, queue as _q, random, re, shutil, socket, struct, subprocess, sys, threading, time
import tkinter as tk
import wave
import winreg
from ctypes import wintypes

VERSION = "0.2.0"
APP = "Claude widget"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.environ.get("LOCALAPPDATA", HERE), APP)
_OLD = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "Command Center")
if not os.path.isdir(DATA) and os.path.isdir(_OLD):  # renamed from "Command Center": carry settings over
    shutil.copytree(_OLD, DATA)
CFG = os.path.join(DATA, "config.json")
HIST = os.path.join(DATA, "history.json")
LOG = os.path.join(DATA, "commandcenter.log")
def _early_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# Folder shared with the chats' tools (queue, logs). Set "mcwDir" in config.json; defaults to the app's own data folder.
MCW = _early_cfg().get("mcwDir") or DATA
INBOX = os.path.join(MCW, "notify", "inbox")
PROC = os.path.join(MCW, "notify", "processed")
_LOCAL = os.environ.get("LOCALAPPDATA", "")


def sessions_dirs():
    import glob
    return glob.glob(os.path.join(_LOCAL, "Claude-Data", "claude_*", "claude-code-sessions"))
PORT = 47613
NOWIN = 0x08000000

DEFAULTS = {"opacity": 0.93, "scale": 1.0, "x": None, "y": None, "linger": 12, "volume": 0.5, "mute": False,
            "pause": False, "chime": "soft-chime", "autostart": False, "updateRepo": "", "panel": None}

KIND = {"question": ("#e8b04a", "needs an answer"), "ready": ("#5fb88a", "ready for a look"),
        "blocked": ("#d9824f", "blocked"), "broken": ("#d96060", "broken")}
BG, BG2, FG, DIM, ACC = "#1d2127", "#262c34", "#e6e9ed", "#8d97a3", "#4fb6c9"

user32 = ctypes.windll.user32
user32.GetParent.restype = wintypes.HWND
user32.GetWindowLongW.restype = ctypes.c_long


def log(msg):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
        if os.path.getsize(LOG) > 300000:
            os.replace(LOG, LOG + ".old")
    except OSError:
        pass


def atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def run_quiet(args, timeout=15):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=NOWIN)
        return r.stdout
    except Exception as e:
        log(f"run_quiet {args[:3]}: {e}")
        return ""


# ---------- icon ----------
def make_icon(size=64):
    from PIL import Image, ImageDraw
    s = size * 4
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 4, fill=(29, 33, 39, 255))
    cx, cy = s * 0.30, s * 0.70
    for i, r in enumerate((0.30, 0.50, 0.70)):
        R = s * r
        w = max(2, int(s * 0.055))
        d.arc((cx - R, cy - R, cx + R, cy + R), 270, 360, fill=(79, 182, 201, 255 - i * 55), width=w)
    r0 = s * 0.075
    d.ellipse((cx - r0, cy - r0, cx + r0, cy + r0), fill=(232, 176, 74, 255))
    return im.resize((size, size), Image.LANCZOS)


# ---------- sound (draft: soft two-note chime, pending the user's pick) ----------
def make_chime(path, volume, name="soft-chime"):
    """Scale the bundled chime (picked by the user from the sound round) to the volume setting."""
    amp = 0.1 * (max(0.0, min(1.0, volume)) ** 2) * 2  # peak ~ -20 dBFS at default 0.5
    with wave.open(os.path.join(HERE, "sounds", name + ".wav")) as w:
        sr, raw = w.getframerate(), w.readframes(w.getnframes())
    vals = struct.unpack("<%dh" % (len(raw) // 2), raw)
    pk = max(1, max(abs(v) for v in vals))
    data = struct.pack("<%dh" % len(vals), *[int(v / pk * amp * 32767) for v in vals])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(data)


# ---------- chat lookup ----------
class Chats:
    """name -> local session id, from Claude's own session files and the scope files. Cached by mtime."""
    def __init__(self):
        self.sess = {}      # id -> dict(title, archived, last)
        self.scope = {}     # lower chat name -> id
        self.stamp = 0

    def refresh(self, force=False):
        if not force and time.time() - self.stamp < 20:
            return
        self.stamp = time.time()
        sess = {}
        try:
            roots = [(r, d, f, sd) for sd in sessions_dirs() for r, d, f in os.walk(sd)]
            for root, _d, files, sd in roots:
                if root.count(os.sep) - sd.count(os.sep) > 2:
                    continue
                for fn in files:
                    if fn.startswith("local_") and fn.endswith(".json"):
                        p = os.path.join(root, fn)
                        try:
                            with open(p, encoding="utf-8") as f:
                                head = f.read(2500)
                        except OSError:
                            continue
                        sid = re.search(r'"sessionId":"(local_[^"]+)"', head)
                        if not sid:
                            continue
                        title = re.search(r'"title":"((?:[^"\\]|\\.)*)"', head)
                        la = re.search(r'"lastActivityAt":(\d+)', head)
                        sess[sid.group(1)] = {"title": json.loads('"%s"' % title.group(1)) if title else "",
                                              "archived": '"isArchived":true' in head,
                                              "last": int(la.group(1)) if la else 0}
        except OSError as e:
            log(f"chats scan: {e}")
        self.sess = sess
        sc = {}
        sd = os.path.join(MCW, "kb", "scope")
        try:
            for fn in os.listdir(sd):
                if fn.endswith(".md"):
                    with open(os.path.join(sd, fn), encoding="utf-8", errors="replace") as f:
                        t = f.read(3000)
                    m = re.search(r"^session:\s*(local_[A-Za-z0-9-]+)", t, re.M)
                    if m:
                        sc[fn[:-3].lower()] = m.group(1)
        except OSError:
            pass
        self.scope = sc

    def resolve(self, name):
        self.refresh()
        n = (name or "").strip().lower()
        if not n:
            return ""
        best, bs, bl = "", 0, 0
        for sid, x in self.sess.items():
            t = x["title"].strip().lower()
            if not t or x["archived"]:
                continue
            sc = 2 if t == n else 1 if (n in t or t in n) and min(len(n), len(t)) >= 4 else 0
            if sc and (sc, x["last"]) > (bs, bl):
                best, bs, bl = sid, sc, x["last"]
        if best:
            return best
        return self.scope.get(n, "")

    def title(self, sid):
        return self.sess.get(sid, {}).get("title", "")


def open_chat(sid):
    url = f"claude://code/continue?session={sid}&source=desktop_action" if sid else \
        "claude://code/needs-input?source=desktop_action"
    try:
        os.startfile(url)
    except OSError as e:
        log(f"open_chat {url}: {e}")


# ---------- tracker data ----------
def read_tail(path, n, maxbytes=60000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            sz = f.tell()
            f.seek(max(0, sz - maxbytes))
            return f.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return []


def last_activity(owner, acts):
    key = re.split(r"\s+-\s+|:", owner)[0].strip().lower()
    if not key:
        return ""
    for line in reversed(acts):
        m = re.match(r"\d\d-\d\d \d\d:\d\d ([^:]+):\s*(.*)", line)
        if m and m.group(1).strip().lower() == key:
            return m.group(2)[:140]
    return ""


def gather_tracker():
    out = {"held": [], "waiting": [], "playing": None, "inbox": [], "acts": [], "playtest": 0}
    q = {}
    txt = run_quiet([sys.executable.replace("pythonw", "python"), "-W", "ignore", "-I",
                     os.path.join(MCW, "scripts", "benchlock.py"), "queue"])
    try:
        q = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
    except ValueError:
        pass
    acts = read_tail(os.path.join(MCW, "ACTIVITY.log"), 400)
    out["acts"] = acts[-6:]
    holders = ([q["held"]] if isinstance(q.get("held"), dict) else []) + list(q.get("shared") or [])
    for h in holders:
        o = h.get("owner", "?")
        out["held"].append({"owner": o, "what": last_activity(o, acts), "eta": h.get("expected_end"),
                            "copy": h.get("copy")})
    for w in q.get("waiting") or []:
        o = w.get("owner", "?")
        out["waiting"].append({"owner": o, "what": last_activity(o, acts)})
    out["playing"] = q.get("user_playing")
    try:
        with open(os.path.join(MCW, "USER-INBOX.md"), encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r"\s*(\d+)\.\s+\[ \]\s*(.*)", line)
                if m:
                    out["inbox"].append(m.group(2).strip())
    except OSError:
        pass
    try:
        with open(os.path.join(MCW, "PLAYTEST-QUEUE.md"), encoding="utf-8", errors="replace") as f:
            out["playtest"] = sum(1 for l in f if l.startswith("- ["))
    except OSError:
        pass
    return out


# ---------- window helpers ----------
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = 0x08000000, 0x80, 0x8
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE NOMOVE NOACTIVATE


def hwnd_of(w):
    return user32.GetParent(w.winfo_id()) or w.winfo_id()


def no_activate(w):
    h = hwnd_of(w)
    st = user32.GetWindowLongW(h, GWL_EXSTYLE)
    user32.SetWindowLongW(h, GWL_EXSTYLE, st | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    return h


def show_noactivate(w):
    h = no_activate(w)
    user32.ShowWindow(h, 4)  # SW_SHOWNOACTIVATE
    user32.SetWindowPos(h, wintypes.HWND(-1), 0, 0, 0, 0, SWP_FLAGS)


def work_area():
    r = wintypes.RECT()
    user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


# ---------- app ----------
class App:
    def __init__(self):
        os.makedirs(DATA, exist_ok=True)
        self.cfg = {**DEFAULTS, **load_json(CFG, {})}
        self.hist = load_json(HIST, [])
        self.chats = Chats()
        self.cards = []
        self.seen = {}
        self.todo = _q.Queue()
        self.panel = None
        self.update_url = ""
        self.tracker = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP)
        self.sound = os.path.join(DATA, "chime.wav")
        self.tk_scale = self.root.winfo_fpixels("1i") / 96.0
        self.icon_img = make_icon(64)
        self.apply_volume()
        self.start_tray()
        threading.Thread(target=self.listen, daemon=True).start()
        self.root.after(500, self.tick)
        self.root.after(5000, self.check_update)

    # -- config
    def save(self):
        atomic_write(CFG, json.dumps(self.cfg, indent=1))

    def apply_volume(self):
        try:
            make_chime(self.sound, self.cfg["volume"], self.cfg.get("chime", "soft-chime"))
        except Exception as e:
            log(f"chime: {e}")

    def play(self):
        if self.cfg["mute"]:
            return
        try:
            import winsound
            winsound.PlaySound(self.sound, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception as e:
            log(f"sound: {e}")

    def save_hist(self):
        atomic_write(HIST, json.dumps(self.hist[-60:]))

    # -- single instance + commands
    def listen(self):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", PORT)); s.listen(2)
        except OSError as e:
            log(f"listen: {e}"); return
        while True:
            c, _ = s.accept()
            try:
                cmd = c.recv(64).decode().strip()
                self.post(lambda cmd=cmd: self.command(cmd))
            finally:
                c.close()

    def post(self, fn):
        try:
            self.root.after(0, fn)
        except Exception:
            self.todo.put(fn)

    def command(self, cmd):
        if cmd == "show":
            self.show_panel()
        elif cmd == "test":
            self.add_note({"chat": "Command Center", "text": "Test notification. Click opens this chat.",
                           "kind": "ready", "session": "", "ts": time.time()}, force=True)
        elif cmd == "quit":
            self.quit()

    # -- tray
    def start_tray(self):
        import pystray
        m = pystray.Menu(
            pystray.MenuItem("Open", lambda i, it: self.post(self.show_panel), default=True),
            pystray.MenuItem("Mute sound", lambda i, it: self.post(self.toggle_mute), checked=lambda it: self.cfg["mute"]),
            pystray.MenuItem("Pause popups", lambda i, it: self.post(self.toggle_pause), checked=lambda it: self.cfg["pause"]),
            pystray.MenuItem(lambda it: f"Update available: {self.update_url and 'open page'}",
                             lambda i, it: os.startfile(self.update_url), visible=lambda it: bool(self.update_url)),
            pystray.MenuItem("Quit", lambda i, it: self.post(self.quit)))
        self.tray = pystray.Icon(APP, self.icon_img, APP, m)
        self.tray.run_detached()

    def toggle_mute(self):
        self.cfg["mute"] = not self.cfg["mute"]; self.save()

    def toggle_pause(self):
        self.cfg["pause"] = not self.cfg["pause"]; self.save()

    def quit(self):
        try:
            self.tray.stop()
        except Exception:
            pass
        self.root.quit()

    # -- inbox polling (a directory listing every 2 s; nothing else runs while idle)
    def tick(self):
        try:
            self._tick()
        except Exception:
            import traceback
            log("tick " + traceback.format_exc())
        self.root.after(2000, self.tick)

    def _tick(self):
        try:
            names = sorted(n for n in os.listdir(INBOX) if n.endswith(".json"))
        except OSError:
            names = []
        new = 0
        for n in names:
            p = os.path.join(INBOX, n)
            d = load_json(p, None)
            try:
                os.makedirs(PROC, exist_ok=True)
                os.replace(p, os.path.join(PROC, n))
                old = sorted(os.listdir(PROC))[:-100]
                for o in old:  # keep the last 100 processed notices
                    os.replace(os.path.join(PROC, o), os.path.join(PROC, o + ".old"))
            except OSError:
                pass
            if isinstance(d, dict) and d.get("text"):
                new += self.add_note(d, play=False)
        if new:
            self.play()
        while not self.todo.empty():
            self.todo.get()()

    def add_note(self, d, play=True, force=False):
        key = (d.get("chat", ""), d.get("text", ""))
        now = time.time()
        if not force and now - self.seen.get(key, 0) < 600:
            return 0
        self.seen[key] = now
        sid = d.get("session") or self.chats.resolve(d.get("chat", ""))
        note = {"chat": d.get("chat", "?"), "text": d["text"], "kind": d.get("kind", "question"),
                "session": sid, "ts": d.get("ts", now), "ack": False}
        self.hist.append(note); self.save_hist()
        if not self.cfg["pause"]:
            self.show_card(note)
        if self.panel:
            self.refresh_panel()
        if play:
            self.play()
        return 1

    # -- popups
    def geometry_origin(self):
        l, t, r, b = work_area()
        return l, t, r, b

    def card_size(self):
        k = self.cfg["scale"] * self.tk_scale
        return int(360 * k), k

    def show_card(self, note, sample=False):
        if len(self.cards) >= 4:
            self.dismiss(self.cards[0])
        W, k = self.card_size()
        top = tk.Toplevel(self.root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.attributes("-alpha", self.cfg["opacity"])
        col, label = KIND.get(note["kind"], KIND["question"])
        top.configure(bg=BG)
        bar = tk.Frame(top, bg=col, width=int(5 * k))
        bar.pack(side="left", fill="y")
        body = tk.Frame(top, bg=BG)
        body.pack(side="left", fill="both", expand=True, padx=int(10 * k), pady=int(8 * k))
        f1 = ("Segoe UI Semibold", max(8, int(10 * self.cfg["scale"])))
        f2 = ("Segoe UI", max(8, int(9 * self.cfg["scale"])))
        head = tk.Frame(body, bg=BG); head.pack(fill="x")
        tk.Label(head, text=note["chat"], font=f1, fg=FG, bg=BG, anchor="w").pack(side="left")
        tk.Label(head, text="  " + label, font=f2, fg=col, bg=BG).pack(side="left")
        x = tk.Label(head, text="\u2715", font=f2, fg=DIM, bg=BG, cursor="hand2"); x.pack(side="right")
        tk.Label(body, text=note["text"], font=f2, fg=FG, bg=BG, wraplength=W - int(40 * k), justify="left",
                 anchor="w").pack(fill="x", pady=(2, 0))
        card = {"win": top, "note": note, "job": None, "hover": False}
        for w in [top, body, head] + list(body.winfo_children()) + list(head.winfo_children()):
            w.bind("<Button-1>", lambda e, c=card: self.click_card(c))
            w.bind("<Button-3>", lambda e, c=card: self.dismiss(c))
            w.bind("<Enter>", lambda e, c=card: c.__setitem__("hover", True))
            w.bind("<Leave>", lambda e, c=card: c.__setitem__("hover", False))
        x.bind("<Button-1>", lambda e, c=card: (self.dismiss(c), "break")[1])
        top.update_idletasks()
        card["h"] = top.winfo_reqheight()
        card["w"] = W
        if not sample:
            self.cards.append(card)
        self.layout()
        show_noactivate(top)
        top.update_idletasks()
        self.layout()
        if not sample:
            card["job"] = self.root.after(int(self.cfg["linger"] * 1000), lambda c=card: self.fade(c))
        return card

    def anchor(self, h_total):
        l, t, r, b = work_area()
        W, k = self.card_size()
        x = self.cfg["x"] if self.cfg["x"] is not None else r - W - int(16 * k)
        y = self.cfg["y"] if self.cfg["y"] is not None else b - h_total - int(16 * k)
        return max(l, min(x, r - W)), max(t, min(y, b - 40))

    def layout(self):
        gap = 8
        total = sum(c["h"] + gap for c in self.cards)
        x, y = self.anchor(total)
        for c in self.cards:
            c["win"].geometry(f"{c['w']}x{c['h']}+{x}+{y}")
            y += c["h"] + gap

    def click_card(self, card):
        self.ack(card["note"])
        self.dismiss(card)
        open_chat(card["note"].get("session"))

    def ack(self, note):
        note["ack"] = True; self.save_hist()
        if self.panel:
            self.refresh_panel()

    def fade(self, card, step=0):
        if card not in self.cards:
            return
        if card["hover"]:
            card["job"] = self.root.after(1500, lambda: self.fade(card)); return
        a = self.cfg["opacity"] * (1 - step / 8)
        if step >= 8:
            self.dismiss(card); return
        try:
            card["win"].attributes("-alpha", max(0.02, a))
        except tk.TclError:
            return
        card["job"] = self.root.after(60, lambda: self.fade(card, step + 1))

    def dismiss(self, card):
        if card in self.cards:
            self.cards.remove(card)
        try:
            card["win"].destroy()
        except tk.TclError:
            pass
        self.layout()

    # -- panel
    def show_panel(self):
        if self.panel:
            self.panel.deiconify(); self.panel.lift(); self.refresh_panel(); return
        p = tk.Toplevel(self.root)
        self.panel = p
        p.title(APP)
        p.configure(bg=BG)
        p.geometry(self.cfg.get("panel") or "460x640")
        p.minsize(380, 360)
        try:
            from PIL import ImageTk
            self._ico = ImageTk.PhotoImage(self.icon_img)
            p.iconphoto(True, self._ico)
        except Exception:
            pass
        p.protocol("WM_DELETE_WINDOW", self.hide_panel)
        p.bind("<Configure>", self._panel_geom)
        self.canvas = tk.Canvas(p, bg=BG, highlightthickness=0)
        sb = tk.Scrollbar(p, command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y"); self.canvas.pack(side="left", fill="both", expand=True)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.win_id = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.win_id, width=e.width))
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 * (e.delta // 120), "units"))
        self.refresh_panel()

    def _panel_geom(self, e):
        if e.widget is self.panel and self.panel.state() == "normal":
            self.cfg["panel"] = self.panel.geometry()
            if getattr(self, "_gj", None):
                self.root.after_cancel(self._gj)
            self._gj = self.root.after(800, self.save)

    def hide_panel(self):
        p, self.panel = self.panel, None
        if getattr(self, "_rj", None):
            self.root.after_cancel(self._rj)
        self.canvas.unbind_all("<MouseWheel>")
        p.destroy()

    def refresh_panel(self):
        if not self.panel:
            return
        if getattr(self, "_rj", None):
            self.root.after_cancel(self._rj)
        # collect tracker data off the UI thread
        def work():
            self.tracker = gather_tracker()
            self.post(self.draw_panel)
        threading.Thread(target=work, daemon=True).start()
        self._rj = self.root.after(10000, self.refresh_panel)

    def draw_panel(self):
        if not self.panel:
            return
        self.chats.refresh()
        for w in self.inner.winfo_children():
            w.destroy()
        t = self.tracker or {}
        F = lambda s, b=False: ("Segoe UI Semibold" if b else "Segoe UI", s)

        def section(title, count=None):
            f = tk.Frame(self.inner, bg=BG); f.pack(fill="x", padx=14, pady=(14, 2))
            tk.Label(f, text=title.upper(), font=F(8, True), fg=ACC, bg=BG).pack(side="left")
            if count is not None:
                tk.Label(f, text=str(count), font=F(8), fg=DIM, bg=BG).pack(side="left", padx=6)
            return f

        def row(head, text="", sid=None, color=None, action=None):
            f = tk.Frame(self.inner, bg=BG2); f.pack(fill="x", padx=14, pady=2)
            if color:
                tk.Frame(f, bg=color, width=4).pack(side="left", fill="y")
            b = tk.Frame(f, bg=BG2); b.pack(side="left", fill="x", expand=True, padx=8, pady=5)
            tk.Label(b, text=head, font=F(9, True), fg=FG, bg=BG2, anchor="w").pack(fill="x")
            if text:
                tk.Label(b, text=text, font=F(9), fg=DIM, bg=BG2, anchor="w", justify="left", wraplength=360).pack(fill="x")
            if action or sid:
                for w in [f, b] + list(b.winfo_children()):
                    w.configure(cursor="hand2")
                    w.bind("<Button-1>", lambda e, a=action, s=sid: a() if a else open_chat(s))

        def empty(s):
            tk.Label(self.inner, text=s, font=F(9), fg=DIM, bg=BG, anchor="w").pack(fill="x", padx=18)

        if self.update_url:
            row("Update available", "Click to open the release page.", action=lambda: os.startfile(self.update_url), color=ACC)

        pend = [n for n in reversed(self.hist) if not n.get("ack")]
        section("Needs you", len(pend))
        for n in pend[:10]:
            col = KIND.get(n["kind"], KIND["question"])[0]
            row(f"{n['chat']}  \u00b7  {time.strftime('%H:%M', time.localtime(n['ts']))}", n["text"],
                color=col, action=lambda n=n: (self.ack(n), open_chat(n.get("session"))))
        if not pend:
            empty("Nothing waiting.")
        if pend:
            b = tk.Label(self.inner, text="Clear all", font=F(8), fg=DIM, bg=BG, cursor="hand2")
            b.pack(anchor="e", padx=16); b.bind("<Button-1>", lambda e: ([n.__setitem__("ack", True) for n in self.hist], self.save_hist(), self.draw_panel()))

        ib = t.get("inbox", [])
        section("Chats waiting on your input (Underling's list)", len(ib))
        for s in ib[:8]:
            name = re.split(r"[ :\[]", s, 1)[0]
            row(s[:120], "", sid=self.chats.resolve(re.match(r"([A-Z][\w ]{2,30}?)(?: [A-Z]{4,}|:|\()", s).group(1) if re.match(r"([A-Z][\w ]{2,30}?)(?: [A-Z]{4,}|:|\()", s) else name))
        if not ib:
            empty("USER-INBOX has no open items.")
        if t.get("playtest"):
            empty(f"Playtest queue: {t['playtest']} entries (PLAYTEST-QUEUE.md)")

        section("Running now", len(t.get("held", [])))
        for h in t.get("held", []):
            eta = f"  \u00b7  until ~{h['eta']}" if h.get("eta") else ""
            row(h["owner"] + eta, h["what"] or "running a test", sid=self.chats.resolve(re.split(r" - ", h["owner"])[0]), color="#5fb88a")
        if t.get("playing"):
            row("You are playing", str(t["playing"]), color=ACC)
        if not t.get("held") and not t.get("playing"):
            empty("No tests running.")

        section("Test queue", len(t.get("waiting", [])))
        for i, w in enumerate(t.get("waiting", []), 1):
            row(f"{i}. {w['owner']}", w["what"] or "waiting for a bench slot", sid=self.chats.resolve(re.split(r" - ", w["owner"])[0]), color="#e8b04a")
        if not t.get("waiting"):
            empty("Queue is empty.")

        section("Recent activity")
        for a in reversed(t.get("acts", [])):
            tk.Label(self.inner, text=a[:150], font=F(8), fg=DIM, bg=BG, anchor="w", justify="left",
                     wraplength=400).pack(fill="x", padx=18, pady=1)

        section("Settings")
        self.settings_block()
        tk.Label(self.inner, text=f"{APP} {VERSION}  \u00b7  GPL-3.0  \u00b7  Made with AI assistance.", font=F(8),
                 fg=DIM, bg=BG).pack(pady=14)

    def settings_block(self):
        f = tk.Frame(self.inner, bg=BG); f.pack(fill="x", padx=14)

        def slider(label, key, lo, hi, step, fmt, on=None):
            r = tk.Frame(f, bg=BG); r.pack(fill="x", pady=2)
            tk.Label(r, text=label, width=13, anchor="w", font=("Segoe UI", 9), fg=FG, bg=BG).pack(side="left")
            var = tk.DoubleVar(value=self.cfg[key])
            val = tk.Label(r, text=fmt(self.cfg[key]), width=6, font=("Segoe UI", 9), fg=DIM, bg=BG)
            val.pack(side="right")

            def ch(v):
                self.cfg[key] = round(float(v), 2); val.config(text=fmt(self.cfg[key])); self.save()
                if on: on()
            s = tk.Scale(r, from_=lo, to=hi, resolution=step, orient="horizontal", variable=var, showvalue=False,
                         bg=BG, fg=FG, troughcolor=BG2, highlightthickness=0, bd=0, command=ch, length=200)
            s.pack(side="right", padx=4)
            return s

        def release_preview(_e=None):
            self.preview()
        s1 = slider("Transparency", "opacity", 0.3, 1.0, 0.01, lambda v: f"{int((1 - v) * 100)}%", self.preview)
        s2 = slider("Size", "scale", 0.7, 2.0, 0.05, lambda v: f"{int(v * 100)}%", self.preview)
        s3 = slider("Show for (s)", "linger", 4, 40, 1, lambda v: f"{int(v)}s")
        s4 = slider("Volume", "volume", 0.0, 1.0, 0.05, lambda v: f"{int(v * 100)}%")
        s4.bind("<ButtonRelease-1>", lambda e: (self.apply_volume(), self.play()))
        btns = tk.Frame(f, bg=BG); btns.pack(fill="x", pady=6)

        def btn(text, cmd):
            b = tk.Label(btns, text=text, font=("Segoe UI", 9), fg=FG, bg=BG2, padx=10, pady=4, cursor="hand2")
            b.pack(side="left", padx=(0, 6)); b.bind("<Button-1>", lambda e: cmd())
        btn("Move popups\u2026", self.move_mode)
        btn("Test notification", lambda: self.command("test"))
        btn("Reset position", lambda: (self.cfg.update(x=None, y=None), self.save()))
        chk = tk.Frame(f, bg=BG); chk.pack(fill="x")
        for text, key in (("Mute sound", "mute"), ("Pause popups", "pause")):
            v = tk.BooleanVar(value=self.cfg[key])
            tk.Checkbutton(chk, text=text, variable=v, bg=BG, fg=FG, selectcolor=BG2, activebackground=BG,
                           activeforeground=FG, command=lambda k=key, v=v: (self.cfg.__setitem__(k, v.get()), self.save())
                           ).pack(side="left", padx=(0, 10))
        v = tk.BooleanVar(value=self.cfg["autostart"])
        tk.Checkbutton(chk, text="Start with Windows", variable=v, bg=BG, fg=FG, selectcolor=BG2, activebackground=BG,
                       activeforeground=FG, command=lambda: self.set_autostart(v.get())).pack(side="left")

    def preview(self):
        if getattr(self, "_pv", None):
            self.dismiss_sample()
        self._pv = self.show_card({"chat": "Preview", "text": "This is how notifications look.", "kind": "ready"}, sample=True)
        self._pv_job = self.root.after(1800, self.dismiss_sample)

    def dismiss_sample(self):
        pv, self._pv = getattr(self, "_pv", None), None
        if pv:
            try:
                pv["win"].destroy()
            except tk.TclError:
                pass

    def set_autostart(self, on):
        self.cfg["autostart"] = on; self.save()
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0,
                                winreg.KEY_SET_VALUE) as k:
                if on:
                    pw = sys.executable.replace("python.exe", "pythonw.exe")
                    winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, f'"{pw}" "{os.path.abspath(__file__)}" --tray')
                else:
                    try:
                        winreg.DeleteValue(k, APP)
                    except FileNotFoundError:
                        pass
        except OSError as e:
            log(f"autostart: {e}")

    # -- drag mode: a sample card follows the mouse; click Done / press Esc to keep it
    def move_mode(self):
        W, k = self.card_size()
        card = self.show_card({"chat": "Drag me", "text": "Drag to where popups should appear, then click the Done bar.",
                               "kind": "ready"}, sample=True)
        win = card["win"]
        win.geometry(f"{card['w']}x{card['h']}+{self.anchor(card['h'])[0]}+{self.anchor(card['h'])[1]}")
        st = {}
        done = tk.Toplevel(self.root); done.overrideredirect(True); done.attributes("-topmost", True)
        tk.Label(done, text="Done \u2713 keep this position", font=("Segoe UI Semibold", 10), fg="#101418", bg=ACC,
                 padx=14, pady=6, cursor="hand2").pack()

        def place_done():
            done.geometry(f"+{win.winfo_x()}+{win.winfo_y() - 36}")

        def press(e): st["dx"], st["dy"] = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()
        def drag(e):
            win.geometry(f"+{e.x_root - st['dx']}+{e.y_root - st['dy']}"); place_done()

        def finish(_e=None):
            self.cfg["x"], self.cfg["y"] = win.winfo_x(), win.winfo_y(); self.save()
            win.destroy(); done.destroy()
        for w in [win] + win.winfo_children() + [c for ch in win.winfo_children() for c in ch.winfo_children()]:
            w.bind("<ButtonPress-1>", press); w.bind("<B1-Motion>", drag); w.configure(cursor="fleur")
        for w in [done] + done.winfo_children():
            w.bind("<Button-1>", finish)
        win.update_idletasks(); place_done()

    # -- updater: checks GitHub releases when updateRepo is set; never installs silently
    def check_update(self):
        repo = self.cfg.get("updateRepo", "").strip()
        if repo:
            def work():
                try:
                    import urllib.request
                    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/releases/latest",
                                                 headers={"User-Agent": APP})
                    d = json.load(urllib.request.urlopen(req, timeout=6))
                    tag = d.get("tag_name", "").lstrip("v")
                    if tag and tuple(map(int, re.findall(r"\d+", tag))) > tuple(map(int, re.findall(r"\d+", VERSION))):
                        self.update_url = d.get("html_url", "")
                except Exception as e:
                    log(f"update check: {e}")
            threading.Thread(target=work, daemon=True).start()
        self.root.after(30 * 60 * 1000, self.check_update)

    def run(self):
        if "--tray" not in sys.argv:
            self.root.after(300, self.show_panel)
        self.root.mainloop()


def send(cmd):
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=2) as s:
            s.sendall(cmd.encode())
        return True
    except OSError:
        return False


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    cmd = next((a[6:] for a in sys.argv[1:] if a.startswith("--cmd=")), None)
    if cmd:
        sys.exit(0 if send(cmd) else 1)
    if send("show" if "--tray" not in sys.argv else "noop"):
        return  # single instance: the running one handles it
    try:
        App().run()
    except Exception:
        import traceback
        log("CRASH " + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
