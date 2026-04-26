import asyncio
import logging
import aiohttp
import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, ConversationHandler, filters
)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000")

ENTER_EMAIL, ENTER_CODE, ENTER_REPLY_TEXT = range(3)


# ─── Хелперы ──────────────────────────────────────────────────────────────────

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
    await api_update_user({"telegram_id": user.id, "telegram_username": user.username or ""})
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
        await update.message.reply_text(f"✅ Код отправлен на {email}\n\n📩 Введи код из письма:")
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
            f"✅ Успешно! Аккаунт подключён.\n"
            f"👤 {playerok_user.get('username', email)}"
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
    keyboard = [
        [InlineKeyboardButton("📦 Заказы", callback_data="orders"),
         InlineKeyboardButton("🛒 Лоты", callback_data="items")],
        [InlineKeyboardButton("💬 Чаты", callback_data="chats"),
         InlineKeyboardButton("⬆️ Поднять лоты", callback_data="bump")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
         InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("🚪 Выйти", callback_data="logout")],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    text = "🎮 *Playerok Bot*\n\nВыбери действие:"
    if update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="Markdown")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id

    if query.data == "back":
        await main_menu(update, context)
        return

    if query.data == "back_settings":
        db_user = await api_get_user(user_id)
        await show_settings(query, db_user)
        return

    db_user = await api_get_user(user_id)
    if not db_user or not db_user.get("is_authorized"):
        await query.edit_message_text("❌ Ты не авторизован. Напиши /start")
        return

    token = db_user.get("playerok_token", "")
    playerok_user_id = db_user.get("playerok_user_id", "")

    if query.data == "profile":
        await show_profile(query, db_user, token, playerok_user_id)
    elif query.data == "orders":
        await show_orders(query, token, playerok_user_id)
    elif query.data == "items":
        await show_items(query, token, playerok_user_id)
    elif query.data == "chats":
        await show_chats(query, token, playerok_user_id)
    elif query.data == "bump":
        await bump_items(query, token, playerok_user_id, user_id)
    elif query.data == "settings":
        await show_settings(query, db_user)
    elif query.data == "toggle_reply":
        new_val = not db_user.get("auto_reply_enabled", True)
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
    elif query.data == "set_reply_text":
        await query.edit_message_text(
            "✏️ Введи новый текст авто-ответа:\n\n"
            "Можно использовать:\n`{username}` — ник покупателя\n`{item}` — название товара",
            parse_mode="Markdown"
        )
        context.user_data["waiting_for"] = "reply_text"
        context.user_data["edit_msg"] = query.message.message_id
    elif query.data == "logout":
        await api_update_user({"telegram_id": user_id, "is_authorized": False, "playerok_token": ""})
        await api_log(user_id, "logout")
        await query.edit_message_text("👋 Вышел из аккаунта. Напиши /start чтобы войти снова.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    waiting = context.user_data.get("waiting_for")
    if waiting == "reply_text":
        user_id = update.effective_user.id
        new_text = update.message.text.strip()
        await api_update_user({"telegram_id": user_id, "auto_reply_text": new_text})
        context.user_data.pop("waiting_for", None)
        await update.message.reply_text(f"✅ Текст авто-ответа обновлён:\n_{new_text}_", parse_mode="Markdown")
        db_user = await api_get_user(user_id)
        keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="settings")]]
        await update.message.reply_text("Вернуться в настройки:", reply_markup=InlineKeyboardMarkup(keyboard))


# ─── Профиль ──────────────────────────────────────────────────────────────────

async def show_profile(query, db_user: dict, token: str, playerok_user_id: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю профиль...")
    client = PlayerokClient(token, playerok_user_id)
    profile = await client.get_profile()
    if profile:
        balance = profile.get("balance", {}).get("value", "—")
        unread = profile.get("unreadChatsCounter", 0)
        text = (
            f"👤 *Профиль*\n\n"
            f"Ник: *{profile.get('username', '—')}*\n"
            f"Email: `{profile.get('email', '—')}`\n"
            f"Баланс: *{balance} ₽*\n"
            f"Непрочитанных чатов: *{unread}*\n"
            f"Статус: ✅ Авторизован"
        )
    else:
        text = (
            f"👤 *Профиль*\n\n"
            f"Ник: *{db_user.get('playerok_username', '—')}*\n"
            f"Статус: ✅ Авторизован"
        )
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Заказы ───────────────────────────────────────────────────────────────────

async def show_orders(query, token: str, playerok_user_id: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю заказы...")
    client = PlayerokClient(token, playerok_user_id)
    data = await client.get_orders(first=20)
    if not data or "deals" not in data:
        text = "❌ Не удалось получить заказы"
    else:
        edges = data["deals"].get("edges", [])
        if not edges:
            text = "📦 Заказов пока нет"
        else:
            text = f"📦 *Заказы* ({len(edges)} шт.):\n\n"
            for edge in edges[:15]:
                order = edge.get("node", {})
                buyer = order.get("user", {}).get("username", "—")
                item = order.get("item", {}) or {}
                name = item.get("name", "—")
                price = item.get("price", "—")
                status = order.get("status", "—")
                text += f"🔹 *{buyer}* — {name} — {price}₽\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Лоты ─────────────────────────────────────────────────────────────────────

async def show_items(query, token: str, playerok_user_id: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю лоты...")
    client = PlayerokClient(token, playerok_user_id)
    data = await client.get_my_items(first=50)
    if not data or "items" not in data:
        text = "❌ Не удалось получить лоты"
    else:
        edges = data["items"].get("edges", [])
        if not edges:
            text = "🛒 Лотов нет"
        else:
            text = f"🛒 *Мои лоты* ({len(edges)} шт.):\n\n"
            for edge in edges[:20]:
                item = edge.get("node", {})
                status = item.get("status", "")
                emoji = "✅" if status == "APPROVED" else "⏳" if status == "PENDING_MODERATION" else "⏸"
                name = item.get("name", "—")
                price = item.get("price", "—")
                text += f"{emoji} {name} — {price}₽\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Чаты ─────────────────────────────────────────────────────────────────────

async def show_chats(query, token: str, playerok_user_id: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю чаты...")
    client = PlayerokClient(token, playerok_user_id)
    data = await client.get_chats(first=15)
    if not data or "userChats" not in data:
        text = "❌ Не удалось получить чаты"
    else:
        edges = data["userChats"].get("edges", [])
        if not edges:
            text = "💬 Чатов нет"
        else:
            text = f"💬 *Чаты* ({len(edges)} шт.):\n\n"
            for edge in edges[:10]:
                chat = edge.get("node", {})
                unread = chat.get("unreadMessagesCounter", 0)
                participants = chat.get("participants", [])
                buyer = participants[0].get("username", "—") if participants else "—"
                deals = chat.get("deals", [])
                item_name = "—"
                if deals:
                    item = deals[0].get("item", {})
                    item_name = item.get("name", "—") if item else "—"
                unread_emoji = f" 🔴 {unread}" if unread > 0 else ""
                text += f"👤 *{buyer}* — {item_name}{unread_emoji}\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


# ─── Поднятие лотов ───────────────────────────────────────────────────────────

async def bump_items(query, token: str, playerok_user_id: str, telegram_id: int):
    from playerok import PlayerokClient
    await query.edit_message_text("⬆️ Поднимаю лоты...")
    client = PlayerokClient(token, playerok_user_id)
    data = await client.get_my_items(first=50)
    if not data or "items" not in data:
        await query.edit_message_text("❌ Не удалось получить лоты")
        return
    edges = data["items"].get("edges", [])
    active = [e["node"] for e in edges if e.get("node", {}).get("status") == "APPROVED"]
    bumped = 0
    for item in active:
        result = await client.bump_item(item["id"])
        if result:
            bumped += 1
        await asyncio.sleep(0.5)
    await api_log(telegram_id, "bump_items", details=f"{bumped}/{len(active)}")
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(
        f"✅ Поднято *{bumped}* из *{len(active)}* лотов",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )


# ─── Настройки ────────────────────────────────────────────────────────────────

async def show_settings(query, db_user: dict):
    auto_reply = "✅" if db_user.get("auto_reply_enabled") else "❌"
    auto_bump = "✅" if db_user.get("auto_bump_enabled") else "❌"
    auto_restore = "✅" if db_user.get("auto_restore_enabled") else "❌"
    bump_interval = db_user.get("bump_interval", 3600)
    restore_interval = db_user.get("restore_interval", 300)
    reply_text = db_user.get("auto_reply_text", "—")

    keyboard = [
        [InlineKeyboardButton(f"{auto_reply} Авто-ответ", callback_data="toggle_reply")],
        [InlineKeyboardButton(f"{auto_bump} Авто-поднятие (каждые {bump_interval//60} мин)", callback_data="toggle_bump")],
        [InlineKeyboardButton(f"{auto_restore} Авто-восстановление", callback_data="toggle_restore")],
        [InlineKeyboardButton("✏️ Изменить текст авто-ответа", callback_data="set_reply_text")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ]
    text = (
        f"⚙️ *Настройки*\n\n"
        f"{auto_reply} Авто-ответ\n"
        f"{auto_bump} Авто-поднятие (каждые {bump_interval//60} мин)\n"
        f"{auto_restore} Авто-восстановление\n\n"
        f"📝 Текст ответа:\n_{reply_text}_"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


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
