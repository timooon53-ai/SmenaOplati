import json
import logging
import os
import threading
import asyncio

import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll

os.environ.setdefault("BOT_DB_PATH", "VD.db")
from cfg import VK_TOKEN
from main import (
    build_headers,
    build_payload,
    do_single_request_and_log,
    generate_session_id,
    get_request_count_for_user,
    init_db,
    load_proxies,
    proxy_state_text,
    proxies_enabled,
    session_service,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

VK_TOKEN = VK_TOKEN


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
                        "action": {"type": "text", "label": "💳 Поменять оплату"},
                        "color": "primary",
                    },
                    {
                        "action": {"type": "text", "label": "👤 Профиль"},
                        "color": "secondary",
                    },
                ],
                [
                    {
                        "action": {"type": "text", "label": "🚂 Загрузить поездки"},
                        "color": "primary",
                    },
                    {
                        "action": {"type": "text", "label": "📜 Логи"},
                        "color": "secondary",
                    },
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
        if lowered in {"🎯 одиночная смена", "одиночная смена"}:
            self.update_state(user_id, flow="single", step="token", data={})
            self.send(
                user_id,
                "Пришли token2 или session_id. Если укажешь session_id — token2 очищу.",
            )
            return True

        if lowered in {"🚀 запустить потоки", "запустить потоки"}:
            self.update_state(user_id, flow="bulk", step="token", data={})
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

    def start_data_collection(self, user_id: int):
        self.update_state(user_id, step="choose_mode", data={}, timer=None)
        self.send(
            user_id,
            "Выбирай режим: одиночная смена или потоки.",
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

    def handle_stateful_input(self, user_id: int, text: str) -> bool:
        state = self.state.get(user_id) or {}
        step = state.get("step")
        data = state.get("data", {})

        if step == "choose_mode":
            return self.handle_change_payment_mode(user_id, text)

        if step == "token":
            if text:
                if "session" in text.lower() or text.isdigit():
                    data["session_cookie"] = text
                    data.pop("token", None)
                else:
                    data["token"] = text
                    data.pop("session_cookie", None)
                self.update_state(user_id, data=data)
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

        required = [data.get("orderid"), data.get("card"), data.get("id")]
        if not any([data.get("token"), data.get("session_cookie")]) or not all(required):
            self.send(user_id, "Не все данные заданы. Нажми «💳 Поменять оплату» и попробуй снова.")
            return

        if flow == "bulk":
            threads = int(data.get("threads", 1))
            total = int(data.get("total", 0))
            if total <= 0:
                self.send(user_id, "Укажи количество запросов для потоков.")
                return

            self.send(
                user_id,
                "Запускаю массовую отправку. Каждые 5 секунд идёт логирование в БД.",
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

        if text == "👤 Профиль" or lowered == "профиль":
            self.handle_profile(user_id)
            return

        if text == "💳 Поменять оплату":
            self.start_data_collection(user_id)
            return

        if text == "📜 Логи":
            self.send(
                user_id,
                "Логи можно выгрузить по ID сессии через Телеграм-бота."
                " В VK логирование продолжается автоматически.",
                self.start_keyboard(),
            )
            return

        if text == "🚂 Загрузить поездки":
            self.send(
                user_id,
                "Менеджер поездок пока доступен только в Телеграм-боте. "
                "Смена оплаты и логирование работают здесь полностью.",
                self.start_keyboard(),
            )
            return

        self.send(user_id, "Не понял команду, используй кнопки ниже.", self.start_keyboard())

    def run(self):
        logger.info("Запускаю VK-бота")
        for event in self.longpoll.listen():
            if event.type == VkEventType.MESSAGE_NEW and event.to_me:
                try:
                    self.handle_event(event)
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Ошибка в обработке события VK: %s", exc)


def main():
    init_db()
    load_proxies()
    if not VK_TOKEN:
        raise RuntimeError("В конфиге не задан VK_TOKEN")
    bot = VkBot(VK_TOKEN)
    bot.run()


if __name__ == "__main__":
    main()
