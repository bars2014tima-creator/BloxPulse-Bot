import os
import sqlite3
import random
import logging
from datetime import datetime, timezone, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO
)
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().isdigit()
}
DB_PATH = os.getenv("DB_PATH", "bloxpulse.db")

# Conversation state is kept per user while the bot is running.
states = {}

def db():
    return sqlite3.connect(DB_PATH)

def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT,
                item_type TEXT NOT NULL,
                item_name TEXT NOT NULL,
                direction TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                proposal_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                vote TEXT NOT NULL,
                UNIQUE(proposal_id, user_id)
            )
        """)

def weekly_count(user_id):
    # Rolling seven-day limit, based on UTC.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    with db() as con:
        row = con.execute(
            "SELECT COUNT(*) FROM proposals WHERE user_id=? AND created_at>=?",
            (user_id, cutoff)
        ).fetchone()
    return row[0]

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Предложить изменение", callback_data="suggest")],
        [InlineKeyboardButton("🗳 Голосования", callback_data="votes")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    states.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "Привет! Это BloxPulse Bot 📊\n\n"
        "Здесь можно предложить повысить или понизить стоимость фрукта или скина. "
        "Каждое предложение проверяется модерацией.",
        reply_markup=main_keyboard()
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как предложить изменение:\n"
        "1. Нажми «Предложить изменение».\n"
        "2. Укажи фрукт или скин.\n"
        "3. Выбери повышение или понижение.\n"
        "4. Напиши причину.\n\n"
        "Лимит: 3 предложения за последние 7 дней. "
        "Отправка предложения не меняет цену автоматически."
    )

async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data == "help":
        await q.message.reply_text(
            "Лимит — 3 предложения за последние 7 дней. "
            "Предложения сначала проверяют модераторы."
        )
        return

    if data == "suggest":
        used = weekly_count(uid)
        if used >= 3:
            await q.message.reply_text(
                "Лимит исчерпан: у тебя уже 3 предложения за последние 7 дней. "
                "Попробуй позже."
            )
            return
        a, b = random.randint(2, 9), random.randint(2, 9)
        states[uid] = {"step": "captcha", "answer": str(a + b)}
        await q.message.reply_text(
            f"Проверка, что ты не автоматический бот 🤖\nСколько будет {a} + {b}?"
        )
        return

    if data.startswith("type:"):
        state = states.get(uid)
        if not state or state.get("step") != "type":
            return
        state["item_type"] = data.split(":", 1)[1]
        state["step"] = "item"
        await q.message.reply_text("Напиши точное название фрукта или скина:")
        return

    if data.startswith("direction:"):
        state = states.get(uid)
        if not state or state.get("step") != "direction":
            return
        state["direction"] = data.split(":", 1)[1]
        state["step"] = "reason"
        await q.message.reply_text(
            "Напиши причину изменения. Например: «спрос вырос после обновления»."
        )
        return

    if data.startswith("review:") and uid in ADMIN_IDS:
        _, action, proposal_id = data.split(":")
        status = "approved" if action == "approve" else "rejected"
        with db() as con:
            row = con.execute(
                "SELECT user_id FROM proposals WHERE id=? AND status='pending'",
                (int(proposal_id),)
            ).fetchone()
            if row:
                con.execute(
                    "UPDATE proposals SET status=? WHERE id=?",
                    (status, int(proposal_id))
                )
        if row:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(f"Предложение #{proposal_id}: {status}.")
            try:
                await context.bot.send_message(
                    row[0],
                    f"Твоё предложение #{proposal_id}: "
                    + ("одобрено модератором." if status == "approved"
                       else "отклонено модератором.")
                )
            except Exception:
                pass
        return

    if data.startswith("vote:"):
        _, proposal_id, choice = data.split(":")
        with db() as con:
            status_row = con.execute(
                "SELECT status FROM proposals WHERE id=?", (int(proposal_id),)
            ).fetchone()
            if not status_row or status_row[0] != "approved":
                await q.message.reply_text("Это предложение пока недоступно для голосования.")
                return
            try:
                con.execute(
                    "INSERT INTO votes(proposal_id,user_id,vote) VALUES(?,?,?)",
                    (int(proposal_id), uid, choice)
                )
                await q.message.reply_text("Голос учтён! Спасибо.")
            except sqlite3.IntegrityError:
                await q.message.reply_text("Ты уже голосовал за это предложение.")
        return

    if data == "votes":
        with db() as con:
            rows = con.execute(
                "SELECT id,item_type,item_name,direction,reason FROM proposals "
                "WHERE status='approved' ORDER BY id DESC LIMIT 10"
            ).fetchall()
        if not rows:
            await q.message.reply_text("Пока нет одобренных предложений для голосования.")
            return
        for pid, item_type, item_name, direction, reason in rows:
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton("👍 Поддерживаю", callback_data=f"vote:{pid}:yes"),
                InlineKeyboardButton("👎 Не согласен", callback_data=f"vote:{pid}:no")
            ]])
            await q.message.reply_text(
                f"Предложение #{pid}\n{item_type}: {item_name}\n"
                f"Изменение: {direction}\nПричина: {reason}",
                reply_markup=kb
            )

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    uid = user.id
    state = states.get(uid)
    if not state:
        await update.message.reply_text(
            "Выбери действие в меню командой /start."
        )
        return

    text = update.message.text.strip()

    if state["step"] == "captcha":
        if text != state["answer"]:
            states.pop(uid, None)
            await update.message.reply_text(
                "Проверка не пройдена. Нажми /start и попробуй ещё раз."
            )
            return
        state["step"] = "type"
        await update.message.reply_text(
            "Проверка пройдена! Что меняем?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🍎 Фрукт", callback_data="type:Фрукт")],
                [InlineKeyboardButton("🎨 Скин", callback_data="type:Скин")]
            ])
        )
        return

    if state["step"] == "item":
        if len(text) < 2 or len(text) > 50:
            await update.message.reply_text("Название должно быть от 2 до 50 символов.")
            return
        state["item_name"] = text
        state["step"] = "direction"
        await update.message.reply_text(
            "Что предлагаешь?",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📈 Повысить", callback_data="direction:Повысить")],
                [InlineKeyboardButton("📉 Понизить", callback_data="direction:Понизить")]
            ])
        )
        return

    if state["step"] == "reason":
        if len(text) < 10 or len(text) > 500:
            await update.message.reply_text("Причина должна быть от 10 до 500 символов.")
            return

        if weekly_count(uid) >= 3:
            states.pop(uid, None)
            await update.message.reply_text("Лимит 3 предложения за 7 дней уже исчерпан.")
            return

        with db() as con:
            cur = con.execute(
                "INSERT INTO proposals(user_id,username,item_type,item_name,direction,reason,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    uid, user.username or "", state["item_type"],
                    state["item_name"], state["direction"], text,
                    datetime.now(timezone.utc).isoformat()
                )
            )
            proposal_id = cur.lastrowid

        states.pop(uid, None)
        await update.message.reply_text(
            f"Предложение #{proposal_id} отправлено на проверку! ✅\n"
            "Цена на сайте пока не меняется."
        )

        admin_text = (
            f"🆕 Предложение #{proposal_id}\n"
            f"Пользователь: @{user.username or 'без username'} (ID {uid})\n"
            f"Тип: {state['item_type']}\nПредмет: {state['item_name']}\n"
            f"Изменение: {state['direction']}\nПричина: {text}"
        )
        review_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Одобрить", callback_data=f"review:approve:{proposal_id}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"review:reject:{proposal_id}")
        ]])
        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    admin_id, admin_text, reply_markup=review_kb
                )
            except Exception:
                logging.exception("Не удалось отправить предложение модератору")

async def post_init(application: Application):
    init_db()
    logging.info("BloxPulse Bot started")

def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN в переменных окружения.")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    app.run_polling()

if __name__ == "__main__":
    main()
  
