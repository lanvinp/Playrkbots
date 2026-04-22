import aiohttp
import asyncio
import json
import logging
from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

PLAYEROK_API = "https://playerok.com/graphql"

HEADERS = {
    "Content-Type": "application/json",
    "Accept": "*/*",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Origin": "https://playerok.com",
    "Referer": "https://playerok.com/",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


class PlayerokAuth:
    """Авторизация в Playerok через браузер (Playwright) — обходит DDoS-Guard"""

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    async def __aenter__(self):
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="ru-RU",
            extra_http_headers={
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )
        self._page = await self._context.new_page()
        # Открываем сайт чтобы получить куки и пройти DDoS-Guard
        try:
            await self._page.goto("https://playerok.com", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)  # ждём прохождения проверки
        except Exception as e:
            logger.warning(f"Не удалось открыть главную страницу: {e}")
        return self

    async def __aexit__(self, *args):
        try:
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception:
            pass

    async def _graphql(self, query: str, variables: dict = None, token: str = None) -> tuple:
        """Выполнить GraphQL запрос через браузер"""
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        headers = {**HEADERS}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        try:
            result = await self._page.evaluate("""
                async ([url, payload, headers]) => {
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: headers,
                        body: JSON.stringify(payload),
                        credentials: 'include',
                    });
                    const text = await resp.text();
                    return { status: resp.status, body: text };
                }
            """, [PLAYEROK_API, payload, headers])

            if result["status"] != 200:
                logger.error(f"HTTP {result['status']}: {result['body'][:200]}")
                return None, f"Ошибка сервера (статус {result['status']})"

            data = json.loads(result["body"])
            if "errors" in data:
                logger.error(f"GraphQL errors: {data['errors']}")
                return None, data["errors"][0].get("message", "Неизвестная ошибка")
            return data.get("data"), None

        except Exception as e:
            logger.error(f"GraphQL request error: {e}")
            return None, str(e)

    async def send_code(self, email: str) -> tuple[bool, str]:
        """Отправить код на почту"""
        query = """
        mutation SendLoginCode($email: String!) {
            sendLoginCode(email: $email) {
                success
            }
        }
        """
        data, error = await self._graphql(query, {"email": email})
        if error:
            return False, error
        if data and data.get("sendLoginCode", {}).get("success"):
            return True, "Код отправлен на почту"
        return False, "Не удалось отправить код"

    async def verify_code(self, email: str, code: str) -> tuple:
        """Подтвердить код и получить токен"""
        query = """
        mutation LoginWithCode($email: String!, $code: String!) {
            loginWithCode(email: $email, code: $code) {
                token
                user {
                    id
                    username
                }
            }
        }
        """
        data, error = await self._graphql(query, {"email": email, "code": code})
        if error:
            return None, error

        if data and "loginWithCode" in data:
            result = data["loginWithCode"]
            token = result.get("token")
            user = result.get("user", {})
            return token, user
        return None, "Неверный код"

    async def get_profile(self, token: str) -> tuple:
        """Получить профиль по токену"""
        query = """
        query {
            profile {
                id
                username
                email
            }
        }
        """
        data, error = await self._graphql(query, token=token)
        if error:
            return None, error
        return data.get("profile"), None


class PlayerokClient:
    """Клиент для работы с Playerok API (обычные запросы с токеном)"""

    def __init__(self, token: str):
        self.token = token
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        headers = {**HEADERS, "Authorization": f"Bearer {self.token}"}
        self.session = aiohttp.ClientSession(headers=headers)
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def _request(self, query: str, variables: dict = None):
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        try:
            async with self.session.post(PLAYEROK_API, json=payload) as resp:
                if resp.content_type != "application/json":
                    text = await resp.text()
                    logger.error(f"Non-JSON response ({resp.status}): {text[:200]}")
                    return None
                data = await resp.json()
                if "errors" in data:
                    logger.error(f"GraphQL errors: {data['errors']}")
                    return None
                return data.get("data")
        except Exception as e:
            logger.error(f"Request error: {e}")
            return None

    async def get_orders(self, status: str = "PENDING"):
        query = """
        query GetOrders($status: String) {
            orders(status: $status) {
                nodes {
                    id
                    status
                    createdAt
                    buyer { username }
                    item { id name price }
                }
            }
        }
        """
        return await self._request(query, {"status": status})

    async def get_chats(self):
        query = """
        query {
            chats {
                nodes {
                    id
                    unreadCount
                    lastMessage { text createdAt }
                    order { id buyer { username } }
                }
            }
        }
        """
        return await self._request(query)

    async def send_message(self, chat_id: str, text: str):
        query = """
        mutation SendMessage($chatId: ID!, $text: String!) {
            sendMessage(chatId: $chatId, text: $text) {
                id text createdAt
            }
        }
        """
        return await self._request(query, {"chatId": chat_id, "text": text})

    async def get_my_items(self):
        query = """
        query {
            myItems {
                nodes {
                    id name price status updatedAt
                }
            }
        }
        """
        return await self._request(query)

    async def bump_item(self, item_id: str):
        """Поднять лот"""
        query = """
        mutation BumpItem($itemId: ID!) {
            bumpItem(itemId: $itemId) {
                id updatedAt
            }
        }
        """
        return await self._request(query, {"itemId": item_id})
