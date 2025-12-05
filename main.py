import logging
import json
import sqlite3
import asyncio
import random
import tempfile
import os
import threading
import html
from typing import Final, Optional, Tuple, List, Callable, Awaitable

import aiohttp
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

from cfg import TOKEN_BOTA


BOT_TOKEN: Final = TOKEN_BOTA

CHANGE_PAYMENT_URL: Final = "https://tc.mobile.yandex.net/3.0/changepayment"
DB_PATH: Final = "bot.db"
PROXY_FILE: Final = "proxy.txt"

(
    ASK_TOKEN,
    ASK_ORDERID,
    ASK_CARD,
    ASK_ID,
    MENU,
    REMEMBER_CARD,
    ASK_THREADS,
    ASK_TOTAL_REQUESTS,
    ASK_LOG_SESSION_ID,
    ASK_TRIP_VALUE,
    ASK_STREAM_TOKEN,
    ASK_STREAM_ORDERID,
    ASK_STREAM_CARD,
    ASK_STREAM_ID,
    ASK_STREAM_THREADS,
    ASK_STREAM_TOTAL,
) = range(16)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)



PROXIES: List[str] = []
_proxy_cycle = None
_proxy_lock = threading.Lock()


class ChangePaymentClient:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.proxy_pool: List[str] = []
        self._session: Optional[aiohttp.ClientSession] = None
        self._proxy_index = 0

    async def start(self):
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self):
        if self._session is not None:
            await self._session.close()
            self._session = None

    def update_proxies(self, proxies: List[str]):
        self.proxy_pool = proxies
        self._proxy_index = 0

    def _next_proxy(self) -> Optional[str]:
        if not self.proxy_pool:
            return None
        proxy = self.proxy_pool[self._proxy_index]
        self._proxy_index = (self._proxy_index + 1) % len(self.proxy_pool)
        return proxy

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
        progress_cb: Optional[
            Callable[[int, int, int, Optional[str]], Awaitable[None]]
        ] = None,
        stop_event: Optional[asyncio.Event] = None,
    ) -> Tuple[int, int]:
        await self.client.start()
        stop_event = stop_event or asyncio.Event()

        completed = 0
        success = 0
        semaphore = asyncio.Semaphore(concurrency)

        async def _job(idx: int):
            nonlocal completed, success
            async with semaphore:
                if stop_event.is_set():
                    return

                ok, status_code, response_text = await self.send_one(
                    tg_id, headers, payload, session_id, use_proxies
                )

                completed += 1
                if ok and status_code is not None and 200 <= status_code < 300:
                    success += 1

                if progress_cb:
                    await progress_cb(completed, success, status_code or 0, response_text)

                await asyncio.sleep(0.3)

        tasks = [asyncio.create_task(_job(i)) for i in range(total_requests)]
        await asyncio.gather(*tasks, return_exceptions=True)
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


def get_next_proxy() -> Optional[str]:
    global _proxy_cycle
    if not PROXIES or _proxy_cycle is None:
        return None
    with _proxy_lock:
        try:
            return next(_proxy_cycle)
        except StopIteration:
            return None



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
        pass  # уже есть

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS rec_card (
            tg_id INTEGER PRIMARY KEY,
            card TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS trip_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER NOT NULL,
            token2 TEXT,
            trip_id TEXT,
            card TEXT,
            orderid TEXT,
            trip_link TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

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


def save_card_for_user(tg_id: int, card: str):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO rec_card (tg_id, card)
        VALUES (?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            card = excluded.card,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (tg_id, card),
    )
    conn.commit()
    conn.close()


def get_saved_card_for_user(tg_id: int) -> Optional[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT card FROM rec_card WHERE tg_id = ?;", (tg_id,))
    row = cur.fetchone()
    conn.close()
    if row:
        return row[0]
    return None


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


def get_trip_template(trip_id: int, tg_id: int) -> Optional[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, token2, trip_id, card, orderid, trip_link
        FROM trip_templates
        WHERE id = ? AND tg_id = ?
        LIMIT 1;
        """,
        (trip_id, tg_id),
    )
    row = cur.fetchone()
    conn.close()
    if row:
        keys = ["id", "token2", "trip_id", "card", "orderid", "trip_link"]
        return dict(zip(keys, row))
    return None


def update_trip_template_field(trip_id: int, tg_id: int, field: str, value: str) -> None:
    if field not in {"token2", "trip_id", "card", "orderid", "trip_link"}:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE trip_templates SET {field} = ? WHERE id = ? AND tg_id = ?;",
        (value, trip_id, tg_id),
    )
    conn.commit()
    conn.close()


def list_trip_templates(tg_id: int) -> List[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, token2, trip_id, card, orderid, trip_link, created_at
        FROM trip_templates
        WHERE tg_id = ?
        ORDER BY id DESC;
        """,
        (tg_id,),
    )
    rows = cur.fetchall()
    conn.close()
    keys = ["id", "token2", "trip_id", "card", "orderid", "trip_link", "created_at"]
    return [dict(zip(keys, row)) for row in rows]


def delete_trip_template(trip_id: int, tg_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM trip_templates WHERE id = ? AND tg_id = ?;", (trip_id, tg_id))
    conn.commit()
    conn.close()


def clear_trip_template(trip_id: int, tg_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE trip_templates
        SET token2 = NULL,
            trip_id = NULL,
            card = NULL,
            orderid = NULL,
            trip_link = NULL
        WHERE id = ? AND tg_id = ?;
        """,
        (trip_id, tg_id),
    )
    conn.commit()
    conn.close()


def export_session_logs_to_file(tg_id: int, session_id: str) -> Optional[str]:

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, created_at, status_code, response_body
        FROM requests
        WHERE tg_id = ? AND session_id = ?
        ORDER BY id;
        """,
        (tg_id, session_id),
    )
    rows = cur.fetchall()
    conn.close()

    if not rows:
        return None

    fd, path = tempfile.mkstemp(suffix=".txt", prefix=f"logs_{session_id}_")
    os.close(fd)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"TG ID: {tg_id}\n")
        f.write(f"Session ID: {session_id}\n")
        f.write(f"Всего записей: {len(rows)}\n")
        f.write("=" * 50 + "\n\n")

        for idx, (req_id, created_at, status_code, response_body) in enumerate(
            rows, start=1
        ):
            f.write(f"Запрос #{idx} (DB id={req_id})\n")
            f.write(f"Время: {created_at}\n")
            f.write(f"HTTP статус: {status_code}\n")
            f.write("Ответ:\n")
            f.write(response_body if response_body is not None else "")
            f.write("\n" + "-" * 40 + "\n\n")

    return path



def build_headers(user_token: str) -> dict:
    return {
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "ru",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "ru.yandex.ytaxi/700.100.0.500995 (iPhone; iPhone14,4; iOS 18.3.1; Darwin)",
        "Authorization": f"Bearer {user_token}",
    }


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


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Заебашить", "Профиль"],
            ["Изменить поездки"],
            ["Прокси/аккаунты", "Логи"],
        ],
        resize_keyboard=True,
    )


def actions_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Одиночная смена"],
            ["Запустить потоки"],
            ["Остановить потоки"],
            ["Назад"],
        ],
        resize_keyboard=True,
    )


def logs_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Посмотреть логи"],
            ["Логи последней сессии"],
            ["Назад"],
        ],
        resize_keyboard=True,
    )


def proxy_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Перезагрузить прокси"],
            ["Прокси вкл/выкл"],
            ["Загрузить поездки"],
            ["Назад"],
        ],
        resize_keyboard=True,
    )


def _field_icon(value: Optional[str]) -> str:
    return "✅" if value else "⬜"


def ensure_active_trip_record(tg_id: int, context: ContextTypes.DEFAULT_TYPE) -> dict:
    trip_id = context.user_data.get("active_trip_id")
    record = None
    if trip_id:
        record = get_trip_template(trip_id, tg_id)

    if record is None:
        trip_id = create_trip_template(tg_id)
        context.user_data["active_trip_id"] = trip_id
        record = get_trip_template(trip_id, tg_id) or {}

    set_trip_form_mode(context, trip_id, "create")

    return record


def set_trip_form_mode(context: ContextTypes.DEFAULT_TYPE, trip_id: int, mode: str):
    modes = context.user_data.setdefault("trip_form_mode", {})
    modes[trip_id] = mode


def get_trip_form_mode(context: ContextTypes.DEFAULT_TYPE, trip_id: int) -> str:
    modes = context.user_data.get("trip_form_mode", {})
    return modes.get(trip_id, "create")


def _trip_has_values(record: dict) -> bool:
    return any(record.get(field) for field in ("token2", "trip_id", "card", "orderid", "trip_link"))


def trip_form_markup(record: dict, *, mode: str = "create") -> InlineKeyboardMarkup:
    trip_id = record.get("id")
    buttons = [
        [
            InlineKeyboardButton(
                f"{_field_icon(record.get('token2'))} token2",
                callback_data=f"tripfield:{trip_id}:token2",
            )
        ],
        [
            InlineKeyboardButton(
                f"{_field_icon(record.get('trip_id'))} ID",
                callback_data=f"tripfield:{trip_id}:trip_id",
            )
        ],
        [
            InlineKeyboardButton(
                f"{_field_icon(record.get('card'))} card-x",
                callback_data=f"tripfield:{trip_id}:card",
            )
        ],
        [
            InlineKeyboardButton(
                f"{_field_icon(record.get('orderid'))} orderid",
                callback_data=f"tripfield:{trip_id}:orderid",
            )
        ],
        [
            InlineKeyboardButton(
                f"{_field_icon(record.get('trip_link'))} ссылка на поездку",
                callback_data=f"tripfield:{trip_id}:trip_link",
            )
        ],
    ]

    if _trip_has_values(record):
        save_caption = "Сохранить параметры" if mode == "edit" else "Сохранить"
        control_row = [InlineKeyboardButton(save_caption, callback_data=f"tripsave:{trip_id}")]
        if mode == "create":
            control_row.append(InlineKeyboardButton("Очистить", callback_data=f"tripclear:{trip_id}"))
        buttons.append(control_row)

    return InlineKeyboardMarkup(buttons)


async def show_trip_loader(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None

    if tg_id is None:
        await update.message.reply_text(
            "Не смог получить твой TG ID 🤔", reply_markup=main_keyboard()
        )
        return MENU

    record = ensure_active_trip_record(tg_id, context)
    text = (
        "Загрузи поездку. Нажми на нужный параметр, введи данные, и они сразу"
        " запишутся в БД. После заполнения любого поля появятся кнопки"
        " «Сохранить» и «Очистить»."
    )
    await update.message.reply_text(
        text,
        reply_markup=trip_form_markup(record, mode=get_trip_form_mode(context, record.get("id", 0))),
    )
    return MENU


async def tripfield_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str, field = query.data.split(":", 2)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, какую ячейку нужно заполнить.")
        return MENU

    context.user_data["active_trip_id"] = trip_id
    context.user_data["pending_trip_input"] = {
        "trip_id": trip_id,
        "field": field,
    }

    field_names = {
        "token2": "token2",
        "trip_id": "ID",
        "card": "card-x",
        "orderid": "orderid",
        "trip_link": "ссылку на поездку",
    }
    await query.message.reply_text(
        f"Введи {field_names.get(field, 'значение')} для этой поездки:",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_TRIP_VALUE


async def trip_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None
    pending = context.user_data.get("pending_trip_input")

    if tg_id is None or not isinstance(pending, dict):
        await update.message.reply_text(
            "Не нашёл активный слот для сохранения. Нажми «Загрузить поездки» снова.",
            reply_markup=main_keyboard(),
        )
        return MENU

    trip_id = int(pending.get("trip_id", 0))
    field = pending.get("field")
    value = update.message.text.strip()

    update_trip_template_field(trip_id, tg_id, field, value)
    context.user_data.pop("pending_trip_input", None)
    context.user_data["active_trip_id"] = trip_id

    record = get_trip_template(trip_id, tg_id) or {}
    await update.message.reply_text(
        "Сохранил ✅ Данные записаны в таблицу.",
        reply_markup=trip_form_markup(record, mode=get_trip_form_mode(context, trip_id)),
    )
    return MENU


def stream_start_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Выбрать из уже созданных", callback_data="streams:choose")],
            [InlineKeyboardButton("Создать своё", callback_data="streams:create")],
        ]
    )


async def send_trip_templates_list(
    chat, tg_id: int, context: ContextTypes.DEFAULT_TYPE
):
    templates = list_trip_templates(tg_id)
    if not templates:
        await chat.reply_text(
            "Нет сохранённых поездок. Сначала нажми «Загрузить поездки» и заполни поля.",
            reply_markup=main_keyboard(),
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                f"#{t['id']} | {t.get('orderid') or 'orderid не задан'}",
                callback_data=f"tripselect:{t['id']}",
            )
        ]
        for t in templates
    ]

    await chat.reply_text(
        "Выбери одну из сохранённых поездок:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_trip_manager(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None

    if tg_id is None:
        await update.message.reply_text(
            "Не смог получить твой TG ID 🤔", reply_markup=main_keyboard()
        )
        return MENU

    await send_trip_manager_list(update.message, tg_id, context)
    return MENU


async def send_trip_manager_list(chat, tg_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    templates = list_trip_templates(tg_id)
    if not templates:
        await chat.reply_text(
            "Пока нет сохранённых поездок. Сперва нажми «Загрузить поездки».",
            reply_markup=main_keyboard(),
        )
        return False

    keyboard = [
        [
            InlineKeyboardButton(
                f"#{t['id']} | {t.get('orderid') or 'orderid не задан'}",
                callback_data=f"tripmanage:{t['id']}",
            )
        ]
        for t in templates
    ]
    await chat.reply_text(
        "Выбери поездку для редактирования или удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return True


async def streams_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data.split(":", 1)[1]

    if choice == "create":
        context.user_data["stream_config"] = {}
        await query.message.reply_text(
            "Создаём новый набор данных для потоков. Введи token2:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_STREAM_TOKEN

    if choice == "choose":
        user = update.effective_user
        tg_id = user.id if user else None
        if tg_id is None:
            await query.message.reply_text(
                "Не смог получить твой TG ID 🤔", reply_markup=main_keyboard()
            )
            return MENU

        await send_trip_templates_list(query.message, tg_id, context)
        return MENU

    await query.message.reply_text("Непонятный выбор.", reply_markup=main_keyboard())
    return MENU


async def trip_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что открыть.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None
    record = get_trip_template(trip_id, tg_id or 0) if tg_id else None

    if not record:
        await query.message.reply_text("Не нашёл такую запись в БД.")
        return MENU

    text_lines = [
        f"ID записи: {record['id']}",
        f"token2: {record.get('token2') or '—'}",
        f"ID: {record.get('trip_id') or '—'}",
        f"card-x: {record.get('card') or '—'}",
        f"orderid: {record.get('orderid') or '—'}",
        f"Ссылка: {record.get('trip_link') or '—'}",
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Использовать в смене", callback_data=f"tripuse:{record['id']}"
                ),
                InlineKeyboardButton(
                    "Удалить из БД", callback_data=f"tripdelete:{record['id']}"
                ),
            ]
        ]
    )

    await query.message.reply_text("\n".join(text_lines), reply_markup=keyboard)
    return MENU


async def trip_manage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что открыть.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None

    if trip_id_str == "back":
        if tg_id is None:
            await query.message.reply_text(
                "Не смог получить TG ID.", reply_markup=main_keyboard()
            )
            return MENU
        await send_trip_manager_list(query.message, tg_id, context)
        return MENU

    try:
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что открыть.")
        return MENU

    record = get_trip_template(trip_id, tg_id or 0) if tg_id else None
    if not record:
        await query.message.reply_text("Не нашёл такую запись в БД.")
        return MENU

    set_trip_form_mode(context, trip_id, "edit")

    text_lines = [
        f"ID записи: {record['id']}",
        f"token2: {record.get('token2') or '—'}",
        f"ID: {record.get('trip_id') or '—'}",
        f"card-x: {record.get('card') or '—'}",
        f"orderid: {record.get('orderid') or '—'}",
        f"Ссылка: {record.get('trip_link') or '—'}",
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Редактировать", callback_data=f"tripedit:{record['id']}")],
            [InlineKeyboardButton("Удалить из БД", callback_data=f"tripdelete:{record['id']}")],
            [InlineKeyboardButton("Назад к списку", callback_data="tripmanage:back")],
        ]
    )

    await query.message.reply_text("\n".join(text_lines), reply_markup=keyboard)
    return MENU


async def trip_edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что редактировать.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None
    record = get_trip_template(trip_id, tg_id or 0) if tg_id else None

    if not record:
        await query.message.reply_text("Не нашёл запись для редактирования.")
        return MENU

    set_trip_form_mode(context, trip_id, "edit")
    context.user_data["active_trip_id"] = trip_id

    await query.message.reply_text(
        "Редактируем поездку. Нажми на параметр, укажи новое значение и затем"
        " нажми «Сохранить параметры».",
        reply_markup=trip_form_markup(record, mode="edit"),
    )
    return MENU


async def trip_save_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что сохранять.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None
    record = get_trip_template(trip_id, tg_id or 0) if tg_id else None
    if not record:
        await query.message.reply_text("Не нашёл запись в БД.")
        return MENU

    context.user_data["active_trip_id"] = trip_id
    await query.message.reply_text(
        "Параметры сохранены в БД.",
        reply_markup=trip_form_markup(record, mode=get_trip_form_mode(context, trip_id)),
    )
    return MENU


async def trip_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что очистить.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None
    if tg_id is None:
        await query.message.reply_text("Не смог получить TG ID.")
        return MENU

    clear_trip_template(trip_id, tg_id)
    context.user_data.pop("pending_trip_input", None)
    context.user_data["active_trip_id"] = trip_id
    set_trip_form_mode(context, trip_id, "create")
    record = get_trip_template(trip_id, tg_id) or {}

    await query.message.reply_text(
        "Очистил все поля. Можешь заполнять заново.",
        reply_markup=trip_form_markup(record, mode="create"),
    )
    return MENU


async def trip_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что удалить.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None
    if tg_id is None:
        await query.message.reply_text("Не смог получить TG ID.")
        return MENU

    delete_trip_template(trip_id, tg_id)
    await query.message.reply_text("Удалил запись из БД.", reply_markup=main_keyboard())
    await send_trip_manager_list(query.message, tg_id, context)
    return MENU


async def trip_use_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, trip_id_str = query.data.split(":", 1)
        trip_id = int(trip_id_str)
    except Exception:  # noqa: BLE001
        await query.message.reply_text("Не понял, что использовать.")
        return MENU

    user = update.effective_user
    tg_id = user.id if user else None
    record = get_trip_template(trip_id, tg_id or 0) if tg_id else None
    if not record:
        await query.message.reply_text("Не нашёл запись.")
        return MENU

    context.user_data["token"] = record.get("token2")
    context.user_data["orderid"] = record.get("orderid")
    context.user_data["card"] = record.get("card")
    context.user_data["id"] = record.get("trip_id")

    await context.bot.send_message(
        chat_id=user.id,
        text="Данные перенесены в смену. Сколько потоков запустить?",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ASK_THREADS


async def stream_token_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    context.user_data.setdefault("stream_config", {})["token"] = token
    await update.message.reply_text(
        "Принял token2. Теперь введи orderid:", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_STREAM_ORDERID


async def stream_orderid_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orderid = update.message.text.strip()
    context.user_data.setdefault("stream_config", {})["orderid"] = orderid
    await update.message.reply_text(
        "Теперь card-x:", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_STREAM_CARD


async def stream_card_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = update.message.text.strip()
    context.user_data.setdefault("stream_config", {})["card"] = card
    await update.message.reply_text(
        "Введи ID:", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_STREAM_ID


async def stream_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = update.message.text.strip()
    context.user_data.setdefault("stream_config", {})["id"] = val
    await update.message.reply_text(
        "Сколько потоков запустить?", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_STREAM_THREADS


async def stream_threads_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        threads = int(text)
        if threads <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно целое положительное число потоков.", reply_markup=main_keyboard()
        )
        return MENU

    context.user_data.setdefault("stream_config", {})["threads"] = threads
    await update.message.reply_text(
        "Сколько всего запросов сделать?", reply_markup=ReplyKeyboardRemove()
    )
    return ASK_STREAM_TOTAL


async def stream_total_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        total_requests = int(text)
        if total_requests <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно целое положительное число запросов.", reply_markup=main_keyboard()
        )
        return MENU

    config = context.user_data.get("stream_config", {})
    required = [config.get("token"), config.get("orderid"), config.get("card"), config.get("id")]
    if not all(required):
        await update.message.reply_text(
            "Не все данные заданы. Попробуй снова через «Запустить потоки».",
            reply_markup=main_keyboard(),
        )
        return MENU
    context.user_data["token"] = config.get("token")
    context.user_data["orderid"] = config.get("orderid")
    context.user_data["card"] = config.get("card")
    context.user_data["id"] = config.get("id")

    threads = config.get("threads", 1)
    context.user_data["threads"] = threads
    await bulk_change_payment(update, context, threads, total_requests)
    return MENU



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "use_proxies" not in context.user_data:
        context.user_data["use_proxies"] = True

    use_proxies = context.user_data["use_proxies"]
    proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"

    await update.message.reply_text(
        "Привет! 👋\n"
        "Я бот для отправки запроса changepayment.\n\n"
        "Нажми «Заебашить», там выбери «Одиночная смена» или «Запустить потоки».\n"
        "Для управления прокси и поездками жми «Прокси/аккаунты».\n\n"
        f"Текущее состояние прокси: {proxy_state}",
        reply_markup=main_keyboard(),
    )
    return MENU


async def start_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data

    if choice == "single":
        await query.message.reply_text(
            "Окей, погнали. 🚀\n"
            "Сначала отправь токен (только сам <token>, без Bearer):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_TOKEN

    if choice == "bulk":
        await query.message.reply_text(
            "Выбрал массовый запуск. Сначала введи параметры через «Заебашить»,"
            " а потом нажми «Запустить потоки».",
            reply_markup=main_keyboard(),
        )
        return MENU

    await query.message.reply_text("Неизвестный выбор.", reply_markup=main_keyboard())
    return MENU


async def ask_token(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    context.user_data["token"] = token

    await update.message.reply_text(
        "Ок. Теперь отправь, пожалуйста, <orderid>:"
    )
    return ASK_ORDERID


async def ask_orderid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    orderid = update.message.text.strip()
    context.user_data["orderid"] = orderid

    user = update.effective_user
    tg_id = user.id if user else None

    saved_card = get_saved_card_for_user(tg_id) if tg_id is not None else None

    if saved_card:
        context.user_data["card"] = saved_card
        await update.message.reply_text(
            f"Использую запомненную карту: {saved_card}\n"
            f"Если хочешь её изменить — просто отправь новую карту в ответ на этот запрос.\n\n"
            f"Теперь отправь, пожалуйста, <id>:"
        )
        return ASK_ID
    else:
        await update.message.reply_text(
            "Принято. Теперь отправь, пожалуйста, <card> (payment_method_id):"
        )
        return ASK_CARD


async def ask_card(update: Update, context: ContextTypes.DEFAULT_TYPE):
    card = update.message.text.strip()
    context.user_data["card"] = card

    await update.message.reply_text(
        "Отлично. Теперь отправь, пожалуйста, <id>:"
    )
    return ASK_ID


async def ask_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _id = update.message.text.strip()
    context.user_data["id"] = _id

    await update.message.reply_text(
        "Все параметры сохранены ✅\n\n"
        "Теперь ты можешь:\n"
        "• Через «Заебашить» → «Одиночная смена» — один POST-запрос.\n"
        "• Через «Заебашить» → «Запустить потоки» — массовая отправка.\n"
        "• «Профиль» — статистика.\n"
        "• «Логи» — меню для выгрузки логов.\n"
        "• «Прокси/аккаунты» — работа с прокси и поездками.",
        reply_markup=main_keyboard(),
    )
    return MENU


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Заебашить":
        await update.message.reply_text(
            "Выбери действие:", reply_markup=actions_keyboard()
        )
        return MENU

    if text == "Одиночная смена":
        use_proxies = context.user_data.get("use_proxies", True)
        proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"
        await update.message.reply_text(
            "Окей, погнали. 🚀\n"
            f"Сейчас прокси: {proxy_state}\n\n"
            "Сначала отправь токен (только сам <token>, без Bearer):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_TOKEN

    if text == "Запустить потоки":
        await update.message.reply_text(
            "Выбери, как запускать потоки:", reply_markup=stream_start_markup()
        )
        return MENU

    if text == "Остановить потоки":
        stop_event: Optional[asyncio.Event] = context.user_data.get("stop_event")
        progress = context.user_data.get("active_session", {}).get("progress")
        if isinstance(stop_event, asyncio.Event) and not stop_event.is_set():
            stop_event.set()
            completed = progress.get("completed", 0) if isinstance(progress, dict) else 0
            success = progress.get("success", 0) if isinstance(progress, dict) else 0
            failed = max(completed - success, 0)
            await update.message.reply_text(
                "Окей, останавливаю потоки. ⛔ "
                f"Уже отправлено: {completed}. Успехов: {success}. Неуспехов: {failed}.",
                reply_markup=actions_keyboard(),
            )
        else:
            await update.message.reply_text(
                "Сейчас нет активной массовой отправки.",
                reply_markup=actions_keyboard(),
            )
        return MENU

    if text == "Назад":
        await update.message.reply_text("Возвращаюсь в меню.", reply_markup=main_keyboard())
        return MENU

    if text == "Профиль":
        return await show_profile(update, context)

    if text == "Логи":
        await update.message.reply_text("Что показать?", reply_markup=logs_keyboard())
        return MENU

    if text == "Посмотреть логи":
        await update.message.reply_text(
            "Введи ID сессии (5–7 цифр), лог которой хочешь получить:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_LOG_SESSION_ID

    if text == "Логи последней сессии":
        return await last_session_logs(update, context)

    if text == "Прокси/аккаунты":
        await update.message.reply_text(
            "Управляем прокси и поездками.", reply_markup=proxy_keyboard()
        )
        return MENU

    if text == "Прокси вкл/выкл":
        current = context.user_data.get("use_proxies", True)
        new_value = not current
        context.user_data["use_proxies"] = new_value
        state = "ВКЛ" if new_value and PROXIES else "ВЫКЛ (или список пуст)"
        await update.message.reply_text(
            f"Прокси теперь: {state}",
            reply_markup=proxy_keyboard(),
        )
        return MENU

    if text == "Перезагрузить прокси":
        load_proxies()
        use_proxies = context.user_data.get("use_proxies", True)
        state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"
        await update.message.reply_text(
            f"Прокси перечитаны. Текущее состояние: {state}",
            reply_markup=proxy_keyboard(),
        )
        return MENU

    if text == "Загрузить поездки":
        return await show_trip_loader(update, context)

    if text == "Изменить поездки":
        return await show_trip_manager(update, context)

    await update.message.reply_text(
        "Не понял команду. Используй кнопки на клавиатуре.",
        reply_markup=main_keyboard(),
    )
    return MENU


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None

    if tg_id is None:
        await update.message.reply_text(
            "Не смог получить твой TG ID 🤔",
            reply_markup=main_keyboard(),
        )
        return MENU

    total_requests = get_request_count_for_user(tg_id)
    saved_card = get_saved_card_for_user(tg_id)
    last_session_id = context.user_data.get("last_session_id")
    use_proxies = context.user_data.get("use_proxies", True)
    proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"

    if saved_card:
        msg = (
            f"👤 Профиль\n\n"
            f"TG ID: <code>{html.escape(str(tg_id))}</code>\n"
            f"Всего отправлено запросов: <b>{total_requests}</b>\n"
            f"Запомненная карта: <code>{html.escape(saved_card)}</code>\n"
        )
    else:
        msg = (
            f"👤 Профиль\n\n"
            f"TG ID: <code>{html.escape(str(tg_id))}</code>\n"
            f"Всего отправлено запросов: <b>{total_requests}</b>\n"
            f"Запомненная карта: не сохранена\n"
        )

    msg += f"\nПрокси: {proxy_state}\n"

    if last_session_id:
        msg += f"\nПоследний ID сессии: <code>{html.escape(str(last_session_id))}</code>\n"

    msg += "\nКнопка «Логи последней сессии» сразу скинет .txt по последней сессии."

    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    return MENU


async def remember_card_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None

    if tg_id is None:
        await update.message.reply_text(
            "Не смог получить твой TG ID 🤔 Попробуй ещё раз.",
            reply_markup=main_keyboard(),
        )
        return MENU

    card = update.message.text.strip()
    save_card_for_user(tg_id, card)
    context.user_data["card"] = card

    await update.message.reply_text(
        f"Карта <code>{html.escape(card)}</code> сохранена ✅\n"
        f"Теперь она будет автоматически подставляться в запросы.\n"
        f"Если захочешь её поменять — отправь другую карту, и я её обновлю.",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )
    return MENU


async def ask_threads_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        threads = int(text)
        if threads <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно целое положительное число потоков."
            " Можешь снова ввести число или нажать любую кнопку меню.",
            reply_markup=main_keyboard(),
        )
        return MENU

    context.user_data["threads"] = threads
    await update.message.reply_text(
        "Ок. Сколько всего запросов нужно отправить?",
    )
    return ASK_TOTAL_REQUESTS


async def ask_total_requests_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        total_requests = int(text)
        if total_requests <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно целое положительное число запросов."
            " Можешь снова ввести число или нажать любую кнопку меню.",
            reply_markup=main_keyboard(),
        )
        return MENU

    threads = context.user_data.get("threads")
    if not threads:
        await update.message.reply_text(
            "Что-то пошло не так с количеством потоков. Начни заново.",
            reply_markup=main_keyboard(),
        )
        return MENU

    await bulk_change_payment(update, context, threads, total_requests)
    return MENU


async def ask_log_session_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None
    session_id = update.message.text.strip()

    if tg_id is None:
        await update.message.reply_text(
            "Не смог получить твой TG ID 🤔",
            reply_markup=main_keyboard(),
        )
        return MENU

    if not (session_id.isdigit() and 5 <= len(session_id) <= 7):
        await update.message.reply_text(
            "ID сессии должен быть из 5–7 цифр. Попробуй ещё раз или нажми любую кнопку.",
            reply_markup=main_keyboard(),
        )
        return MENU

    path = export_session_logs_to_file(tg_id, session_id)
    if path is None:
        await update.message.reply_text(
            f"Логи для сессии {session_id} не найдены.",
            reply_markup=main_keyboard(),
        )
        return MENU

    try:
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=f"logs_{session_id}.txt"),
                caption=f"Логи для сессии {session_id}",
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    await update.message.reply_text(
        "Готово ✅",
        reply_markup=main_keyboard(),
    )
    return MENU


async def last_session_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    tg_id = user.id if user else None
    if tg_id is None:
        await update.message.reply_text(
            "Не смог получить твой TG ID 🤔",
            reply_markup=main_keyboard(),
        )
        return MENU

    session_id = context.user_data.get("last_session_id")
    if not session_id:
        await update.message.reply_text(
            "У тебя пока нет последней сессии (ещё не отправлял запросы).",
            reply_markup=main_keyboard(),
        )
        return MENU

    path = export_session_logs_to_file(tg_id, session_id)
    if path is None:
        await update.message.reply_text(
            f"Логи для последней сессии {session_id} не найдены.",
            reply_markup=main_keyboard(),
        )
        return MENU

    try:
        with open(path, "rb") as f:
            await update.message.reply_document(
                document=InputFile(f, filename=f"logs_{session_id}.txt"),
                caption=f"Логи для последней сессии {session_id}",
            )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    await update.message.reply_text(
        "Готово ✅",
        reply_markup=main_keyboard(),
    )
    return MENU


async def change_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Один запрос (отдельная сессия).
    """
    user = update.effective_user
    tg_id = user.id if user else 0

    user_token = context.user_data.get("token")
    orderid = context.user_data.get("orderid")

    saved_card = get_saved_card_for_user(tg_id)
    if saved_card:
        card = saved_card
        context.user_data["card"] = card
    else:
        card = context.user_data.get("card")

    _id = context.user_data.get("id")

    if not all([user_token, orderid, card, _id]):
        await update.message.reply_text(
            "Похоже, какие-то параметры не заданы. Нажми «Заебашить» и введи данные заново.",
            reply_markup=main_keyboard(),
        )
        return MENU

    use_proxies = context.user_data.get("use_proxies", True)

    session_id = generate_session_id()
    context.user_data["last_session_id"] = session_id

    proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"

    await update.message.reply_text(
        f"Отправляю запрос... ⏳\n"
        f"ID сессии: <code>{session_id}</code>\n"
        f"Прокси: {proxy_state}",
        parse_mode="HTML",
    )

    headers = build_headers(user_token)
    payload = build_payload(orderid, card, _id)

    ok, status_code, response_text = await do_single_request_and_log(
        tg_id, headers, payload, session_id, use_proxies
    )

    if response_text is None:
        response_text = ""

    max_len = 1500
    sliced_response = response_text[:max_len] + (
        "\n\n[ответ обрезан]" if len(response_text) > max_len else ""
    )
    body_text = html.escape(sliced_response)

    if ok:
        msg = (
            f"✅ Запрос отправлен.\n"
            f"ID сессии: <code>{session_id}</code>\n"
            f"Прокси: {proxy_state}\n\n"
            f"Статус: {status_code}\n"
            f"Тело ответа:\n<pre>{body_text}</pre>"
        )
    else:
        msg = (
            f"❌ Не удалось отправить запрос.\n"
            f"ID сессии: <code>{session_id}</code>\n"
            f"Прокси: {proxy_state}\n"
            f"Статус: {status_code}\n"
            f"Подробности:\n<pre>{body_text}</pre>"
        )

    await update.message.reply_text(
        msg, parse_mode="HTML", reply_markup=main_keyboard()
    )
    return MENU


async def bulk_change_payment(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    threads: int,
    total_requests: int,
):
    """
    Массовая отправка: threads — одновременные запросы,
    total_requests — сколько всего логических запросов сделать.
    Добавлена честная задержка 300 мс между запросами, бэкофф на 429/5xx и
    корректная остановка.
    """
    user = update.effective_user
    tg_id = user.id if user else 0
    chat_id = update.effective_chat.id

    active_stop: Optional[asyncio.Event] = context.user_data.get("stop_event")
    if isinstance(active_stop, asyncio.Event) and not active_stop.is_set():
        await update.message.reply_text(
            "У тебя уже идёт массовая отправка. Дождись окончания или нажми"
            " «Остановить потоки».",
            reply_markup=main_keyboard(),
        )
        return

    user_token = context.user_data.get("token")
    orderid = context.user_data.get("orderid")

    saved_card = get_saved_card_for_user(tg_id)
    if saved_card:
        card = saved_card
        context.user_data["card"] = card
    else:
        card = context.user_data.get("card")

    _id = context.user_data.get("id")

    if not all([user_token, orderid, card, _id]):
        await update.message.reply_text(
            "Параметры не заданы полностью. Нажми «Заебашить» и введи данные.",
            reply_markup=main_keyboard(),
        )
        return

    use_proxies = context.user_data.get("use_proxies", True)
    proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"

    headers = build_headers(user_token)
    payload = build_payload(orderid, card, _id)

    session_id = generate_session_id()
    context.user_data["last_session_id"] = session_id

    await update.message.reply_text(
        f"Запускаю массовую отправку.\n"
        f"ID сессии: <code>{session_id}</code>\n"
        f"Потоки (одновременных запросов): {threads}\n"
        f"Всего логических запросов: {total_requests}\n"
        f"Прокси: {proxy_state}\n\n"
        f"Каждые 5 секунд буду присылать лог (headers, body, последний ответ).\n"
        f"Чтобы остановить — нажми «Остановить потоки».",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    progress = {
        "completed": 0,
        "success": 0,
        "last_status": None,
        "last_response": "",
    }

    stop_event = asyncio.Event()
    context.user_data["stop_event"] = stop_event
    context.user_data["active_session"] = {
        "session_id": session_id,
        "progress": progress,
    }

    async def progress_cb(
        completed: int, success: int, status: int, response: Optional[str]
    ):
        progress["completed"] = completed
        progress["success"] = success
        progress["last_status"] = status
        if response:
            max_len = 800
            sliced = response[:max_len] + (
                "\n\n[ответ обрезан]" if len(response) > max_len else ""
            )
            progress["last_response"] = html.escape(sliced)

    async def reporter():
        while not stop_event.is_set():
            await asyncio.sleep(5)
            if stop_event.is_set():
                break

            msg = (
                f"📊 Промежуточный лог\n"
                f"ID сессии: <code>{session_id}</code>\n"
                f"Выполнено логических запросов: {progress['completed']} из {total_requests}\n"
                f"Успешных: {progress['success']}\n"
                f"Последний статус: {progress['last_status']}\n"
                f"Прокси: {proxy_state}\n\n"
                f"<b>Headers</b>:\n<pre>{html.escape(json.dumps(headers, ensure_ascii=False, indent=2))}</pre>\n"
                f"<b>Body</b>:\n<pre>{html.escape(json.dumps(payload, ensure_ascii=False, indent=2))}</pre>\n"
                f"<b>Последний ответ</b>:\n<pre>{progress['last_response']}</pre>"
            )
            try:
                await context.bot.send_message(
                    chat_id=chat_id, text=msg, parse_mode="HTML"
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("Ошибка отправки репорта: %s", e)

    reporter_task = asyncio.create_task(reporter())

    completed, success = await session_service.run_bulk(
        tg_id=tg_id,
        headers=headers,
        payload=payload,
        use_proxies=use_proxies,
        total_requests=total_requests,
        concurrency=threads,
        session_id=session_id,
        progress_cb=progress_cb,
        stop_event=stop_event,
    )

    stop_event.set()
    context.user_data.pop("stop_event", None)
    context.user_data.pop("active_session", None)
    try:
        await reporter_task
    except Exception:
        pass

    failed = completed - success

    await update.message.reply_text(
        f"Массовая отправка завершена (или остановлена).\n"
        f"ID сессии: <code>{session_id}</code>\n"
        f"Прокси: {proxy_state}\n"
        f"Успешных логических запросов: {success}\n"
        f"Неуспешных: {failed}\n"
        f"Всего попыток: {completed} из запланированных {total_requests}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Диалог завершён. Чтобы начать сначала — отправь /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    return ConversationHandler.END



def main():
    init_db()
    load_proxies()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_token)],
            ASK_ORDERID: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_orderid)],
            ASK_CARD: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_card)],
            ASK_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, ask_id)],
            MENU: [
                CallbackQueryHandler(tripfield_callback, pattern="^tripfield:"),
                CallbackQueryHandler(trip_save_callback, pattern="^tripsave:"),
                CallbackQueryHandler(trip_clear_callback, pattern="^tripclear:"),
                CallbackQueryHandler(streams_option_callback, pattern="^streams:"),
                CallbackQueryHandler(trip_select_callback, pattern="^tripselect:"),
                CallbackQueryHandler(trip_manage_callback, pattern="^tripmanage:"),
                CallbackQueryHandler(trip_edit_callback, pattern="^tripedit:"),
                CallbackQueryHandler(trip_delete_callback, pattern="^tripdelete:"),
                CallbackQueryHandler(trip_use_callback, pattern="^tripuse:"),
                CallbackQueryHandler(start_choice_callback),
                MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler),
            ],
            REMEMBER_CARD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remember_card_handler)
            ],
            ASK_THREADS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_threads_handler)
            ],
            ASK_TOTAL_REQUESTS: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, ask_total_requests_handler
                )
            ],
            ASK_LOG_SESSION_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_log_session_handler)
            ],
            ASK_TRIP_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, trip_value_handler)
            ],
            ASK_STREAM_TOKEN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_token_handler)
            ],
            ASK_STREAM_ORDERID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_orderid_handler)
            ],
            ASK_STREAM_CARD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_card_handler)
            ],
            ASK_STREAM_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_id_handler)
            ],
            ASK_STREAM_THREADS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_threads_handler)
            ],
            ASK_STREAM_TOTAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, stream_total_handler)
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler("start", start),  # <--- добавили
        ],
    )

    app.add_handler(conv)

    app.run_polling()


if __name__ == "__main__":
    main()
