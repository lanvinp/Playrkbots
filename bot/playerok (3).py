import logging
import asyncio
from curl_cffi import AsyncSession

logger = logging.getLogger(__name__)

PLAYEROK_API = "https://playerok.com/graphql"


def _make_headers(token: str = None, operation: str = "viewer") -> dict:
    h = {
        "accept": "*/*",
        "accept-language": "ru,en;q=0.9",
        "apollo-require-preflight": "true",
        "apollographql-client-name": "web",
        "content-type": "application/json",
        "origin": "https://playerok.com",
        "priority": "u=1, i",
        "referer": "https://playerok.com/",
        "sec-ch-ua": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "x-gql-op": operation,
        "x-gql-path": "/",
        "x-timezone-offset": "-240",
        "x-apollo-operation-name": operation,
    }
    if token:
        h["cookie"] = f"token={token}"
    return h


class PlayerokAuth:
    """Авторизация через почту — без прокси, VPS пускает напрямую"""

    async def send_code(self, email: str) -> tuple[bool, str]:
        query = """
        mutation getEmailAuthCode($email: String!) {
            getEmailAuthCode(input: {email: $email})
        }
        """
        payload = {
            "operationName": "getEmailAuthCode",
            "query": query,
            "variables": {"email": email},
        }
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.post(
                        PLAYEROK_API,
                        json=payload,
                        headers=_make_headers(operation="getEmailAuthCode"),
                        timeout=20,
                    )
                    data = r.json()
                    if "errors" in data:
                        msg = data["errors"][0].get("message", "Ошибка")
                        return False, msg
                    if data.get("data", {}).get("getEmailAuthCode"):
                        return True, "Код отправлен на почту"
                    return False, "Не удалось отправить код"
            except Exception as e:
                logger.warning(f"send_code attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
        return False, "Нет соединения с сервером"

    async def verify_code(self, email: str, code: str) -> tuple[str | None, dict | str]:
        query = """
        mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) {
            checkEmailAuthCode(input: $input) {
                id
                username
                email
                __typename
            }
        }
        """
        payload = {
            "operationName": "checkEmailAuthCode",
            "query": query,
            "variables": {"input": {"code": code, "email": email}},
        }
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.post(
                        PLAYEROK_API,
                        json=payload,
                        headers=_make_headers(operation="checkEmailAuthCode"),
                        timeout=20,
                    )
                    # Токен приходит в Set-Cookie
                    token = None
                    cookie_header = r.headers.get("set-cookie", "")
                    for part in cookie_header.split(";"):
                        part = part.strip()
                        if part.startswith("token=") and len(part) > 10:
                            token = part[6:]
                            break

                    # Также ищем в cookies объекте
                    if not token:
                        for cookie in r.cookies.jar:
                            if cookie.name == "token" and cookie.value and len(cookie.value) > 10:
                                token = cookie.value
                                break

                    data = r.json()
                    if "errors" in data:
                        msg = data["errors"][0].get("message", "Неверный код")
                        return None, msg

                    user = data.get("data", {}).get("checkEmailAuthCode", {})
                    if token:
                        return token, user
                    return None, "Не удалось получить токен"

            except Exception as e:
                logger.warning(f"verify_code attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
        return None, "Нет соединения с сервером"


class PlayerokClient:
    """Клиент для работы с API — токен передаётся через cookie"""

    def __init__(self, token: str):
        self.token = token

    async def _req(self, operation: str, query: str, variables: dict = None):
        payload = {
            "operationName": operation,
            "query": query,
            "variables": variables or {},
        }
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.post(
                        PLAYEROK_API,
                        json=payload,
                        headers=_make_headers(token=self.token, operation=operation),
                        timeout=15,
                    )
                    if r.status_code != 200:
                        logger.error(f"{operation} failed: {r.status_code}")
                        return None
                    data = r.json()
                    if "errors" in data:
                        logger.error(f"{operation} error: {data['errors']}")
                        return None
                    return data.get("data")
            except Exception as e:
                logger.warning(f"{operation} attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    async def get_orders(self, status: str = "PENDING"):
        query = """
        query GetOrders($status: String) {
            orders(status: $status) {
                nodes {
                    id status createdAt
                    buyer { username }
                    item { id name price }
                }
            }
        }
        """
        return await self._req("GetOrders", query, {"status": status})

    async def get_chats(self):
        query = """
        query getChats {
            chats {
                nodes {
                    id unreadCount
                    lastMessage { text createdAt }
                    order { id buyer { username } }
                }
            }
        }
        """
        return await self._req("getChats", query)

    async def send_message(self, chat_id: str, text: str):
        query = """
        mutation SendMessage($chatId: ID!, $text: String!) {
            sendMessage(chatId: $chatId, text: $text) {
                id text createdAt
            }
        }
        """
        return await self._req("SendMessage", query, {"chatId": chat_id, "text": text})

    async def get_my_items(self):
        query = """
        query getMyItems {
            myItems {
                nodes { id name price status updatedAt }
            }
        }
        """
        return await self._req("getMyItems", query)

    async def bump_item(self, item_id: str):
        query = """
        mutation BumpItem($itemId: ID!) {
            bumpItem(itemId: $itemId) { id updatedAt }
        }
        """
        return await self._req("BumpItem", query, {"itemId": item_id})

    async def get_profile(self):
        query = """
        query viewer {
            viewer { id username email }
        }
        """
        data = await self._req("viewer", query)
        return data.get("viewer") if data else None
