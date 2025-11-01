"""
EmoBot - Telegram emotional/educational assistant
Adapted for Railway deployment (server-ready).

Identical functionality:
- Registration (/start)
- User persistence (JSON)
- OpenRouter chat connection
- Scheduled daily follow-ups

Requires environment variables:
  TELEGRAM_TOKEN
  OPENROUTER_API_KEY
"""

import asyncio
import json
import os
from datetime import datetime, date
import logging
import pytz

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ConversationHandler, ContextTypes
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
import httpx

# -------------------- Config --------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or "8238105603:AAGBIEiWVZD7EfSN8KN06FebIxsf1qD6apk"
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or "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"

if not TELEGRAM_TOKEN or not OPENROUTER_API_KEY:
    raise EnvironmentError(
        "❌ Falta configurar las variables TELEGRAM_TOKEN y OPENROUTER_API_KEY en Railway."
    )

DATA_DIR = "data"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
LOG_LEVEL = logging.INFO

# Timezone (user's timezone)
LOCAL_TZ = pytz.timezone("America/Lima")

TIMESLOT_HOUR = {"mañana": 8, "manana": 8, "tarde": 15, "noche": 21}

REGISTER_NAME, REGISTER_TIME, REGISTER_PERSONALITY = range(3)

# -------------------- Logging --------------------
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

# -------------------- Helpers for user storage --------------------
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump({}, f)

async def load_users():
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _load_users_sync)

def _load_users_sync():
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return {}

async def save_users(users: dict):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _save_users_sync, users)

def _save_users_sync(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

# -------------------- OpenRouter integration --------------------
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

async def openrouter_chat(user_id: str, user_message: str, personality: str, last_topic: str = None, history: list = None):
    system_prompt = (
        "Eres Peter, un asistente educativo y emocional masculino. "
        "Eres calmado, reflexivo y racional, pero empático. "
        "Usas un lenguaje sereno, motivador y lógico."
        if personality.lower().startswith("p") else
        "Eres Wuen, una asistente emocional y educativa femenina. "
        "Eres cálida, comprensiva y cercana. Hablas con ternura y empatía, "
        "ayudando a las personas a sentirse escuchadas y guiadas."
    )

    messages = [{"role": "system", "content": system_prompt}]
    if last_topic:
        messages.append({"role": "system", "content": f"Último tema del usuario: {last_topic}"})
    if history:
        messages.extend(history[-8:])
    messages.append({"role": "user", "content": user_message})

    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "gpt-4o-mini", "messages": messages, "max_tokens": 512, "temperature": 0.8}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(OPENROUTER_URL, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            content = None
            if "choices" in data and len(data["choices"]) > 0:
                choice = data["choices"][0]
                if "message" in choice and "content" in choice["message"]:
                    content = choice["message"]["content"]
                elif "text" in choice:
                    content = choice["text"]
            if not content:
                logger.error("Formato inesperado de OpenRouter: %s", data)
                return "Lo siento, tuve un problema al procesar la respuesta."
            return content.strip()
    except Exception as e:
        logger.exception("Error llamando a OpenRouter: %s", e)
        return "Lo siento, no puedo conectarme con el servicio de IA ahora mismo."

# -------------------- Topic extractor --------------------
def extract_topic_from_message(message: str) -> str:
    if not message:
        return ""
    for sep in ['.', '!', '?', '\n']:
        if sep in message:
            first = message.split(sep)[0].strip()
            if first:
                return (first[:120]).strip()
    return message.strip()[:120]

# -------------------- Telegram handlers --------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("¡Hola! 🌱 Soy tu asistente emocional. Para comenzar, ¿cómo te llamas?")
    return REGISTER_NAME

async def register_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['name'] = update.message.text.strip()
    await update.message.reply_text("¿En qué horario sueles estar libre? (mañana / tarde / noche)")
    return REGISTER_TIME

async def register_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text not in TIMESLOT_HOUR:
        await update.message.reply_text("Por favor escribe 'mañana', 'tarde' o 'noche'.")
        return REGISTER_TIME
    context.user_data['time'] = text
    keyboard = [["Peter", "Wuen"]]
    await update.message.reply_text(
        "Elige la personalidad con la que deseas hablar:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True)
    )
    return REGISTER_PERSONALITY

async def register_personality(update: Update, context: ContextTypes.DEFAULT_TYPE):
    personality = update.message.text.strip()
    if personality not in ("Peter", "Wuen"):
        await update.message.reply_text("Por favor elige 'Peter' o 'Wuen'.")
        return REGISTER_PERSONALITY

    context.user_data['personality'] = personality
    users = await load_users()
    uid = str(update.effective_user.id)
    users[uid] = {
        'name': context.user_data['name'],
        'time': context.user_data['time'],
        'personality': personality,
        'last_topic': None,
        'history': [],
        'last_sent_date': None
    }
    await save_users(users)

    await update.message.reply_text(
        f"Perfecto, {context.user_data['name']}! 🤖 Ya estás registrado con la personalidad {personality}.",
        reply_markup=ReplyKeyboardRemove()
    )

    greeting = "Hola! Me alegra conocerte. ¿Quieres contarme cómo te sientes hoy?"
    reply = await openrouter_chat(uid, greeting, personality)
    await update.message.reply_text(reply)
    return ConversationHandler.END

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid = str(update.effective_user.id)
    users = await load_users()
    if uid not in users:
        await update.message.reply_text("Aún no estás registrado. Envía /start para registrarte.")
        return
    user = users[uid]
    personality = user.get('personality', 'Wuen')
    history = user.get('history', [])
    history.append({'role': 'user', 'content': text})
    last_topic = extract_topic_from_message(text)
    user['last_topic'] = last_topic
    user['history'] = history[-30:]
    user['last_message_date'] = datetime.now(LOCAL_TZ).isoformat()
    await save_users(users)

    reply = await openrouter_chat(uid, text, personality, last_topic, history)
    users = await load_users()
    users[uid]['history'].append({'role': 'assistant', 'content': reply})
    users[uid]['history'] = users[uid]['history'][-30:]
    await save_users(users)
    await update.message.reply_text(reply)

async def perfil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    users = await load_users()
    if uid not in users:
        await update.message.reply_text("No estás registrado. Usa /start para registrarte.")
        return
    u = users[uid]
    msg = (
        f"Nombre: {u.get('name')}\n"
        f"Personalidad: {u.get('personality')}\n"
        f"Horario: {u.get('time')}\n"
        f"Último tema: {u.get('last_topic')}\n"
    )
    await update.message.reply_text(msg)

async def ayuda(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "Comandos disponibles:\n"
        "/start - Registrarte\n"
        "/perfil - Ver tu perfil\n"
        "/ayuda - Mostrar esta ayuda\n"
        "Solo envía mensajes al chat para conversar con tu asistente."
    )
    await update.message.reply_text(txt)

# -------------------- Scheduler --------------------
async def send_followups(application: Application):
    users = await load_users()
    now = datetime.now(LOCAL_TZ)
    current_hour = now.hour
    today_str = date.today().isoformat()
    for uid, info in users.items():
        timeslot = info.get('time')
        if not timeslot:
            continue
        target_hour = TIMESLOT_HOUR.get(timeslot)
        if current_hour != target_hour or info.get('last_sent_date') == today_str:
            continue
        name = info.get('name') or ""
        last_topic = info.get('last_topic')
        personality = info.get('personality', 'Wuen')
        user_msg = f"Hola {name}, recordando que hablaste sobre: {last_topic}. ¿Cómo te fue desde entonces?" if last_topic else f"Hola {name}, ¿cómo te sientes hoy?"
        reply_text = await openrouter_chat(uid, user_msg, personality, last_topic, info.get('history', []))
        try:
            await application.bot.send_message(chat_id=int(uid), text=reply_text)
            users[uid]['last_sent_date'] = today_str
            users[uid]['last_sent_time'] = now.isoformat()
            users[uid].setdefault('history', []).append({'role': 'assistant', 'content': reply_text})
            users[uid]['history'] = users[uid]['history'][-30:]
            await save_users(users)
            logger.info("Sent follow-up to %s (%s)", uid, name)
        except Exception as e:
            logger.exception("Failed to send follow-up to %s: %s", uid, e)

# -------------------- Main --------------------
async def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            REGISTER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_name)],
            REGISTER_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_time)],
            REGISTER_PERSONALITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, register_personality)]
        },
        fallbacks=[CommandHandler('ayuda', ayuda)]
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler('perfil', perfil))
    app.add_handler(CommandHandler('ayuda', ayuda))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler(timezone=LOCAL_TZ)
    scheduler.add_job(lambda: asyncio.create_task(send_followups(app)), IntervalTrigger(minutes=10))
    scheduler.start()

    logger.info("🤖 Bot iniciado en Railway. Ejecutando polling...")
    await app.run_polling()

if __name__ == '__main__':
    import nest_asyncio
    import asyncio

    # 🔧 Permite reusar el loop si ya está corriendo (Railway o entornos async)
    nest_asyncio.apply()

    try:
        asyncio.get_event_loop().run_until_complete(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Saliendo...")
