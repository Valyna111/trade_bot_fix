#!/usr/bin/env python3
# ============================================================================
#  WS-РЕЖИМ ОБМЕНА — реакция на обмен по WebSocket-пушу вместо HTTP-опроса.
# ============================================================================
#  Держит socket.io-соединение на аккаунт, слушает пуш `new-sendNewTrade`
#  и на событие пишет триггер-файл для основного бота.
# ============================================================================

import os
import sys
import json
import time
import threading
import socketio
from pathlib import Path
from typing import Optional, Dict, Callable

# ============================================================================
#  КОНФИГУРАЦИЯ
# ============================================================================

WSS_HOST = os.getenv("WSS_HOST", "wss10.mangabuff.ru")
SYNC_SEC = max(15, int(os.getenv("WS_SYNC_SEC", 60)))
WS_TRIGGER_FILE = Path(__file__).parent / "ws_trigger.json"


# ============================================================================
#  WS КЛИЕНТ
# ============================================================================

class WSClient:
    def __init__(self, chat_id: int = None, user_id: str = None, cookies: dict = None):
        self.chat_id = chat_id
        self.user_id = user_id
        self.cookies = cookies
        self.sio = None
        self.connected = False
        self.running = False
        self.reconnect_timer = None
        self._on_trade = None
        self._setup_socket()

    def _setup_socket(self):
        self.sio = socketio.Client(reconnection=False, logger=False, engineio_logger=False)

        @self.sio.event
        def connect():
            self.connected = True
            print(f"[WS] Подключён к {WSS_HOST}")
            if self.user_id:
                self.sio.emit('joinRoom', {'room': '/', 'userId': int(self.user_id)})
                print(f"[WS] Подписан на обмены (userId={self.user_id})")

        @self.sio.event
        def connect_error(data):
            self.connected = False
            print(f"[WS] Ошибка подключения: {data}")

        @self.sio.event
        def disconnect():
            self.connected = False
            print("[WS] Отключён")
            if self.running:
                self._schedule_reconnect()

        @self.sio.event
        def new_sendNewTrade(data):
            print(f"[{time.strftime('%H:%M:%S')}] 🔔 new-sendNewTrade → триггер")
            if self.chat_id:
                trigger = {
                    'type': 'new_trade',
                    'timestamp': int(time.time() * 1000),
                    'chat_id': self.chat_id
                }
                try:
                    WS_TRIGGER_FILE.write_text(json.dumps(trigger))
                    print("[WS] Триггер записан")
                except Exception as e:
                    print(f"[WS] Ошибка записи: {e}")

    def connect(self, chat_id: int = None, user_id: str = None, cookies: dict = None) -> bool:
        self.running = True
        if chat_id:
            self.chat_id = chat_id
        if user_id:
            self.user_id = user_id
        if cookies:
            self.cookies = cookies

        if not self.user_id:
            print("[WS] Нет user_id")
            return False

        if self.connected:
            return True

        try:
            cookie_str = '; '.join([f'{k}={v}' for k, v in (self.cookies or {}).items()])
            headers = {
                'Cookie': cookie_str,
                'Origin': 'https://mangabuff.ru',
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            url = f"wss://{WSS_HOST}/socket.io/?EIO=4&transport=websocket"
            self.sio.connect(url, headers=headers, transports=['websocket'])
            return True
        except Exception as e:
            print(f"[WS] Ошибка: {e}")
            self._schedule_reconnect()
            return False

    def _schedule_reconnect(self, delay: int = 5):
        if self.reconnect_timer:
            return

        def reconnect():
            self.reconnect_timer = None
            if self.running and not self.connected:
                try:
                    self.sio.connect(self.sio.connection_url)
                except Exception as e:
                    print(f"[WS] Переподключение не удалось: {e}")
                    self._schedule_reconnect(delay + 5)

        self.reconnect_timer = threading.Timer(delay, reconnect)
        self.reconnect_timer.daemon = True
        self.reconnect_timer.start()

    def disconnect(self):
        self.running = False
        if self.reconnect_timer:
            self.reconnect_timer.cancel()
            self.reconnect_timer = None
        if self.connected:
            try:
                self.sio.disconnect()
            except:
                pass
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected


# ============================================================================
#  WS-МЕНЕДЖЕР
# ============================================================================

class WSManager:
    def __init__(self):
        self.clients: Dict[int, WSClient] = {}
        self.active_chat_id: Optional[int] = None
        self.running = False
        self.thread = None

    def start(self, chat_id: int, user_id: str, cookies: dict) -> bool:
        if self.running and self.active_chat_id == chat_id:
            return True

        self.stop()

        self.active_chat_id = chat_id
        self.running = True
        self.thread = threading.Thread(
            target=self._worker,
            args=(chat_id, user_id, cookies),
            daemon=True
        )
        self.thread.start()
        return True

    def stop(self):
        self.running = False
        if self.active_chat_id in self.clients:
            self.clients[self.active_chat_id].disconnect()
            del self.clients[self.active_chat_id]
        self.active_chat_id = None

    def _worker(self, chat_id: int, user_id: str, cookies: dict):
        client = WSClient(chat_id, user_id, cookies)
        self.clients[chat_id] = client
        client.connect()

        while self.running and chat_id == self.active_chat_id:
            time.sleep(1)

        client.disconnect()
        if chat_id in self.clients:
            del self.clients[chat_id]

    def is_active(self) -> bool:
        return self.running and self.active_chat_id is not None


# ============================================================================
#  ГЛОБАЛЬНЫЙ МЕНЕДЖЕР И ФУНКЦИИ ДЛЯ ИМПОРТА
# ============================================================================

_ws_manager = WSManager()


def start_ws(chat_id: int, user_id: str, cookies: dict) -> bool:
    """
    Запускает WebSocket для указанного чата.
    
    Args:
        chat_id: ID чата в Telegram
        user_id: ID пользователя на mangabuff.ru
        cookies: Cookies для авторизации
    
    Returns:
        bool: True если запущен успешно
    """
    return _ws_manager.start(chat_id, user_id, cookies)


def stop_ws() -> None:
    """Останавливает WebSocket"""
    _ws_manager.stop()


def ws_is_active() -> bool:
    """Проверяет, активен ли WebSocket"""
    return _ws_manager.is_active()


# ============================================================================
#  ЗАПУСК В РЕЖИМЕ ОЖИДАНИЯ (для отладки, не используется в основном боте)
# ============================================================================

if __name__ == '__main__':
    """
    Этот блок выполняется ТОЛЬКО при прямом запуске wsexchange.py.
    В основном боте (trade_bot.py) он НЕ выполняется, потому что
    trade_bot.py импортирует функции, а не запускает файл как скрипт.
    """
    print("[WS] Запуск WebSocket-менеджера в режиме ожидания...")
    print(f"[WS] Хост: {WSS_HOST}")
    print("[WS] Ожидание команд... (Ctrl+C для остановки)")
    
    try:
        # Бесконечное ожидание с проверкой триггеров
        while True:
            # Проверяем, есть ли файл-триггер
            if WS_TRIGGER_FILE.exists():
                try:
                    data = json.loads(WS_TRIGGER_FILE.read_text(encoding="utf-8"))
                    print(f"[WS] Обнаружен триггер: {data}")
                except Exception as e:
                    print(f"[WS] Ошибка чтения триггера: {e}")
            
            time.sleep(10)
    except KeyboardInterrupt:
        print("\n[WS] Завершение...")
        sys.exit(0)