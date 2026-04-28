import logging
import asyncio
import json
import certifi
import curl_cffi
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

PLAYEROK_API = "https://playerok.com/graphql"

_IMPERSONATE_PROFILES = [
    'chrome124', 'chrome131', 'chrome120', 'chrome123', 'chrome116',
    'chrome119', 'chrome107', 'chrome110', 'chrome104',
]
_profile_index = 0
_executor = ThreadPoolExecutor(max_workers=4)


def _get_session():
    global _profile_index
    profile = _IMPERSONATE_PROFILES[_profile_index % len(_IMPERSONATE_PROFILES)]
    _profile_index += 1
    return curl_cffi.Session(impersonate=profile, timeout=15, verify=certifi.where())


def _make_headers(token: str = None, operation: str = "viewer", referer: str = "https://playerok.com/") -> dict:
    h = {
        "accept": "*/*",
        "accept-language": "ru,en;q=0.9,en-GB;q=0.8,en-US;q=0.7",
        "access-control-allow-headers": "sentry-trace, baggage",
        "apollo-require-preflight": "true",
        "apollographql-client-name": "web",
        "content-type": "application/json",
        "origin": "https://playerok.com",
        "priority": "u=1, i",
        "referer": referer,
        "sec-ch-ua": '"Chromium";v="144", "Google Chrome";v="144", "Not_A Brand";v="99"',
        "sec-ch-ua-arch": '"x86"',
        "sec-ch-ua-bitness": '"64"',
        "sec-ch-ua-full-version": '"144.0.7559.110"',
        "sec-ch-ua-full-version-list": 'Not(A:Brand";v="8.0.0.0", "Chromium";v="144.0.7559.110", "Google Chrome";v="144.0.7559.110"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"19.0.0"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
        "x-gql-op": operation,
        "x-gql-path": "/",
        "x-timezone-offset": "-180",
    }
    if token:
        h["cookie"] = f"token={token}"
    return h


def _sync_get(token: str, operation: str, payload: dict) -> dict | None:
    """Синхронный GET запрос через curl_cffi.Session с params=payload"""
    headers = _make_headers(token=token, operation=operation)
    for attempt in range(3):
        try:
            session = _get_session()
            r = session.get(url=PLAYEROK_API, params=payload, headers=headers, timeout=15)
            if r.status_code != 200:
                logger.error(f"{operation} GET failed: {r.status_code} {r.text[:200]}")
                return None
            data = r.json()
            if "errors" in data:
                logger.error(f"{operation} error: {data['errors']}")
                return None
            return data.get("data")
        except Exception as e:
            logger.warning(f"{operation} GET attempt {attempt+1}: {e}")
            if attempt < 2:
                import time
                time.sleep(1)
    return None


def _sync_post(token: str, operation: str, payload: dict) -> dict | None:
    """Синхронный POST запрос через curl_cffi.Session"""
    headers = _make_headers(token=token, operation=operation)
    for attempt in range(3):
        try:
            session = _get_session()
            r = session.post(url=PLAYEROK_API, json=payload, headers=headers, timeout=15)
            if r.status_code != 200:
                logger.error(f"{operation} POST failed: {r.status_code}")
                return None
            data = r.json()
            if "errors" in data:
                logger.error(f"{operation} POST error: {data['errors']}")
                return None
            return data.get("data")
        except Exception as e:
            logger.warning(f"{operation} POST attempt {attempt+1}: {e}")
            if attempt < 2:
                import time
                time.sleep(1)
    return None


async def _run_sync(func, *args):
    """Запускаем синхронную функцию в executor"""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, func, *args)


class PlayerokAuth:
    async def send_code(self, email: str) -> tuple[bool, str]:
        query = "mutation getEmailAuthCode($email: String!) { getEmailAuthCode(input: {email: $email}) }"
        payload = {"operationName": "getEmailAuthCode", "query": query, "variables": {"email": email}}
        data = await _run_sync(_sync_post, None, "getEmailAuthCode", payload)
        if data and data.get("getEmailAuthCode"):
            return True, "Код отправлен на почту"
        return False, "Не удалось отправить код"

    async def verify_code(self, email: str, code: str) -> tuple[str | None, dict | str]:
        query = """
        mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) {
            checkEmailAuthCode(input: $input) { id username email __typename }
        }
        """
        payload = {"operationName": "checkEmailAuthCode", "query": query, "variables": {"input": {"code": code, "email": email}}}
        for attempt in range(3):
            try:
                session = _get_session()
                headers = _make_headers(operation="checkEmailAuthCode")
                r = await asyncio.get_event_loop().run_in_executor(
                    _executor, lambda: session.post(url=PLAYEROK_API, json=payload, headers=headers, timeout=20)
                )
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

    async def _get(self, operation: str, payload: dict) -> dict | None:
        return await _run_sync(_sync_get, self.token, operation, payload)

    async def _post(self, operation: str, payload: dict) -> dict | None:
        return await _run_sync(_sync_post, self.token, operation, payload)

    async def get_profile(self):
        query = "query viewer { viewer { id username email role balance { value __typename } unreadChatsCounter __typename } }"
        payload = {"operationName": "viewer", "query": query, "variables": {}}
        data = await self._post("viewer", payload)
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
        payload = {
            "operationName": "deals",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id, "direction": "OUT", "status": ["PAID"]}, "showForbiddenImage": True}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "c3b623b5fe0758cf91b2335ebf36ff65f8650a6672a792a3ca7a36d270d396fb"}})
        }
        return await self._get("deals", payload)

    async def get_chats(self, first: int = 10):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        payload = {
            "operationName": "userChats",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id}}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "999f86b7c94a4cb525ed5549d8f24d0d24036214f02a213e8fd7cefc742bbd58"}})
        }
        return await self._get("userChats", payload)

    async def get_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        payload = {
            "operationName": "items",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id, "status": ["APPROVED", "PENDING_MODERATION", "PENDING_APPROVAL"], "withOfficial": False}, "showForbiddenImage": True}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"}})
        }
        return await self._get("items", payload)

    async def get_all_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        payload = {
            "operationName": "items",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id, "status": ["APPROVED", "PENDING_MODERATION", "PENDING_APPROVAL", "REJECTED", "CLOSED", "EXPIRED"], "withOfficial": False}, "showForbiddenImage": True}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"}})
        }
        return await self._get("items", payload)

    async def send_message(self, chat_id: str, text: str):
        query = "mutation sendMessage($input: SendMessageInput!) { sendMessage(input: $input) { id text createdAt __typename } }"
        payload = {"operationName": "sendMessage", "query": query, "variables": {"input": {"chatId": chat_id, "text": text}}}
        return await self._post("sendMessage", payload)

    async def bump_item(self, item_id: str):
        query = "mutation bumpItem($id: ID!) { bumpItem(id: $id) { id priorityPosition __typename } }"
        payload = {"operationName": "bumpItem", "query": query, "variables": {"id": item_id}}
        return await self._post("bumpItem", payload)

    async def restore_item(self, item_id: str):
        query = "mutation publishItem($input: PublishItemInput!) { publishItem(input: $input) { id status __typename } }"
        payload = {"operationName": "publishItem", "query": query, "variables": {"input": {"itemId": item_id, "priorityStatuses": [], "transactionProviderId": "LOCAL"}}}
        return await self._post("publishItem", payload)

    async def mark_chat_read(self, chat_id: str):
        query = "mutation markChatAsRead($input: MarkChatAsReadInput!) { markChatAsRead(input: $input) { id unreadMessagesCounter __typename } }"
        payload = {"operationName": "markChatAsRead", "query": query, "variables": {"input": {"chatId": chat_id}}}
        return await self._post("markChatAsRead", payload)
