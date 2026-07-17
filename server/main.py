from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from typing import Optional
import os
import asyncio
import httpx
from datetime import datetime
import uuid

app = FastAPI()

# --- State ---
state = {
    "active": False,
    "pending_commands": [],
    "last_seen": None,
    "command_callbacks": {},
}

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")
DEVICE_SECRET = os.environ.get("DEVICE_SECRET", "secret123")
SELF_URL = os.environ.get("SELF_URL", "")


# --- Keep alive ---
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


# --- Telegram ---
async def send_tg(chat_id: str, text: str):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    except Exception as e:
        print(f"send_tg error: {e}")


def phone_online() -> bool:
    if not state["last_seen"]:
        return False
    return (datetime.now() - state["last_seen"]).total_seconds() < 120


def last_seen_str() -> str:
    if not state["last_seen"]:
        return "никогда"
    delta = int((datetime.now() - state["last_seen"]).total_seconds())
    if delta < 60:
        return f"{delta} сек назад"
    return f"{delta // 60} мин назад"


async def enqueue_command(chat_id: str, cmd: dict, description: str):
    if not phone_online():
        await send_tg(chat_id, f"⚠️ Телефон оффлайн (последний ping: {last_seen_str()})\nКоманда добавлена в очередь — выполнится когда телефон появится.")
    else:
        await send_tg(chat_id, f"📡 Телефон онлайн (ping: {last_seen_str()})\nОтправляю команду...")

    cmd_id = str(uuid.uuid4())[:8]
    cmd["_id"] = cmd_id
    state["pending_commands"].append(cmd)
    state["command_callbacks"][cmd_id] = chat_id

    await send_tg(chat_id, f"✅ Команда `{description}` в очереди (ID: `{cmd_id}`)\nЖду подтверждения от телефона...")


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
        await send_tg(chat_id, (
            "📱 *Phone Control Bot*\n\n"
            "/on — включить режим управления\n"
            "/off — выключить режим управления\n"
            "/shutdown — заблокировать экран\n"
            "/dnd — отключить «Не беспокоить»\n"
            "/msg <текст> — показать сообщение на экране\n"
            "/status — статус подключения"
        ))

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
            await enqueue_command(chat_id, {"cmd": "show_message", "text": message}, "показать сообщение")

    elif text == "/status":
        online = "🟢 Онлайн" if phone_online() else "🔴 Оффлайн"
        mode = "🟢 Активен (каждые 10 сек)" if state["active"] else "🔴 Спящий (каждую минуту)"
        await send_tg(chat_id, (
            f"📊 *Статус*\n"
            f"Телефон: {online} (ping: {last_seen_str()})\n"
            f"Режим: {mode}\n"
            f"Очередь команд: {len(state['pending_commands'])}"
        ))

    else:
        await send_tg(chat_id, "❓ Неизвестная команда. Напиши /help")


# --- Webhook ---
@app.post("/webhook")
async def webhook(update: dict):
    asyncio.create_task(process_update(update))
    return {"ok": True}


# --- Poll ---
@app.get("/poll")
async def poll(x_device_secret: Optional[str] = Header(None)):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    state["last_seen"] = datetime.now()

    if state["pending_commands"]:
        cmd = state["pending_commands"].pop(0)
        cmd_id = cmd.get("_id")

        if cmd_id and cmd_id in state["command_callbacks"]:
            chat_id = state["command_callbacks"].pop(cmd_id)
            asyncio.create_task(send_tg(chat_id, f"📲 Телефон получил команду `{cmd.get('cmd')}` (ID: `{cmd_id}`) и выполняет её."))

        return {"active": state["active"], "command": cmd}

    return {"active": state["active"], "command": None}


@app.get("/health")
async def health():
    return {"status": "ok"}
