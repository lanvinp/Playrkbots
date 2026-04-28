import logging
import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=4)


async def _run_sync(func, *args):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_executor, func, *args)


class PlayerokAuth:
    async def send_code(self, email: str) -> tuple[bool, str]:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from pok.conn import Conn
        from pok.gql import QUERIES

        def _do():
            conn = Conn.__new__(Conn)
            conn.token = ""
            conn._Conn__proxy_string = None
            conn.requests_timeout = 15
            conn.request_max_retries = 5
            conn.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36'
            import certifi
            conn._ca_bundle = certifi.where()
            conn._refresh_clients()
            import logging
            conn.logger = logging.getLogger('pl.conn')

            payload = {
                'operationName': 'getEmailAuthCode',
                'query': 'mutation getEmailAuthCode($email: String!) { getEmailAuthCode(input: {email: $email}) }',
                'variables': {'email': email}
            }
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            data = r.json()
            if 'errors' in data:
                return False, data['errors'][0].get('message', 'Ошибка')
            if data.get('data', {}).get('getEmailAuthCode'):
                return True, 'Код отправлен на почту'
            return False, 'Не удалось отправить код'

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"send_code error: {e}")
            return False, str(e)

    async def verify_code(self, email: str, code: str) -> tuple[str | None, dict | str]:
        import sys, os
        sys.path.insert(0, os.path.dirname(__file__))
        from pok.conn import Conn

        def _do():
            conn = Conn.__new__(Conn)
            conn.token = ""
            conn._Conn__proxy_string = None
            conn.requests_timeout = 20
            conn.request_max_retries = 5
            conn.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36'
            import certifi, logging
            conn._ca_bundle = certifi.where()
            conn._refresh_clients()
            conn.logger = logging.getLogger('pl.conn')

            query = """
            mutation checkEmailAuthCode($input: CheckEmailAuthCodeInput!) {
                checkEmailAuthCode(input: $input) { id username email __typename }
            }
            """
            payload = {"operationName": "checkEmailAuthCode", "query": query, "variables": {"input": {"code": code, "email": email}}}
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)

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
            if 'errors' in data:
                return None, data['errors'][0].get('message', 'Неверный код')
            user = data.get('data', {}).get('checkEmailAuthCode', {})
            if token:
                return token, user
            return None, 'Не удалось получить токен'

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"verify_code error: {e}")
            return None, str(e)


def _make_conn(token: str):
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from pok.conn import Conn
    import certifi, logging as _logging

    if hasattr(Conn, 'instance'):
        del Conn.instance

    conn = Conn.__new__(Conn)
    conn.token = token
    conn._Conn__proxy_string = None
    conn.requests_timeout = 15
    conn.request_max_retries = 5
    conn.user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36'
    conn._ca_bundle = certifi.where()
    conn._refresh_clients()
    conn.logger = _logging.getLogger('pl.conn')
    return conn


class PlayerokClient:
    def __init__(self, token: str, user_id: str = None):
        self.token = token
        self._user_id = user_id

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def _conn(self):
        return _make_conn(self.token)

    async def get_profile(self):
        from pok.gql import QUERIES

        def _do():
            conn = self._conn()
            payload = {'operationName': 'viewer', 'query': QUERIES.get('viewer'), 'variables': {}}
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data', {}).get('viewer')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"get_profile error: {e}")
            return None

    async def _get_user_id(self):
        if self._user_id:
            return self._user_id
        profile = await self.get_profile()
        if profile:
            self._user_id = profile.get('id')
        return self._user_id

    async def get_orders(self, first: int = 20):
        user_id = await self._get_user_id()
        if not user_id:
            return None

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'deals',
                'variables': json.dumps({'pagination': {'first': first}, 'filter': {'userId': user_id, 'direction': 'OUT', 'status': ['PAID']}, 'showForbiddenImage': True}),
                'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': 'c3b623b5fe0758cf91b2335ebf36ff65f8650a6672a792a3ca7a36d270d396fb'}})
            }
            r = conn.request('get', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"get_orders error: {e}")
            return None

    async def get_chats(self, first: int = 10):
        user_id = await self._get_user_id()
        if not user_id:
            return None

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'userChats',
                'variables': json.dumps({'pagination': {'first': first}, 'filter': {'userId': user_id}}),
                'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': '999f86b7c94a4cb525ed5549d8f24d0d24036214f02a213e8fd7cefc742bbd58'}})
            }
            r = conn.request('get', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"get_chats error: {e}")
            return None

    async def get_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'items',
                'variables': json.dumps({'pagination': {'first': first}, 'filter': {'userId': user_id, 'status': ['APPROVED', 'PENDING_MODERATION', 'PENDING_APPROVAL'], 'withOfficial': False}, 'showForbiddenImage': True}),
                'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': '63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557'}})
            }
            r = conn.request('get', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"get_my_items error: {e}")
            return None

    async def get_all_my_items(self, first: int = 50):
        user_id = await self._get_user_id()
        if not user_id:
            return None

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'items',
                'variables': json.dumps({'pagination': {'first': first}, 'filter': {'userId': user_id, 'status': ['APPROVED', 'PENDING_MODERATION', 'PENDING_APPROVAL', 'REJECTED', 'CLOSED'], 'withOfficial': False}, 'showForbiddenImage': True}),
                'extensions': json.dumps({'persistedQuery': {'version': 1, 'sha256Hash': '63eefcfd813442882ad846360d925279bc376e8bc85a577ebefbee0f9c78b557'}})
            }
            r = conn.request('get', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"get_all_my_items error: {e}")
            return None

    async def send_message(self, chat_id: str, text: str):
        from pok.gql import QUERIES

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'createChatMessage',
                'query': QUERIES.get('createChatMessage'),
                'variables': {'input': {'chatId': chat_id, 'text': text}}
            }
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"send_message error: {e}")
            return None

    async def bump_item(self, item_id: str):
        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'bumpItem',
                'query': 'mutation bumpItem($id: ID!) { bumpItem(id: $id) { id priorityPosition __typename } }',
                'variables': {'id': item_id}
            }
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"bump_item error: {e}")
            return None

    async def restore_item(self, item_id: str):
        from pok.gql import QUERIES

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'publishItem',
                'query': QUERIES.get('publishItem'),
                'variables': {'input': {'itemId': item_id, 'priorityStatuses': [], 'transactionProviderId': 'LOCAL'}}
            }
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"restore_item error: {e}")
            return None

    async def mark_chat_read(self, chat_id: str):
        from pok.gql import QUERIES

        def _do():
            conn = self._conn()
            payload = {
                'operationName': 'markChatAsRead',
                'query': QUERIES.get('markChatAsRead'),
                'variables': {'input': {'chatId': chat_id}}
            }
            r = conn.request('post', 'https://playerok.com/graphql', {'accept': '*/*'}, payload)
            return r.json().get('data')

        try:
            return await _run_sync(_do)
        except Exception as e:
            logger.error(f"mark_chat_read error: {e}")
            return None
