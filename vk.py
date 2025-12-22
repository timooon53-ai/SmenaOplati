import json
import logging
import os
import threading
import asyncio
import time
from typing import Iterable

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
    list_trip_templates,
    init_db,
    load_proxies,
    proxy_state_text,
    proxies_enabled,
    create_trip_template,
    update_trip_template_field,
    delete_trip_template,
    session_service,
    fetch_trip_details_from_token,
    fetch_session_details,
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
                        "action": {"type": "text", "label": "➕ Добавить поездку"},
                        "color": "primary",
                    }
                ],
                [
                    {
                        "action": {"type": "text", "label": "💳 Поменять оплату"},
                        "color": "secondary",
                    }
                ]
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
            last_sent = 0.0

            async def progress_cb(completed: int, success: int, status_code: int, _resp: str | None):
                nonlocal last_sent
                now = time.monotonic()
                if last_sent == 0 or now - last_sent >= 5 or completed == total_requests:
                    msg = (
                        f"Сессия {session_id}: {completed}/{total_requests} завершено. "
                        f"Успехов: {success}"
                    )
                    await asyncio.to_thread(self.send, user_id, msg)
                    last_sent = now

            completed, success = await session_service.run_bulk(
                user_id,
                headers,
                payload,
                proxies_enabled(),
                total_requests,
                threads,
                session_id,
                progress_cb=progress_cb,
            )
            return session_id, completed, success

        return asyncio.run(_job())

    def _format_trip(self, idx: int, trip: dict) -> str:
        name = trip.get("trip_name") or f"Поездка #{trip.get('id')}"
        notes = []
        if trip.get("token2"):
            notes.append("token2")
        if trip.get("session_id"):
            notes.append("session_id")
        if trip.get("trip_id"):
            notes.append(f"ID: {trip['trip_id']}")
        if trip.get("orderid"):
            notes.append(f"orderid: {trip['orderid']}")
        if trip.get("card"):
            notes.append(f"card-x: {trip['card']}")
        if trip.get("trip_link"):
            notes.append("ссылка есть")
        note_text = ", ".join(notes) if notes else "пусто"
        return f"{idx}. {name} (id={trip.get('id')}): {note_text}"

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
            "Напиши 'удалить N' — удалить."
        )
        self.update_state(user_id, step="trip_list", trips=trips)
        self.send(user_id, "\n".join(lines), self.start_keyboard())

    def _prepare_trip_creation(self, user_id: int):
        trip_id = create_trip_template(user_id)
        self.update_state(user_id, step="trip_orderid_new", active_trip=trip_id, data={})
        self.send(
            user_id,
            "Введи orderid (можно пропустить, отправив '-'). Затем пришли token2/session_id — карту и ID я подставлю сам.",
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
            if text and text != "-":
                self._fill_trip_field(user_id, "orderid", text)
            self.update_state(user_id, step="trip_token_new")
            self.send(user_id, "Пришли token2 или session_id (если session_id, token2 очищу).")
            return True

        if step == "trip_token_new":
            trip_id = state.get("active_trip")
            if "session" in text.lower():
                self._fill_trip_field(user_id, "session_id", text)
                update_trip_template_field(trip_id, user_id, "token2", None)
                autofill_note = self._autofill_trip_template(trip_id, user_id, text, is_session=True)
            else:
                self._fill_trip_field(user_id, "token2", text)
                autofill_note = self._autofill_trip_template(trip_id, user_id, text, is_session=False)
            summary_parts = ["Поездка сохранена."]
            if autofill_note:
                summary_parts.append(autofill_note)
            trip = list_trip_templates(user_id)[-1] if list_trip_templates(user_id) else None
            if trip:
                summary_parts.append(self._format_trip(1, trip))
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
                "Запускаю массовую отправку. Каждые 5 секунд присылаю прогресс и пишу в БД.",
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
