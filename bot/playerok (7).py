import logging
import asyncio
import json
from urllib.parse import quote
from curl_cffi.requests import AsyncSession

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
        "sec-ch-ua": '"Chromium";v="147", "Not-A.Brand";v="8", "Google Chrome";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-gql-op": operation,
        "x-gql-path": "/",
        "x-timezone-offset": "-240",
        "x-apollo-operation-name": operation,
    }
    if token:
        h["cookie"] = f"token={token}"
    return h


def _make_get_headers(token: str = None, operation: str = "viewer") -> dict:
    """Заголовки для GET запросов — без apollo-require-preflight и content-type"""
    h = {
        "accept": "*/*",
        "accept-language": "ru,en;q=0.9",
        "apollographql-client-name": "web",
        "origin": "https://playerok.com",
        "priority": "u=1, i",
        "referer": "https://playerok.com/",
        "sec-ch-ua": '"Chromium";v="147", "Not-A.Brand";v="8", "Google Chrome";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
        "x-gql-op": operation,
        "x-gql-path": "/",
        "x-timezone-offset": "-240",
        "x-apollo-operation-name": operation,
    }
    if token:
        h["cookie"] = f"token={token}"
    return h


class PlayerokAuth:
    async def send_code(self, email: str) -> tuple[bool, str]:
        query = """
        mutation getEmailAuthCode($email: String!) {
            getEmailAuthCode(input: {email: $email})
        }
        """
        payload = {"operationName": "getEmailAuthCode", "query": query, "variables": {"email": email}}
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.post(PLAYEROK_API, json=payload, headers=_make_headers(operation="getEmailAuthCode"), timeout=20)
                    data = r.json()
                    if "errors" in data:
                        return False, data["errors"][0].get("message", "Ошибка")
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
            checkEmailAuthCode(input: $input) { id username email __typename }
        }
        """
        payload = {"operationName": "checkEmailAuthCode", "query": query, "variables": {"input": {"code": code, "email": email}}}
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.post(PLAYEROK_API, json=payload, headers=_make_headers(operation="checkEmailAuthCode"), timeout=20)
                    token = None
                    for cookie in r.cookies.jar:
                        if cookie.name == "token" and cookie.value and len(cookie.value) > 20:
                            token = cookie.value
                            break
                    if not token:
                        raw = r.headers.get("set-cookie", "")
                        for chunk in raw.split(","):
                            for part in chunk.split(";"):
                                part = part.strip()
                                if part.startswith("token=") and len(part) > 26:
                                    candidate = part[6:]
                                    if len(candidate) > 20:
                                        token = candidate
                                        break
                            if token:
                                break
                    data = r.json()
                    if "errors" in data:
                        return None, data["errors"][0].get("message", "Неверный код")
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
    def __init__(self, token: str, user_id: str = None):
        self.token = token
        self._user_id = user_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def _post(self, operation: str, query: str, variables: dict = None):
        payload = {"operationName": operation, "query": query, "variables": variables or {}}
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.post(PLAYEROK_API, json=payload, headers=_make_headers(token=self.token, operation=operation), timeout=15)
                    if r.status_code != 200:
                        logger.error(f"{operation} POST failed: {r.status_code}")
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

    async def _get_persisted(self, operation: str, variables: dict, sha256hash: str):
        """GET запрос с persisted query — правильный формат URL"""
        vars_str = json.dumps(variables, separators=(',', ':'))
        ext_str = json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha256hash}}, separators=(',', ':'))
        url = f"{PLAYEROK_API}?operationName={operation}&variables={quote(vars_str)}&extensions={quote(ext_str)}"
        for attempt in range(3):
            try:
                async with AsyncSession(impersonate="chrome") as s:
                    r = await s.get(url, headers=_make_get_headers(token=self.token, operation=operation), timeout=15)
                    if r.status_code != 200:
                        logger.error(f"{operation} GET failed: {r.status_code}")
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

    async def get_profile(self):
        query = """
        query viewer {
            viewer {
                id username email role
                balance { value __typename }
                unreadChatsCounter
                __typename
            }
        }
        """
        data = await self._post("viewer", query)
        return data.get("viewer") if data else None

    async def _get_user_id(self):
        if self._user_id:
            return self._user_id
        profile = await self.get_profile()
        if profile:
            self._user_id = profile.get("id")
        return self._user_id

    async def get_orders(self, first: int = 20):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        variables = {
            "pagination": {"first": first},
            "filter": {"userId": user_id, "direction": "OUT", "status": ["PAID"]},
            "showForbiddenImage": True
        }
        return await self._get_persisted(
            "deals", variables,
            "c3b623b5fe0758cf91b2335ebf36ff65f8650a6672a792a3ca7a36d270d396fb"
        )

    async def get_chats(self, first: int = 10):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        variables = {"pagination": {"first": first}, "filter": {"userId": user_id}}
        return await self._get_persisted(
            "userChats", variables,
            "999f86b7c94a4cb525ed5549d8f24d0d24036214f02a213e8fd7cefc742bbd58"
        )

    async def get_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        variables = {
            "pagination": {"first": first},
            "filter": {"userId": user_id, "status": ["APPROVED", "PENDING_MODERATION", "PENDING_APPROVAL"], "withOfficial": False},
            "showForbiddenImage": True
        }
        return await self._get_persisted(
            "items", variables,
            "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"
        )

    async def get_all_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        variables = {
            "pagination": {"first": first},
            "filter": {"userId": user_id, "status": ["APPROVED", "PENDING_MODERATION", "PENDING_APPROVAL", "REJECTED", "CLOSED"], "withOfficial": False},
            "showForbiddenImage": True
        }
        return await self._get_persisted(
            "items", variables,
            "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"
        )

    async def send_message(self, chat_id: str, text: str):
        query = "mutation sendMessage($input: SendMessageInput!) { sendMessage(input: $input) { id text createdAt __typename } }"
        return await self._post("sendMessage", query, {"input": {"chatId": chat_id, "text": text}})

    async def bump_item(self, item_id: str):
        query = "mutation bumpItem($id: ID!) { bumpItem(id: $id) { id priorityPosition __typename } }"
        return await self._post("bumpItem", query, {"id": item_id})

    async def restore_item(self, item_id: str):
        query = "mutation restoreItem($id: ID!) { restoreItem(id: $id) { id status __typename } }"
        return await self._post("restoreItem", query, {"id": item_id})

    async def mark_chat_read(self, chat_id: str):
        query = "mutation markChatAsRead($input: MarkChatAsReadInput!) { markChatAsRead(input: $input) { id unreadMessagesCounter __typename } }"
        return await self._post("markChatAsRead", query, {"input": {"chatId": chat_id}})
