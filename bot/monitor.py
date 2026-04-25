import asyncio
import logging
import aiohttp
from telegram import Bot

logger = logging.getLogger(__name__)


class MultiUserMonitor:
    def __init__(self, bot: Bot, backend_url: str):
        self.bot = bot
        self.backend_url = backend_url
        self.known_orders: dict[str, set] = {}
        self.known_messages: dict[str, set] = {}
        self.last_bump: dict[str, float] = {}
        self.last_restore: dict[str, float] = {}

    async def get_all_users(self) -> list[dict]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.backend_url}/internal/users") as r:
                    if r.status == 200:
                        users = await r.json()
                        return [u for u in users if u.get("is_authorized") and u.get("is_active", True)]
        except Exception as e:
            logger.error(f"get_all_users error: {e}")
        return []

    async def get_user(self, telegram_id: str) -> dict | None:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.backend_url}/internal/user/{telegram_id}") as r:
                    if r.status == 200:
                        return await r.json()
        except Exception as e:
            logger.error(f"get_user {telegram_id} error: {e}")
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
        except:
            pass

    async def run(self):
        logger.info("Мониторинг запущен")
        while True:
            try:
                users = await self.get_all_users()
                tasks = [self.check_user(u) for u in users]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"monitor run error: {e}")
            await asyncio.sleep(30)

    async def check_user(self, user: dict):
        from playerok import PlayerokClient
        import time

        telegram_id = str(user["telegram_id"])
        token = user.get("playerok_token")
        if not token:
            return

        if telegram_id not in self.known_orders:
            self.known_orders[telegram_id] = set()
        if telegram_id not in self.known_messages:
            self.known_messages[telegram_id] = set()

        now = time.time()

        try:
            async with PlayerokClient(token) as client:

                # ── Новые заказы ──
                data = await client.get_orders("PENDING")
                if data and "orders" in data:
                    for order in data["orders"].get("nodes", []):
                        order_id = order["id"]
                        if order_id not in self.known_orders[telegram_id]:
                            self.known_orders[telegram_id].add(order_id)
                            await self.notify_new_order(telegram_id, order, client, user)

                # ── Новые сообщения ──
                chats_data = await client.get_chats()
                if chats_data and "chats" in chats_data:
                    for chat in chats_data["chats"].get("nodes", []):
                        if chat.get("unreadCount", 0) > 0:
                            last = chat.get("lastMessage", {}).get("text", "")
                            key = f"{chat['id']}:{last}"
                            if key not in self.known_messages[telegram_id]:
                                self.known_messages[telegram_id].add(key)
                                await self.notify_new_message(telegram_id, chat, client, user)

                # ── Авто-поднятие ──
                if user.get("auto_bump_enabled"):
                    bump_interval = user.get("bump_interval", 3600)
                    last = self.last_bump.get(telegram_id, 0)
                    if now - last >= bump_interval:
                        self.last_bump[telegram_id] = now
                        await self.do_auto_bump(telegram_id, client)

                # ── Авто-восстановление ──
                if user.get("auto_restore_enabled"):
                    restore_interval = user.get("restore_interval", 300)
                    last = self.last_restore.get(telegram_id, 0)
                    if now - last >= restore_interval:
                        self.last_restore[telegram_id] = now
                        await self.do_auto_restore(telegram_id, client)

        except Exception as e:
            logger.error(f"check_user {telegram_id} error: {e}")
            await self.log(telegram_id, "monitor_error", "ERROR", str(e))

    async def notify_new_order(self, telegram_id: str, order: dict, client, user: dict):
        buyer = order.get("buyer", {}).get("username", "—")
        item = order.get("item", {})
        name = item.get("name", "—")
        price = item.get("price", "—")

        text = (
            f"🔔 *Новый заказ!*\n\n"
            f"👤 Покупатель: *{buyer}*\n"
            f"🎮 Товар: {name}\n"
            f"💰 Цена: {price} ₽\n"
            f"🆔 `{order['id']}`"
        )

        try:
            await self.bot.send_message(chat_id=int(telegram_id), text=text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"notify_new_order {telegram_id}: {e}")
            return

        await self.log(telegram_id, "new_order", details=f"{buyer} — {name}")

        # Авто-ответ на новый заказ
        full_user = await self.get_user(telegram_id)
        if full_user and full_user.get("auto_reply_enabled"):
            reply_text = full_user.get("auto_reply_text", "Спасибо за заказ!")
            reply_text = reply_text.replace("{username}", buyer).replace("{item}", name)

            chats_data = await client.get_chats()
            if chats_data and "chats" in chats_data:
                for chat in chats_data["chats"].get("nodes", []):
                    if chat.get("order", {}).get("id") == order["id"]:
                        await client.send_message(chat["id"], reply_text)
                        await self.log(telegram_id, "auto_reply_sent", details=f"Заказ {order['id']}")
                        break

    async def notify_new_message(self, telegram_id: str, chat: dict, client, user: dict):
        order = chat.get("order", {}) or {}
        buyer = order.get("buyer", {}).get("username", "—") if order else "—"
        item_name = order.get("item", {}).get("name", "—") if order else "—"
        last_msg = chat.get("lastMessage", {}).get("text", "—")
        unread = chat.get("unreadCount", 0)

        text = (
            f"💬 *Новое сообщение!*\n\n"
            f"👤 От: *{buyer}*\n"
            f"🎮 Товар: {item_name}\n"
            f"📝 {last_msg}\n"
            f"🔔 Непрочитанных: {unread}"
        )

        try:
            await self.bot.send_message(chat_id=int(telegram_id), text=text, parse_mode="Markdown")
            await self.log(telegram_id, "new_message", details=f"От {buyer}: {last_msg[:50]}")
        except Exception as e:
            logger.error(f"notify_new_message {telegram_id}: {e}")

        # Авто-ответ на сообщение
        full_user = await self.get_user(telegram_id)
        if full_user and full_user.get("auto_reply_enabled"):
            reply_text = full_user.get("auto_reply_text", "Спасибо за заказ!")
            reply_text = reply_text.replace("{username}", buyer).replace("{item}", item_name)
            await client.send_message(chat["id"], reply_text)
            await self.log(telegram_id, "auto_reply_sent", details=f"Чат {chat['id']}")

    async def do_auto_bump(self, telegram_id: str, client):
        from playerok import PlayerokClient
        data = await client.get_my_items()
        if not data or "myItems" not in data:
            return
        nodes = data["myItems"].get("nodes", [])
        active = [i for i in nodes if i.get("status") == "ACTIVE"]
        bumped = 0
        for item in active:
            result = await client.bump_item(item["id"])
            if result:
                bumped += 1
            await asyncio.sleep(0.5)

        if bumped > 0:
            try:
                await self.bot.send_message(
                    chat_id=int(telegram_id),
                    text=f"🚀 Авто-поднятие: поднято {bumped}/{len(active)} лотов"
                )
            except:
                pass
            await self.log(telegram_id, "auto_bump", details=f"{bumped}/{len(active)}")

    async def do_auto_restore(self, telegram_id: str, client):
        data = await client.get_my_items()
        if not data or "myItems" not in data:
            return
        nodes = data["myItems"].get("nodes", [])
        inactive = [i for i in nodes if i.get("status") in ("INACTIVE", "ARCHIVED", "SOLD")]
        restored = 0
        for item in inactive:
            result = await client.restore_item(item["id"])
            if result:
                restored += 1
            await asyncio.sleep(0.5)

        if restored > 0:
            try:
                await self.bot.send_message(
                    chat_id=int(telegram_id),
                    text=f"♻️ Авто-восстановление: восстановлено {restored} лотов"
                )
            except:
                pass
            await self.log(telegram_id, "auto_restore", details=f"{restored} лотов")
