import asyncio
import logging
import aiohttp
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

ENTER_EMAIL, ENTER_CODE, ENTER_REPLY_TEXT = range(3)


# ─── API хелперы ──────────────────────────────────────────────────────────────

async def api_get_user(telegram_id: int) -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{BACKEND_URL}/internal/user/{telegram_id}") as r:
                if r.status == 200:
                    return await r.json()
    except:
        pass
    return None


async def api_update_user(data: dict):
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{BACKEND_URL}/internal/user/update", json=data)
    except Exception as e:
        logger.error(f"api_update_user error: {e}")


async def api_log(telegram_id: int, action: str, level: str = "INFO", details: str = None):
    try:
        async with aiohttp.ClientSession() as s:
            await s.post(f"{BACKEND_URL}/internal/log", json={
                "telegram_id": str(telegram_id),
                "level": level,
                "action": action,
                "details": details,
            })
    except:
        pass


# ─── Авторизация ──────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await api_update_user({
        "telegram_id": user.id,
        "telegram_username": user.username or "",
    })
    db_user = await api_get_user(user.id)
    if db_user and db_user.get("is_authorized"):
        await main_menu(update, context)
        return ConversationHandler.END
    else:
        await update.message.reply_text(
            f"👋 Привет, {user.first_name}!\n\n"
            "Для начала работы подключи свой Playerok аккаунт.\n\n"
            "📧 Введи email от Playerok:"
        )
        return ENTER_EMAIL


async def enter_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from playerok import PlayerokAuth
    email = update.message.text.strip()
    context.user_data["email"] = email
    await update.message.reply_text("⏳ Отправляю код на почту...")
    auth = PlayerokAuth()
    success, message = await auth.send_code(email)
    if success:
        await api_log(update.effective_user.id, "auth_code_sent", details=email)
        await update.message.reply_text(
            f"✅ Код отправлен на {email}\n\n📩 Введи код из письма:"
        )
        return ENTER_CODE
    else:
        await update.message.reply_text(f"❌ Ошибка: {message}\n\nПопробуй снова — введи email:")
        return ENTER_EMAIL


async def enter_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from playerok import PlayerokAuth
    code = update.message.text.strip()
    email = context.user_data.get("email")
    user = update.effective_user
    await update.message.reply_text("⏳ Проверяю код...")
    auth = PlayerokAuth()
    token, result = await auth.verify_code(email, code)
    if token:
        playerok_user = result if isinstance(result, dict) else {}
        await api_update_user({
            "telegram_id": user.id,
            "playerok_token": token,
            "playerok_email": email,
            "playerok_user_id": playerok_user.get("id", ""),
            "playerok_username": playerok_user.get("username", ""),
            "is_authorized": True,
        })
        await api_log(user.id, "auth_success", details=playerok_user.get("username", ""))
        await update.message.reply_text(
            f"✅ Аккаунт подключён!\n👤 {playerok_user.get('username', email)}"
        )
        await main_menu(update, context)
        return ConversationHandler.END
    else:
        await api_log(user.id, "auth_failed", level="WARNING", details=str(result))
        await update.message.reply_text(f"❌ {result}\n\nПопробуй снова — введи код:")
        return ENTER_CODE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. Напиши /start чтобы начать снова.")
    return ConversationHandler.END


# ─── Главное меню ─────────────────────────────────────────────────────────────

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user = await api_get_user(update.effective_user.id)
    username = db_user.get("playerok_username", "—") if db_user else "—"
    keyboard = [
        [InlineKeyboardButton("💬 Чаты", callback_data="chats"),
         InlineKeyboardButton("📦 Заказы", callback_data="orders")],
        [InlineKeyboardButton("🛒 Лоты", callback_data="items"),
         InlineKeyboardButton("⬆️ Поднять лоты", callback_data="bump")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
         InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🚪 Выйти", callback_data="logout")],
    ]
    text = f"🎮 *Playerok Bot*\n👤 {username}\n\nВыбери действие:"
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Обработчик кнопок ────────────────────────────────────────────────────────

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "back":
        await main_menu(update, context)
        return

    db_user = await api_get_user(user_id)
    if not db_user or not db_user.get("is_authorized"):
        await query.edit_message_text("❌ Ты не авторизован. Напиши /start")
        return

    token = db_user.get("playerok_token", "")

    # Настройки
    if query.data == "settings":
        await show_settings(query, db_user)
    elif query.data == "toggle_reply":
        new_val = not db_user.get("auto_reply_enabled", False)
        await api_update_user({"telegram_id": user_id, "auto_reply_enabled": new_val})
        db_user["auto_reply_enabled"] = new_val
        await show_settings(query, db_user)
    elif query.data == "toggle_bump":
        new_val = not db_user.get("auto_bump_enabled", False)
        await api_update_user({"telegram_id": user_id, "auto_bump_enabled": new_val})
        db_user["auto_bump_enabled"] = new_val
        await show_settings(query, db_user)
    elif query.data == "toggle_restore":
        new_val = not db_user.get("auto_restore_enabled", False)
        await api_update_user({"telegram_id": user_id, "auto_restore_enabled": new_val})
        db_user["auto_restore_enabled"] = new_val
        await show_settings(query, db_user)
    elif query.data == "cycle_bump_interval":
        intervals = [1800, 3600, 7200, 14400, 28800]
        current = db_user.get("bump_interval", 3600)
        try:
            idx = intervals.index(current)
            new_val = intervals[(idx + 1) % len(intervals)]
        except ValueError:
            new_val = 3600
        await api_update_user({"telegram_id": user_id, "bump_interval": new_val})
        db_user["bump_interval"] = new_val
        await show_settings(query, db_user)
    elif query.data == "set_reply_text":
        await query.edit_message_text(
            "✏️ Введи текст авто-ответа:\n\n"
            "Доступные переменные:\n"
            "`{username}` — ник покупателя\n"
            "`{item}` — название товара",
            parse_mode="Markdown"
        )
        context.user_data["awaiting"] = "reply_text"
    # Основные функции
    elif query.data == "profile":
        await show_profile(query, db_user, token)
    elif query.data == "orders":
        await show_orders(query, token)
    elif query.data == "chats":
        await show_chats(query, token)
    elif query.data == "items":
        await show_items(query, token)
    elif query.data == "bump":
        await bump_items(query, token, user_id)
    elif query.data == "logout":
        await api_update_user({"telegram_id": user_id, "is_authorized": False, "playerok_token": ""})
        await api_log(user_id, "logout")
        await query.edit_message_text("👋 Вышел из аккаунта. Напиши /start чтобы войти снова.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает текст вне диалога авторизации"""
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return

    user_id = update.effective_user.id
    text = update.message.text.strip()

    if awaiting == "reply_text":
        await api_update_user({"telegram_id": user_id, "auto_reply_text": text})
        context.user_data.pop("awaiting", None)
        keyboard = [[InlineKeyboardButton("🔙 В настройки", callback_data="settings")]]
        await update.message.reply_text(
            f"✅ Текст авто-ответа сохранён:\n\n_{text}_",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )


# ─── Настройки ────────────────────────────────────────────────────────────────

async def show_settings(query, db_user: dict):
    auto_reply = "✅ Вкл" if db_user.get("auto_reply_enabled") else "❌ Выкл"
    auto_bump = "✅ Вкл" if db_user.get("auto_bump_enabled") else "❌ Выкл"
    auto_restore = "✅ Вкл" if db_user.get("auto_restore_enabled") else "❌ Выкл"

    bump_interval = db_user.get("bump_interval", 3600)
    bump_str = f"{bump_interval // 3600}ч" if bump_interval >= 3600 else f"{bump_interval // 60}мин"

    reply_preview = db_user.get("auto_reply_text", "Спасибо за заказ!")
    if len(reply_preview) > 40:
        reply_preview = reply_preview[:40] + "..."

    keyboard = [
        [InlineKeyboardButton(f"♻️ Авто-восстановление: {auto_restore}", callback_data="toggle_restore")],
        [InlineKeyboardButton(f"🚀 Авто-поднятие: {auto_bump}", callback_data="toggle_bump")],
        [InlineKeyboardButton(f"⏱ Интервал поднятия: {bump_str}", callback_data="cycle_bump_interval")],
        [InlineKeyboardButton(f"💬 Авто-ответ: {auto_reply}", callback_data="toggle_reply")],
        [InlineKeyboardButton("✏️ Текст авто-ответа", callback_data="set_reply_text")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ]

    text = (
        f"⚙️ *Настройки: {db_user.get('playerok_username', '—')}*\n\n"
        f"♻️ Авто-восстановление: {auto_restore}\n"
        f"🚀 Авто-поднятие: {auto_bump} (каждые {bump_str})\n"
        f"💬 Авто-ответ: {auto_reply}\n"
        f"📝 Текст: _{reply_preview}_"
    )

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Профиль ──────────────────────────────────────────────────────────────────

async def show_profile(query, db_user: dict, token: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю профиль...")
    client = PlayerokClient(token)
    profile = await client.get_profile()

    balance = "—"
    if profile and isinstance(profile.get("balance"), dict):
        balance = profile["balance"].get("value", "—")

    text = (
        f"👤 *Профиль*\n\n"
        f"Ник: `{db_user.get('playerok_username', '—')}`\n"
        f"Email: `{db_user.get('playerok_email', '—')}`\n"
        f"Баланс: `{balance} ₽`\n\n"
        f"♻️ Авто-восстановление: {'✅' if db_user.get('auto_restore_enabled') else '❌'}\n"
        f"🚀 Авто-поднятие: {'✅' if db_user.get('auto_bump_enabled') else '❌'}\n"
        f"💬 Авто-ответ: {'✅' if db_user.get('auto_reply_enabled') else '❌'}"
    )
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Заказы ───────────────────────────────────────────────────────────────────

async def show_orders(query, token: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю заказы...")
    client = PlayerokClient(token)
    data = await client.get_orders()
    if not data or "orders" not in data:
        text = "❌ Не удалось получить заказы"
    else:
        nodes = data["orders"].get("nodes", [])
        if not nodes:
            text = "📦 Активных заказов нет"
        else:
            text = "📦 *Активные заказы:*\n\n"
            for o in nodes[:10]:
                buyer = o.get("buyer", {}).get("username", "—")
                name = o.get("item", {}).get("name", "—")
                price = o.get("item", {}).get("price", "—")
                text += f"🔹 *{buyer}* — {name} — {price}₽\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Чаты ─────────────────────────────────────────────────────────────────────

async def show_chats(query, token: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю чаты...")
    client = PlayerokClient(token)
    data = await client.get_chats()
    if not data or "chats" not in data:
        text = "❌ Не удалось получить чаты"
    else:
        nodes = data["chats"].get("nodes", [])
        if not nodes:
            text = "💬 Чатов нет"
        else:
            text = "💬 *Активные чаты:*\n\n"
            for chat in nodes[:10]:
                order = chat.get("order", {}) or {}
                buyer = order.get("buyer", {}).get("username", "—") if order else "—"
                item = order.get("item", {}).get("name", "—") if order else "—"
                last = chat.get("lastMessage", {}).get("text", "—")
                unread = chat.get("unreadCount", 0)
                unread_str = f" 🔴{unread}" if unread > 0 else ""
                if len(last) > 40:
                    last = last[:40] + "..."
                text += f"👤 *{buyer}*{unread_str} — {item}\n📝 {last}\n\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Лоты ─────────────────────────────────────────────────────────────────────

async def show_items(query, token: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю лоты...")
    client = PlayerokClient(token)
    data = await client.get_my_items()
    if not data or "myItems" not in data:
        text = "❌ Не удалось получить лоты"
    else:
        nodes = data["myItems"].get("nodes", [])
        if not nodes:
            text = "🛒 Лотов нет"
        else:
            active = [i for i in nodes if i.get("status") == "ACTIVE"]
            inactive = [i for i in nodes if i.get("status") != "ACTIVE"]
            text = f"🛒 *Мои лоты:* {len(active)} активных / {len(inactive)} неактивных\n\n"
            for item in nodes[:15]:
                emoji = "✅" if item.get("status") == "ACTIVE" else "⏸"
                text += f"{emoji} {item.get('name', '—')} — {item.get('price', '—')}₽\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Поднятие лотов ───────────────────────────────────────────────────────────

async def bump_items(query, token: str, telegram_id: int):
    from playerok import PlayerokClient
    await query.edit_message_text("⬆️ Поднимаю лоты...")
    client = PlayerokClient(token)
    data = await client.get_my_items()
    if not data or "myItems" not in data:
        await query.edit_message_text("❌ Не удалось получить лоты")
        return
    nodes = data["myItems"].get("nodes", [])
    active = [i for i in nodes if i.get("status") == "ACTIVE"]
    bumped = 0
    for item in active:
        result = await client.bump_item(item["id"])
        if result:
            bumped += 1
        await asyncio.sleep(0.5)
    await api_log(telegram_id, "bump_items", details=f"{bumped}/{len(active)}")
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(
        f"✅ Поднято {bumped} из {len(active)} лотов",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def post_init(application: Application):
    from monitor import MultiUserMonitor
    monitor = MultiUserMonitor(application.bot, BACKEND_URL)
    asyncio.create_task(monitor.run())


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ENTER_EMAIL: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_email)],
            ENTER_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_code)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
