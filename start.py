import threading

from main import main as start_telegram
from vk import main as start_vk


def run_bots():
    tg_thread = threading.Thread(target=start_telegram, daemon=True)
    tg_thread.start()
    start_vk()


if __name__ == "__main__":
    run_bots()
