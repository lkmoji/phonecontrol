from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
import os
import asyncio
import httpx
from datetime import datetime

app = FastAPI()

# --- State ---
state = {
    "active": False,
    "pending_commands": [],
    "last_seen": None,
    "command_callbacks": {},  # cmd_id -> chat_id
}

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")
DEVICE_SECRET = os.environ.get("DEVICE_SECRET", "secret123")

import uuid

# --- Telegram Bot ---
async def send_tg(chat_id: str, text: str, parse_mode: str = "Markdown"):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})
            return resp.json()
    except Exception as e:
        print(f"send_tg error: {e}")


def phone_online() -> bool:
    if not state["last_seen"]:
        return False
    delta = (datetime.now() - state["last_seen"]).total_seconds()
    return delta < 120  # считаем онлайн если ping был < 2 минут назад


def last_seen_str() -> str:
    if not state["last_seen"]:
        return "никогда"
    delta = int((datetime.now() - state["last_seen"]).total_seconds())
    if delta < 60:
        return f"{delta} сек назад"
    return f"{delta // 60} мин назад"


async def enqueue_command(chat_id: str, cmd: dict, description: str):
    """Добавляет команду в очередь и сообщает о каждом этапе."""

    # Этап 1: проверка что телефон онлайн
    if not phone_online():
        last = last_seen_str()
        await send_tg(chat_id, f"⚠️ Телефон оффлайн (последний ping: {last})\nКоманда добавлена в очередь — выполнится когда телефон появится.")
    else:
        await send_tg(chat_id, f"📡 Телефон онлайн (ping: {last_seen_str()})\nОтправляю команду...")

    # Этап 2: добавляем в очередь с уникальным ID
    cmd_id = str(uuid.uuid4())[:8]
    cmd["_id"] = cmd_id
    state["pending_commands"].append(cmd)
    state["command_callbacks"][cmd_id] = chat_id

    await send_tg(chat_id, f"✅ Команда `{description}` добавлена в очередь (ID: `{cmd_id}`)\nЖду подтверждения от телефона...")


async def process_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        await send_tg(chat_id, "⛔ Нет доступа.")
        return

    if text in ("/start", "/help"):
        help_text = (
            "📱 *Phone Control Bot*\n\n"
            "/on — включить режим управления (частый polling)\n"
            "/off — выключить режим управления\n"
            "/shutdown — заблокировать экран\n"
            "/dnd — отключить режим «Не беспокоить»\n"
            "/msg <текст> — показать сообщение на экране\n"
            "/status — статус подключения"
        )
        await send_tg(chat_id, help_text)

    elif text == "/on":
        state["active"] = True
        state["pending_commands"] = []
        online = "🟢 онлайн" if phone_online() else f"🔴 оффлайн (последний ping: {last_seen_str()})"
        await send_tg(chat_id, f"✅ Режим управления *включён*\nТелефон: {online}\nPolling каждые 10 сек.")

    elif text == "/off":
        state["active"] = False
        state["pending_commands"] = []
        await send_tg(chat_id, "🔕 Режим управления *выключен*. Очередь команд очищена.")

    elif text == "/shutdown":
        if not state["active"]:
            await send_tg(chat_id, "⚠️ Сначала включи режим командой /on")
        else:
            await enqueue_command(chat_id, {"cmd": "shutdown"}, "блокировка экрана")

    elif text == "/dnd":
        if not state["active"]:
            await send_tg(chat_id, "⚠️ Сначала включи режим командой /on")
        else:
            await enqueue_command(chat_id, {"cmd": "dnd_off"}, "отключить DnD")

    elif text.startswith("/msg "):
        if not state["active"]:
            await send_tg(chat_id, "⚠️ Сначала включи режим командой /on")
        else:
            message = text[5:].strip()
            if not message:
                await send_tg(chat_id, "⚠️ Укажи текст: /msg Привет")
                return
            await enqueue_command(chat_id, {"cmd": "show_message", "text": message}, f"показать сообщение")

    elif text == "/status":
        last = last_seen_str()
        online = "🟢 Онлайн" if phone_online() else "🔴 Оффлайн"
        mode = "🟢 Активен (каждые 10 сек)" if state["active"] else "🔴 Спящий (каждую минуту)"
        queue = len(state["pending_commands"])
        await send_tg(
            chat_id,
            f"📊 *Статус*\n"
            f"Телефон: {online} (последний ping: {last})\n"
            f"Режим: {mode}\n"
            f"Очередь команд: {queue}"
        )

    else:
        await send_tg(chat_id, "❓ Неизвестная команда. Напиши /help")


# --- Webhook ---
@app.post("/webhook")
async def webhook(update: dict):
    asyncio.create_task(process_update(update))
    return {"ok": True}


# --- Poll endpoint ---
@app.get("/poll")
async def poll(x_device_secret: Optional[str] = Header(None)):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    state["last_seen"] = datetime.now()

    if state["pending_commands"]:
        cmd = state["pending_commands"].pop(0)
        cmd_id = cmd.get("_id")

        # Уведомляем что команда получена телефоном
        if cmd_id and cmd_id in state["command_callbacks"]:
            chat_id = state["command_callbacks"].pop(cmd_id)
            asyncio.create_task(send_tg(chat_id, f"📲 Телефон получил команду `{cmd.get('cmd')}` (ID: `{cmd_id}`) и выполняет её."))

        return {"active": state["active"], "command": cmd}

    return {"active": state["active"], "command": None}


@app.get("/health")
async def health():
    return {"status": "ok"}
