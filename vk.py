import json
import logging
from typing import Dict

import vk_api
from vk_api.longpoll import VkEventType, VkLongPoll

import cfg
from main import (
    get_request_count_for_user,
    get_user_token,
    init_db,
    is_user_verified,
    random_token,
    upsert_user_token,
    verify_user_by_token,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

VK_TOKEN = getattr(cfg, "VK_TOKEN", None)


class VkBot:
    def __init__(self, token: str):
        self.vk_session = vk_api.VkApi(token=token)
        self.longpoll = VkLongPoll(self.vk_session)
        self.vk = self.vk_session.get_api()
        self.pending_tokens: Dict[int, bool] = {}

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

    def handle_profile(self, user_id: int):
        total_requests = get_request_count_for_user(user_id)
        existing_token = get_user_token(user_id)
        token = existing_token or upsert_user_token(user_id, random_token(), True)

        text = (
            "👤 Профиль\n\n"
            f"VK ID: {user_id}\n"
            f"Всего отправлено запросов: {total_requests}\n"
            f"Токен: {token}\n\n"
            "Сохрани токен — его нужно вводить при первом запуске."
        )
        keyboard = {
            "one_time": False,
            "buttons": [
                [
                    {
                        "action": {"type": "text", "label": "Токен"},
                        "color": "positive",
                    }
                ]
            ],
        }
        self.send(user_id, text, keyboard)

    def process_token(self, user_id: int, text: str) -> bool:
        if verify_user_by_token(user_id, text.strip()):
            self.pending_tokens.pop(user_id, None)
            self.send(user_id, "Токен принят! Добро пожаловать.", self.start_keyboard())
            return True
        self.send(user_id, "Токен не найден. Попробуй снова.")
        self.pending_tokens[user_id] = True
        return False

    def handle_event(self, event):
        user_id = event.user_id
        text = (event.text or "").strip()

        if user_id in self.pending_tokens or not is_user_verified(user_id):
            self.pending_tokens[user_id] = True
            if text:
                self.process_token(user_id, text)
            else:
                self.send(
                    user_id,
                    "Отправь токен (10 символов из цифр и букв), чтобы продолжить.",
                )
            return

        lowered = text.lower()
        if lowered in {"/start", "start", "начать"}:
            self.send(
                user_id,
                "Привет! Я VK-версия бота changepayment. Используй кнопки ниже.",
                self.start_keyboard(),
            )
            return

        if text == "👤 Профиль" or lowered == "профиль":
            self.handle_profile(user_id)
            return

        if text == "Токен":
            new_token = random_token()
            upsert_user_token(user_id, new_token, True)
            self.send(
                user_id,
                f"Сгенерировал новый токен: {new_token}\n"
                "Сохрани его для последующих запусков.",
                self.start_keyboard(),
            )
            return

        self.send(
            user_id,
            "Пока что весь функционал доступен в Телеграм-боте. "
            "Здесь доступны профайл и работа с токеном.",
            self.start_keyboard(),
        )

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
    if not VK_TOKEN:
        raise RuntimeError("В конфиге не задан VK_TOKEN")
    bot = VkBot(VK_TOKEN)
    bot.run()


if __name__ == "__main__":
    main()
