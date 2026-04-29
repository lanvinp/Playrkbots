import logging
import asyncio
import re
from datetime import datetime
from curl_cffi.requests import AsyncSession

logger = logging.getLogger(__name__)

PLAYEROK_API = "https://playerok.com/graphql"
PROXY = "http://O6Re7e:7pux3eFcNz@178.250.185.165:3000"

_DATETIME_FIELDS = (
    "createdAt", "updatedAt", "deletedAt", "completedAt", "confirmedAt",
    "verifiedAt", "paidAt", "statusExpirationDate", "problemReportedAt",
    "approvalDate", "postModerationCheckedAt", "startedAt", "finishedAt",
)


def _normalize_query_newlines(query: str) -> str:
    return query.replace("\\n", "\n")


def _is_datetime_serialize_null_error(message: str) -> bool:
    text = str(message or "")
    return "DateTime.serialize(" in text and "returned: null" in text


def _build_datetime_safe_query(query: str) -> str:
    normalized = _normalize_query_newlines(query)
    lines = normalized.split("\n")
    fields_set = set(_DATETIME_FIELDS)
    safe_lines = [line for line in lines if line.strip() not in fields_set]
    safe_query = "\n".join(safe_lines)
    pattern = r"\b(" + "|".join(re.escape(f) for f in _DATETIME_FIELDS) + r")\b"
    safe_query = re.sub(pattern, "", safe_query)
    safe_query = re.sub(r"[ \t]{2,}", " ", safe_query)
    safe_query = re.sub(r"\n{3,}", "\n\n", safe_query)
    return safe_query


def _get_headers(cookies: dict, operation: str = "viewer") -> dict:
    cookies_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return {
        "accept": "*/*",
        "accept-language": "ru,en-US;q=0.9,en;q=0.8",
        "access-control-allow-headers": "sentry-trace, baggage",
        "apollo-require-preflight": "true",
        "apollographql-client-name": "web",
        "content-type": "application/json",
        "if-none-match": "*",
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
        "x-timezone-offset": "-180",
        "cookie": cookies_str,
    }


async def _execute(payload: dict, cookies: dict) -> dict | None:
    base_payload = dict(payload)
    query = base_payload.get("query")
    if isinstance(query, str):
        base_payload["query"] = _normalize_query_newlines(query)

    operation = base_payload.get("operationName", "viewer")
    headers = _get_headers(cookies, operation)
    datetime_safe_retry_used = False
    active_payload = dict(base_payload)

    for attempt in range(3):
        try:
            async with AsyncSession(
                impersonate="chrome",
                proxies={"https": PROXY, "http": PROXY},
                timeout=60,
            ) as session:
                response = await session.post(
                    url=PLAYEROK_API,
                    json=active_payload,
                    headers=headers,
                    cookies=cookies,
                )

            if 200 <= response.status_code <= 304:
                try:
                    json_data = response.json() or {}
                    errors = json_data.get("errors")
                    if isinstance(errors, list) and errors:
                        raw_error = errors[0]
                        first_error = str(raw_error.get("message") if isinstance(raw_error, dict) else raw_error).strip()
                        if first_error and _is_datetime_serialize_null_error(first_error) and not datetime_safe_retry_used:
                            safe_q = _build_datetime_safe_query(active_payload.get("query", ""))
                            if safe_q and safe_q != active_payload.get("query"):
                                datetime_safe_retry_used = True
                                active_payload = {**active_payload, "query": safe_q}
                                await asyncio.sleep(1)
                                continue
                        logger.error(f"{operation} GraphQL error: {first_error}")
                        return None
                    return json_data.get("data")
                except Exception:
                    pass
                return None

            if response.status_code in {400, 401, 403, 404}:
                logger.error(f"{operation} HTTP {response.status_code}: {response.text[:200]}")
                return None

            if response.status_code == 429:
                logger.warning(f"{operation} rate limit 429, waiting 30s...")
                await asyncio.sleep(30)
                continue

        except Exception as e:
            logger.warning(f"{operation} attempt {attempt+1}/3: {e}")
            if attempt < 2:
                await asyncio.sleep(1)

    return None


class PlayerokAuth:
    async def send_code(self, email: str) -> tuple[bool, str]:
        query = "mutation getEmailAuthCode($email: String!) { getEmailAuthCode(input: {email: $email}) }"
        payload = {"operationName": "getEmailAuthCode", "query": query, "variables": {"email": email}}
        # Для авторизации используем пустые куки
        data = await _execute(payload, {})
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
                async with AsyncSession(
                    impersonate="chrome",
                    proxies={"https": PROXY, "http": PROXY},
                    timeout=60,
                ) as session:
                    response = await session.post(
                        url=PLAYEROK_API,
                        json=payload,
                        headers=_get_headers({}, "checkEmailAuthCode"),
                    )
                    token = None
                    for cookie in response.cookies.jar:
                        if cookie.name == "token" and cookie.value and len(cookie.value) > 20:
                            token = cookie.value
                            break
                    if not token:
                        raw = response.headers.get("set-cookie", "")
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
                    data = response.json()
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
        self._cookies = {"token": token}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

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
        payload = {"operationName": "viewer", "query": query, "variables": {}}
        data = await _execute(payload, self._cookies)
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
        import json
        payload = {
            "operationName": "deals",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id, "direction": "OUT", "status": ["PAID"]}, "showForbiddenImage": True}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "c3b623b5fe0758cf91b2335ebf36ff65f8650a6672a792a3ca7a36d270d396fb"}})
        }
        # GET запрос для persisted queries
        return await self._get_persisted("deals", payload)

    async def _get_persisted(self, operation: str, payload: dict):
        from urllib.parse import quote
        url = PLAYEROK_API + "?operationName=" + payload["operationName"]
        url += "&variables=" + quote(payload["variables"])
        url += "&extensions=" + quote(payload["extensions"])
        headers = _get_headers(self._cookies, operation)
        for attempt in range(3):
            try:
                async with AsyncSession(
                    impersonate="chrome",
                    proxies={"https": PROXY, "http": PROXY},
                    timeout=60,
                ) as session:
                    response = await session.get(url=url, headers=headers, cookies=self._cookies)
                if response.status_code == 200:
                    data = response.json()
                    if "errors" in data:
                        logger.error(f"{operation} error: {data['errors'][0].get('message')}")
                        return None
                    return data.get("data")
                logger.error(f"{operation} GET {response.status_code}: {response.text[:200]}")
                return None
            except Exception as e:
                logger.warning(f"{operation} attempt {attempt+1}: {e}")
                if attempt < 2:
                    await asyncio.sleep(1)
        return None

    async def get_chats(self, first: int = 10):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        import json
        payload = {
            "operationName": "userChats",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id}}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "999f86b7c94a4cb525ed5549d8f24d0d24036214f02a213e8fd7cefc742bbd58"}})
        }
        return await self._get_persisted("userChats", payload)

    async def get_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        import json
        payload = {
            "operationName": "items",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id, "status": ["APPROVED", "PENDING_MODERATION", "PENDING_APPROVAL"], "withOfficial": False}, "showForbiddenImage": True}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"}})
        }
        return await self._get_persisted("items", payload)

    async def get_all_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None
        import json
        payload = {
            "operationName": "items",
            "variables": json.dumps({"pagination": {"first": first}, "filter": {"userId": user_id, "status": ["APPROVED", "PENDING_MODERATION", "PENDING_APPROVAL"], "withOfficial": False}, "showForbiddenImage": True}),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": "63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557"}})
        }
        return await self._get_persisted("items", payload)

    async def send_message(self, chat_id: str, text: str):
        query = "mutation sendMessage($input: SendMessageInput!) { sendMessage(input: $input) { id text createdAt __typename } }"
        payload = {"operationName": "sendMessage", "query": query, "variables": {"input": {"chatId": chat_id, "text": text}}}
        return await _execute(payload, self._cookies)

    async def bump_item(self, item_id: str):
        query = "mutation bumpItem($id: ID!) { bumpItem(id: $id) { id priorityPosition __typename } }"
        payload = {"operationName": "bumpItem", "query": query, "variables": {"id": item_id}}
        return await _execute(payload, self._cookies)

    async def restore_item(self, item_id: str):
        query = """
        mutation publishItem($input: PublishItemInput!) {
            publishItem(input: $input) { id status __typename }
        }
        """
        payload = {"operationName": "publishItem", "query": query, "variables": {"input": {"itemId": item_id, "priorityStatuses": [], "transactionProviderId": "LOCAL"}}}
        return await _execute(payload, self._cookies)

    async def mark_chat_read(self, chat_id: str):
        query = "mutation markChatAsRead($input: MarkChatAsReadInput!) { markChatAsRead(input: $input) { id unreadMessagesCounter __typename } }"
        payload = {"operationName": "markChatAsRead", "query": query, "variables": {"input": {"chatId": chat_id}}}
        return await _execute(payload, self._cookies)
