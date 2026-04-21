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

ENTER_EMAIL, ENTER_CODE = range(2)


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
    async with PlayerokAuth() as auth:
        success, message = await auth.send_code(email)
    if success:
        await api_log(update.effective_user.id, "auth_code_sent", details=email)
        await update.message.reply_text(
            f"✅ Код отправлен на {email}\n\n"
            "Введи код из письма:"
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
    async with PlayerokAuth() as auth:
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
        await update.message.reply_text(f"❌ Неверный код: {result}\n\nПопробуй снова:")
        return ENTER_CODE


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено. Напиши /start чтобы начать снова.")
    return ConversationHandler.END


# ─── Главное меню ─────────────────────────────────────────────────────────────

async def main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 Заказы", callback_data="orders"),
         InlineKeyboardButton("🛒 Лоты", callback_data="items")],
        [InlineKeyboardButton("⬆️ Поднять все лоты", callback_data="bump")],
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

    db_user = await api_get_user(user_id)
    if not db_user or not db_user.get("is_authorized"):
        await query.edit_message_text("❌ Ты не авторизован. Напиши /start")
        return

    token = db_user["playerok_token"]

    if query.data == "profile":
        await show_profile(query, db_user)
    elif query.data == "orders":
        await show_orders(query, token)
    elif query.data == "items":
        await show_items(query, token)
    elif query.data == "bump":
        await bump_items(query, token, user_id)
    elif query.data == "settings":
        await show_settings(query, db_user)
    elif query.data == "toggle_reply":
        new_val = not db_user.get("auto_reply_enabled", True)
        await api_update_user({"telegram_id": user_id, "auto_reply_enabled": new_val})
        db_user["auto_reply_enabled"] = new_val
        await show_settings(query, db_user)
    elif query.data == "logout":
        await api_update_user({"telegram_id": user_id, "is_authorized": False, "playerok_token": ""})
        await api_log(user_id, "logout")
        await query.edit_message_text("👋 Ты вышел из аккаунта. Напиши /start чтобы войти снова.")


async def show_profile(query, db_user: dict):
    text = (
        f"👤 *Профиль*\n\n"
        f"Playerok: `{db_user.get('playerok_username', '—')}`\n"
        f"Статус: {'✅ Авторизован' if db_user.get('is_authorized') else '❌ Не авторизован'}"
    )
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_orders(query, token: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю заказы...")
    async with PlayerokClient(token) as client:
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
                text += f"🔹 {buyer} — {name} — {price}₽\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_items(query, token: str):
    from playerok import PlayerokClient
    await query.edit_message_text("⏳ Загружаю лоты...")
    async with PlayerokClient(token) as client:
        data = await client.get_my_items()
    if not data or "myItems" not in data:
        text = "❌ Не удалось получить лоты"
    else:
        nodes = data["myItems"].get("nodes", [])
        if not nodes:
            text = "🛒 Лотов нет"
        else:
            text = "🛒 *Мои лоты:*\n\n"
            for item in nodes[:15]:
                emoji = "✅" if item.get("status") == "ACTIVE" else "⏸"
                text += f"{emoji} {item.get('name', '—')} — {item.get('price', '—')}₽\n"
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def bump_items(query, token: str, telegram_id: int):
    from playerok import PlayerokClient
    await query.edit_message_text("⬆️ Поднимаю лоты...")
    async with PlayerokClient(token) as client:
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
    await api_log(telegram_id, "bump_items", details=f"Поднято {bumped}/{len(active)}")
    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="back")]]
    await query.edit_message_text(
        f"✅ Поднято {bumped} из {len(active)} лотов",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def show_settings(query, db_user: dict):
    auto_reply = "✅ Вкл" if db_user.get("auto_reply_enabled") else "❌ Выкл"
    keyboard = [
        [InlineKeyboardButton(f"Авто-ответ: {auto_reply}", callback_data="toggle_reply")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back")],
    ]
    text = (
        f"⚙️ *Настройки*\n\n"
        f"Авто-ответ: {auto_reply}\n"
        f"Текст: _{db_user.get('auto_reply_text', '—')}_\n\n"
        f"Интервал мониторинга: {db_user.get('check_interval', 30)} сек"
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

    logger.info("Бот запущен!")
    app.run_polling()


if __name__ == "__main__":
    main()
