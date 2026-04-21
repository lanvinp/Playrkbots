import asyncio
import logging
import aiohttp
from telegram import Bot

logger = logging.getLogger(__name__)


class MultiUserMonitor:
    """Мониторинг заказов для всех авторизованных пользователей"""

    def __init__(self, bot: Bot, backend_url: str, interval: int = 30):
        self.bot = bot
        self.backend_url = backend_url
        self.interval = interval
        # known_orders[telegram_id] = set of order_ids
        self.known_orders: dict[str, set] = {}
        self.known_messages: dict[str, set] = {}

    async def get_all_users(self) -> list[dict]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.backend_url}/admin/users?key=internal") as r:
                    if r.status == 200:
                        users = await r.json()
                        return [u for u in users if u.get("is_authorized") and u.get("is_active")]
        except Exception as e:
            logger.error(f"Ошибка получения пользователей: {e}")
        return []

    async def get_user(self, telegram_id: str) -> dict | None:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.backend_url}/internal/user/{telegram_id}") as r:
                    if r.status == 200:
                        return await r.json()
        except Exception as e:
            logger.error(f"Ошибка получения юзера {telegram_id}: {e}")
        return None

    async def log(self, telegram_id: str, action: str, level: str = "INFO", details: str = None):
        try:
            async with aiohttp.ClientSession() as s:
                await s.post(f"{self.backend_url}/internal/log", json={
                    "telegram_id": telegram_id,
                    "level": level,
                    "action": action,
                    "details": details,
                })
        except Exception as e:
            logger.error(f"Ошибка логирования: {e}")

    async def run(self):
        logger.info("Мультипользовательский мониторинг запущен")
        while True:
            try:
                users = await self.get_all_users()
                tasks = [self.check_user(u) for u in users]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Ошибка мониторинга: {e}")
            await asyncio.sleep(self.interval)

    async def check_user(self, user: dict):
        from playerok import PlayerokClient

        telegram_id = user["telegram_id"]
        token = user.get("playerok_token")
        if not token:
            return

        if telegram_id not in self.known_orders:
            self.known_orders[telegram_id] = set()
        if telegram_id not in self.known_messages:
            self.known_messages[telegram_id] = set()

        try:
            async with PlayerokClient(token) as client:
                # Проверяем заказы
                data = await client.get_orders("PENDING")
                if data and "orders" in data:
                    for order in data["orders"].get("nodes", []):
                        order_id = order["id"]
                        if order_id not in self.known_orders[telegram_id]:
                            self.known_orders[telegram_id].add(order_id)
                            await self.notify_order(telegram_id, order, client, user)

                # Проверяем чаты
                chats = await client.get_chats()
                if chats and "chats" in chats:
                    for chat in chats["chats"].get("nodes", []):
                        if chat.get("unreadCount", 0) > 0:
                            last = chat.get("lastMessage", {}).get("text", "")
                            key = f"{chat['id']}:{last}"
                            if key not in self.known_messages[telegram_id]:
                                self.known_messages[telegram_id].add(key)
                                await self.notify_message(telegram_id, chat)
        except Exception as e:
            await self.log(telegram_id, "monitor_error", "ERROR", str(e))

    async def notify_order(self, telegram_id: str, order: dict, client, user: dict):
        buyer = order.get("buyer", {}).get("username", "—")
        item = order.get("item", {})
        name = item.get("name", "—")
        price = item.get("price", "—")

        text = (
            f"🔔 *Новый заказ!*\n\n"
            f"👤 Покупатель: {buyer}\n"
            f"🎮 Товар: {name}\n"
            f"💰 Цена: {price} ₽\n"
            f"🆔 `{order['id']}`"
        )

        try:
            await self.bot.send_message(chat_id=int(telegram_id), text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Не удалось уведомить {telegram_id}: {e}")
            return

        await self.log(telegram_id, "new_order", details=f"{buyer} — {name}")

        # Авто-ответ
        full_user = await self.get_user(telegram_id)
        if full_user and full_user.get("auto_reply_enabled"):
            reply_text = full_user.get("auto_reply_text", "Спасибо за заказ!")
            chats = await client.get_chats()
            if chats and "chats" in chats:
                for chat in chats["chats"].get("nodes", []):
                    if chat.get("order", {}).get("id") == order["id"]:
                        await client.send_message(chat["id"], reply_text)
                        await self.log(telegram_id, "auto_reply_sent", details=order["id"])
                        break

    async def notify_message(self, telegram_id: str, chat: dict):
        buyer = chat.get("order", {}).get("buyer", {}).get("username", "—")
        last_msg = chat.get("lastMessage", {}).get("text", "—")

        text = (
            f"💬 *Новое сообщение!*\n\n"
            f"👤 От: {buyer}\n"
            f"📝 {last_msg}"
        )

        try:
            await self.bot.send_message(chat_id=int(telegram_id), text=text, parse_mode="Markdown")
            await self.log(telegram_id, "new_message", details=f"От {buyer}")
        except Exception as e:
            logger.error(f"Не удалось уведомить {telegram_id}: {e}")
