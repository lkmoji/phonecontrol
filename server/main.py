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
    "active": False,           # режим активного polling включён?
    "pending_commands": [],    # очередь команд для телефона
    "last_seen": None,
}

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED_CHAT_ID = os.environ.get("ALLOWED_CHAT_ID", "")  # твой Telegram ID
DEVICE_SECRET = os.environ.get("DEVICE_SECRET", "secret123")  # секрет для APK


def check_device(secret: str = Header(None, alias="X-Device-Secret")):
    if secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")


# --- Telegram Bot ---
async def send_tg(chat_id: str, text: str):
    if not BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": chat_id, "text": text})


async def process_update(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()

    # Проверка доступа
    if ALLOWED_CHAT_ID and chat_id != ALLOWED_CHAT_ID:
        await send_tg(chat_id, "⛔ Нет доступа.")
        return

    if text == "/start" or text == "/help":
        help_text = (
            "📱 *Phone Control Bot*\n\n"
            "/on — включить режим управления (частый polling)\n"
            "/off — выключить режим управления\n"
            "/shutdown — выключить телефон\n"
            "/dnd — отключить режим «Не беспокоить»\n"
            "/msg <текст> — показать сообщение на экране\n"
            "/status — статус подключения"
        )
        await send_tg(chat_id, help_text)

    elif text == "/on":
        state["active"] = True
        state["pending_commands"] = []
        await send_tg(chat_id, "✅ Режим управления *включён*. Телефон начнёт слушать каждые 10 сек.")

    elif text == "/off":
        state["active"] = False
        state["pending_commands"] = []
        await send_tg(chat_id, "🔕 Режим управления *выключен*.")

    elif text == "/shutdown":
        if not state["active"]:
            await send_tg(chat_id, "⚠️ Сначала включи режим командой /on")
        else:
            state["pending_commands"].append({"cmd": "shutdown"})
            await send_tg(chat_id, "📴 Команда выключения отправлена.")

    elif text == "/dnd":
        if not state["active"]:
            await send_tg(chat_id, "⚠️ Сначала включи режим командой /on")
        else:
            state["pending_commands"].append({"cmd": "dnd_off"})
            await send_tg(chat_id, "🔔 Команда отключения «Не беспокоить» отправлена.")

    elif text.startswith("/msg "):
        if not state["active"]:
            await send_tg(chat_id, "⚠️ Сначала включи режим командой /on")
        else:
            message = text[5:].strip()
            state["pending_commands"].append({"cmd": "show_message", "text": message})
            await send_tg(chat_id, f"💬 Сообщение «{message}» будет показано на экране.")

    elif text == "/status":
        last = state["last_seen"]
        last_str = last.strftime("%H:%M:%S") if last else "никогда"
        mode = "🟢 Активен (каждые 10 сек)" if state["active"] else "🔴 Спящий (каждую минуту)"
        await send_tg(
            chat_id,
            f"📊 *Статус*\nРежим: {mode}\nПоследний ping: {last_str}\nОчередь команд: {len(state['pending_commands'])}"
        )


# --- Webhook endpoint для Telegram ---
class TelegramUpdate(BaseModel):
    class Config:
        extra = "allow"

@app.post("/webhook")
async def webhook(update: dict):
    asyncio.create_task(process_update(update))
    return {"ok": True}


# --- Endpoints для Android APK ---

@app.get("/poll")
async def poll(x_device_secret: Optional[str] = Header(None)):
    if x_device_secret != DEVICE_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    state["last_seen"] = datetime.now()

    if state["pending_commands"]:
        cmd = state["pending_commands"].pop(0)
        return {
            "active": state["active"],
            "command": cmd
        }

    return {
        "active": state["active"],
        "command": None
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
