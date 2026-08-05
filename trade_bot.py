#!/usr/bin/env python3
"""
Telegram бот для автоматического принятия выгодных обменов на mangabuff.ru
Принимает предложения, где:
Вы отдаёте 1 карту, а получаете 2 и более (2:1, 3:1, 4:1, ...)

Поддерживает:
- HTTP-опрос (каждые 15 сек) — резервный канал
- WebSocket (мгновенные уведомления) — основной канал
- Лимитер: 24 обмена в минуту, потом 1 минута отдыха
- Детект капчи с уведомлением в Telegram
"""

import os
import sys
import json
import re
import time
import threading
import html
import random
from pathlib import Path
from urllib.parse import unquote
from collections import deque
from datetime import datetime, timedelta

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("❌ Установите beautifulsoup4: pip install beautifulsoup4")
    sys.exit(1)

try:
    from curl_cffi.requests import Session as CffiSession
    USE_CURL_CFFI = True
except ImportError:
    import requests
    USE_CURL_CFFI = False
    print("[WARN] curl_cffi не установлен, используется requests.")

try:
    import telebot
    from telebot import types
except ImportError:
    print("❌ Установите pyTelegramBotAPI: pip install pyTelegramBotAPI")
    sys.exit(1)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("❌ Установите python-dotenv: pip install python-dotenv")
    sys.exit(1)

# Импортируем модули капчи и WebSocket
from captcha import create_captcha_handler
from wsexchange import start_ws, stop_ws, ws_is_active


# ==================== ЛИМИТЕР ОБМЕНОВ ====================
class TradeLimiter:
    MAX_TRADES_PER_MINUTE = 24
    REST_MINUTES = 1

    def __init__(self):
        self.trades_timestamps = deque()
        self.rest_until = None
        self.is_resting = False
        self.lock = threading.Lock()

    def can_accept(self) -> bool:
        with self.lock:
            now = datetime.now()
            if self.is_resting:
                if self.rest_until and now >= self.rest_until:
                    self.is_resting = False
                    self.trades_timestamps.clear()
                    self.rest_until = None
                    print("[LIMITER] Отдых закончился, счётчик сброшен")
                else:
                    return False

            one_minute_ago = now - timedelta(minutes=1)
            while self.trades_timestamps and self.trades_timestamps[0] < one_minute_ago:
                self.trades_timestamps.popleft()

            if len(self.trades_timestamps) >= self.MAX_TRADES_PER_MINUTE:
                self.is_resting = True
                self.rest_until = now + timedelta(minutes=self.REST_MINUTES)
                print(f"[LIMITER] Лимит {self.MAX_TRADES_PER_MINUTE} обменов/мин. Отдых до {self.rest_until.strftime('%H:%M:%S')}")
                return False
            return True

    def record_accept(self):
        with self.lock:
            self.trades_timestamps.append(datetime.now())

    def get_stats(self) -> dict:
        with self.lock:
            now = datetime.now()
            one_minute_ago = now - timedelta(minutes=1)
            while self.trades_timestamps and self.trades_timestamps[0] < one_minute_ago:
                self.trades_timestamps.popleft()
            count = len(self.trades_timestamps)
            remaining = self.MAX_TRADES_PER_MINUTE - count

            if self.is_resting:
                remaining_time = 0
                if self.rest_until:
                    remaining_time = int((self.rest_until - now).total_seconds())
                return {
                    'count': count,
                    'remaining': 0,
                    'limit': self.MAX_TRADES_PER_MINUTE,
                    'is_resting': True,
                    'rest_seconds': remaining_time,
                    'status': f"⏰ ОТДЫХ {remaining_time}с"
                }
            return {
                'count': count,
                'remaining': remaining,
                'limit': self.MAX_TRADES_PER_MINUTE,
                'is_resting': False,
                'rest_seconds': 0,
                'status': f"✅ {count}/{self.MAX_TRADES_PER_MINUTE} (осталось {remaining})"
            }


# ==================== КЛАСС АВТОРИЗАЦИИ ====================
class MangaBuffAuth:
    BASE_URL = "https://mangabuff.ru"

    def __init__(self, proxy: dict = None, impersonate: str = "chrome131"):
        self.impersonate = impersonate
        self.user_id = None
        self._setup_session(proxy)

    def _setup_session(self, proxy):
        if USE_CURL_CFFI:
            self.session = CffiSession(impersonate=self.impersonate)
        else:
            self.session = requests.Session()
        if proxy:
            self.session.proxies.update(proxy)

        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.6778.109 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
            'Sec-Ch-Ua': '"Google Chrome";v="131", "Not_A Brand";v="8"',
            'Sec-Ch-Ua-Mobile': '?0',
            'Sec-Ch-Ua-Platform': '"Windows"',
        })

    def _get_csrf_from_cookies(self) -> str:
        xsrf = self.session.cookies.get('XSRF-TOKEN')
        if xsrf:
            return unquote(xsrf)
        for cookie in self.session.cookies:
            name = cookie.name if hasattr(cookie, 'name') else cookie
            if name.upper() == 'XSRF-TOKEN':
                value = cookie.value if hasattr(cookie, 'value') else self.session.cookies[name]
                return unquote(value)
        return ''

    def login(self, email: str, password: str):
        resp = self.session.get(f'{self.BASE_URL}/login')
        if resp.status_code != 200:
            return False, f'GET login failed: HTTP {resp.status_code}'

        csrf = self._get_csrf_from_cookies()
        if not csrf:
            return False, 'CSRF token not found'

        time.sleep(1)

        login_data = {'email': email, 'password': password, 'remember': 'on'}
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
            'X-XSRF-TOKEN': csrf,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f'{self.BASE_URL}/login',
            'Origin': self.BASE_URL,
        }
        resp = self.session.post(f'{self.BASE_URL}/login', data=login_data, headers=headers, allow_redirects=False)

        check = self.session.get(f'{self.BASE_URL}/')
        if check.status_code != 200:
            return False, 'Auth check failed'

        html_text = check.text
        match = re.search(r'data-userid="(\d+)"', html_text)
        if not match:
            match = re.search(r'/users/(\d+)', html_text)
        if match:
            self.user_id = match.group(1)
            cookies = []
            for name, value in self.session.cookies.items():
                cookies.append({'name': name, 'value': value, 'domain': 'mangabuff.ru'})
            return True, {'user_id': self.user_id, 'cookies': cookies}
        else:
            return False, 'User ID not found after login'

    def load_cookies(self, cookies_list: list):
        for c in cookies_list:
            name = c.get('name')
            value = c.get('value')
            domain = c.get('domain', 'mangabuff.ru')
            if name and value:
                self.session.cookies.set(name, value, domain=domain)
        self.user_id = self.get_user_id()

    def is_authenticated(self) -> bool:
        try:
            resp = self.session.get(f'{self.BASE_URL}/')
            if resp.status_code != 200:
                return False
            html_text = resp.text
            if re.search(r'data-userid="\d+"', html_text):
                return True
            if 'header__user' in html_text or '/logout' in html_text:
                return True
            return False
        except:
            return False

    def get_user_id(self) -> str:
        resp = self.session.get(f'{self.BASE_URL}/')
        if resp.status_code != 200:
            return None
        match = re.search(r'data-userid="(\d+)"', resp.text)
        if not match:
            match = re.search(r'/users/(\d+)', resp.text)
        return match.group(1) if match else None

    def get_cookies_dict(self) -> dict:
        cookies = {}
        for name, value in self.session.cookies.items():
            cookies[name] = value
        return cookies


# ==================== ФУНКЦИИ ПАРСИНГА ОБМЕНОВ ====================
def get_trades(auth: MangaBuffAuth):
    url = f"{auth.BASE_URL}/trades"
    response = auth.session.get(url)
    if response.status_code != 200:
        return []
    soup = BeautifulSoup(response.text, 'html.parser')
    trades = []
    trade_items = soup.find_all('a', class_=lambda c: c and 'trade__list-item' in c.split())
    for item in trade_items:
        href = item.get('href')
        if not href or '/trades/' not in href:
            continue
        trade_id = href.split('/')[-1]
        trade_url = f"{auth.BASE_URL}{href}"
        info_div = item.find('div', class_='trade__list-info')
        if not info_div:
            continue
        date_elem = info_div.find('div', class_='trade__list-date')
        date = date_elem.text.strip() if date_elem else ""
        name_elem = info_div.find('div', class_='trade__list-name')
        sender_name = name_elem.text.replace('от ', '').strip() if name_elem else ""
        header_div = info_div.find('div', class_='trade__list-header')
        is_new = bool(header_div and header_div.find('span', class_='trade__list-dot--new'))
        trades.append({
            'trade_id': trade_id,
            'sender_name': sender_name,
            'date': date,
            'is_new': is_new,
            'url': trade_url
        })
    return trades

def get_trade_details(auth: MangaBuffAuth, trade_id: str):
    url = f"{auth.BASE_URL}/trades/{trade_id}"
    response = auth.session.get(url)
    if response.status_code != 200:
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    sender_elem = soup.find('a', class_='trade__header-name')
    if not sender_elem:
        return None
    sender_name = sender_elem.text.strip()
    sender_id = sender_elem.get('href', '').split('/')[-1]
    viewed_elem = soup.find('span', class_='trade__viewed--yes')
    viewed = bool(viewed_elem)

    offered_cards = []
    creator_div = soup.find('div', class_='trade__main-items trade__main-items--creator')
    if creator_div:
        card_links = creator_div.find_all('a', class_='trade__main-item')
        for link in card_links:
            card_url = f"{auth.BASE_URL}{link.get('href')}"
            card_id = card_url.split('/')[-2] if '/cards/' in card_url else ''
            img = link.find('img')
            img_url = img.get('src') if img else ''
            offered_cards.append({'card_id': card_id, 'url': card_url, 'image': img_url})

    required_cards = []
    receiver_div = soup.find('div', class_='trade__main-items trade__main-items--receiver')
    if receiver_div:
        card_links = receiver_div.find_all('a', class_='trade__main-item')
        for link in card_links:
            card_url = f"{auth.BASE_URL}{link.get('href')}"
            card_id = card_url.split('/')[-2] if '/cards/' in card_url else ''
            img = link.find('img')
            img_url = img.get('src') if img else ''
            required_cards.append({'card_id': card_id, 'url': card_url, 'image': img_url})

    return {
        'trade_id': trade_id,
        'sender_id': sender_id,
        'sender_name': sender_name,
        'offered_cards': offered_cards,
        'required_cards': required_cards,
        'viewed': viewed,
        'url': f"{auth.BASE_URL}/trades/{trade_id}"
    }

def accept_trade(auth: MangaBuffAuth, trade_id: str, max_retries: int = 2):
    for attempt in range(max_retries):
        if attempt > 0:
            print(f"[RETRY] Попытка {attempt + 1}/{max_retries} для обмена {trade_id}")
            time.sleep(5)
        
        csrf = auth._get_csrf_from_cookies()
        if not csrf:
            if attempt == max_retries - 1:
                return False, "CSRF token not found"
            continue
        
        headers = {
            'X-XSRF-TOKEN': csrf,
            'X-Requested-With': 'XMLHttpRequest',
            'Referer': f"{auth.BASE_URL}/trades/{trade_id}",
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
        }
        
        endpoints = [
            f"{auth.BASE_URL}/trades/accept",
            f"{auth.BASE_URL}/trades/accept/{trade_id}",
            f"{auth.BASE_URL}/trades/{trade_id}/accept",
        ]
        
        for endpoint in endpoints:
            try:
                resp = auth.session.post(endpoint, headers=headers, data={'trade_id': trade_id})
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                        if data.get('error'):
                            continue
                    except:
                        pass
                    return True, "Обмен успешно принят!"
            except Exception as e:
                continue
        
        if attempt == max_retries - 1:
            return False, f"Не удалось принять обмен после {max_retries} попыток"
    
    return False, "Не удалось принять обмен"


# ==================== НАСТРОЙКИ БОТА ====================
BOT_TOKEN = os.getenv("TRADE_BOT_TOKEN") or os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("❌ Не найден TRADE_BOT_TOKEN или BOT_TOKEN в .env файле")
    sys.exit(1)

CHECK_INTERVAL = 15
SESSIONS_FILE = Path(__file__).parent / "tg_sessions.json"
PROCESSED_TRADES_FILE = Path(__file__).parent / "processed_trades.json"
WS_TRIGGER_FILE = Path(__file__).parent / "ws_trigger.json"

sessions = {}
processed_trades = set()
monitoring_active = False
monitoring_thread = None

limiter = TradeLimiter()

def load_sessions():
    global sessions
    if SESSIONS_FILE.exists():
        try:
            sessions = json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
        except:
            sessions = {}

def save_sessions():
    SESSIONS_FILE.write_text(json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

def load_processed_trades():
    global processed_trades
    if PROCESSED_TRADES_FILE.exists():
        try:
            data = json.loads(PROCESSED_TRADES_FILE.read_text(encoding="utf-8"))
            processed_trades = set(data.get("trades", []))
        except:
            processed_trades = set()

def save_processed_trades():
    data = {"trades": list(processed_trades)}
    PROCESSED_TRADES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

load_sessions()
load_processed_trades()

bot = telebot.TeleBot(BOT_TOKEN)

def get_auth_for_user(chat_id: int) -> MangaBuffAuth:
    auth = MangaBuffAuth()
    if str(chat_id) in sessions:
        cookies = sessions[str(chat_id)].get('cookies', [])
        if cookies:
            auth.load_cookies(cookies)
    return auth

def save_user_session(chat_id: int, user_id: str, cookies: list):
    sessions[str(chat_id)] = {'user_id': user_id, 'cookies': cookies}
    save_sessions()

def clear_user_session(chat_id: int):
    if str(chat_id) in sessions:
        del sessions[str(chat_id)]
        save_sessions()

def get_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=1)
    markup.add(
        types.KeyboardButton("🔁 Мониторинг обменов"),
        types.KeyboardButton("📊 Статус"),
    )
    return markup


# ==================== СОЗДАНИЕ ХЕНДЛЕРА КАПЧИ ====================
def get_account_by_tg(tg_id: int):
    """Получает аккаунт по tg_id из сессий"""
    if str(tg_id) in sessions:
        return sessions[str(tg_id)]
    return None

captcha_handler = create_captcha_handler(
    db={
        'get_account_by_tg': get_account_by_tg,
        'update_account': lambda acc_id, patch: None
    },
    notify=lambda tg_id, text, markup: bot.send_message(tg_id, text, reply_markup=markup),
    tag=lambda a: f"[{a.get('user_id', '')}] ",
    pause_ms=15 * 60 * 1000
)


# ==================== ОБРАБОТЧИК КНОПКИ КАПЧИ ====================
@bot.callback_query_handler(func=lambda call: call.data.startswith('cap_ok:'))
def handle_captcha_ok(call):
    try:
        acc_id = int(call.data.split(':')[1])
        captcha_handler.resume_from_captcha(acc_id)
        bot.answer_callback_query(call.id, "✅ Пауза снята, бот продолжит")
        bot.send_message(call.message.chat.id, "✅ Пауза снята. Нажмите /monitor_start для продолжения работы.")
    except Exception as e:
        bot.answer_callback_query(call.id, f"❌ Ошибка: {e}")


# ==================== МОНИТОРИНГ ====================
def check_ws_trigger():
    """Проверяет файл-триггер от WebSocket"""
    if WS_TRIGGER_FILE.exists():
        try:
            data = json.loads(WS_TRIGGER_FILE.read_text(encoding="utf-8"))
            WS_TRIGGER_FILE.unlink()
            if data.get('type') == 'new_trade':
                chat_id = data.get('chat_id')
                print(f"[WS-TRIGGER] Новый обмен для chat_id {chat_id}")
                if monitoring_active:
                    # Быстрая проверка на фоне основного цикла
                    threading.Thread(target=check_trades_now, args=(chat_id,), daemon=True).start()
        except Exception as e:
            print(f"[WS-TRIGGER] Ошибка: {e}")

def check_trades_now(chat_id: int):
    """Быстрая проверка обменов при получении WS-триггера"""
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        return
    
    if not limiter.can_accept():
        print("[LIMITER] Пропускаем WS-триггер (лимит)")
        return
    
    trades = get_trades(auth)
    for trade in trades:
        if trade['trade_id'] not in processed_trades:
            details = get_trade_details(auth, trade['trade_id'])
            if not details:
                continue
            offered_count = len(details['offered_cards'])
            required_count = len(details['required_cards'])
            
            if required_count == 1 and offered_count >= 2:
                if limiter.can_accept():
                    success, msg = accept_trade(auth, trade['trade_id'])
                    if success:
                        limiter.record_accept()
                        bot.send_message(chat_id, f"✅ **Обмен #{trade['trade_id']} принят!** (WS)", parse_mode='Markdown')
            processed_trades.add(trade['trade_id'])
            save_processed_trades()

def monitoring_loop(chat_id):
    global monitoring_active
    print(f"[TRADE-MONITOR] Запуск для чата {chat_id}")
    print(f"[LIMITER] {limiter.MAX_TRADES_PER_MINUTE} обменов/мин, потом {limiter.REST_MINUTES} мин отдыха")
    
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        monitoring_active = False
        return

    # Запускаем WebSocket
    user_id = auth.get_user_id()
    if user_id:
        start_ws(chat_id, user_id, auth.get_cookies_dict())
        print("[WS] WebSocket запущен для мгновенных уведомлений")

    bot.send_message(
        chat_id,
        f"🔁 Мониторинг обменов запущен.\n"
        f"📊 Лимит: {limiter.MAX_TRADES_PER_MINUTE} обменов/мин, затем {limiter.REST_MINUTES} мин отдыха.\n"
        f"⏱ Проверка каждые {CHECK_INTERVAL} сек.\n"
        f"🔗 WebSocket подключён для мгновенных уведомлений.\n"
        f"Принимаются обмены, где вы отдаёте 1 карту, получаете 2+."
    )

    while monitoring_active:
        try:
            # Проверяем WS-триггер
            check_ws_trigger()

            # Проверяем лимитер
            if not limiter.can_accept():
                stats = limiter.get_stats()
                print(f"[LIMITER] {stats['status']}")
                # Ждём окончания отдыха
                while monitoring_active and limiter.is_resting:
                    time.sleep(5)
                continue

            # HTTP-опрос (резервный канал)
            trades = get_trades(auth)
            new_trades = [t for t in trades if t['trade_id'] not in processed_trades]
            for trade in new_trades:
                processed_trades.add(trade['trade_id'])
                save_processed_trades()

                details = get_trade_details(auth, trade['trade_id'])
                if not details:
                    continue

                offered_count = len(details['offered_cards'])
                required_count = len(details['required_cards'])

                accept = (required_count == 1 and offered_count >= 2)
                result_msg = ""
                
                if accept:
                    if limiter.can_accept():
                        success, msg = accept_trade(auth, trade['trade_id'], max_retries=2)
                        if success:
                            limiter.record_accept()
                            result_msg = "✅ **Обмен автоматически ПРИНЯТ!**"
                            stats = limiter.get_stats()
                            print(f"[LIMITER] Принят обмен. {stats['status']}")
                        else:
                            result_msg = f"❌ **Не удалось принять обмен**: {msg}"
                    else:
                        result_msg = f"⏸ **Обмен пропущен (лимит)** — отдыхаем"
                else:
                    if required_count != 1:
                        reason = f"вы отдаёте {required_count} карт (нужно ровно 1)"
                    elif offered_count < 2:
                        reason = f"вам предлагают {offered_count} карт (нужно 2 и более)"
                    else:
                        reason = "неподходящие условия"
                    result_msg = f"⏩ **Обмен проигнорирован** (получаете:{offered_count} / отдаёте:{required_count}) – {reason}"

                message = f"🔄 **Новое предложение обмена**\n\n"
                message += f"👤 *Отправитель:* {html.escape(details['sender_name'])}\n"
                message += f"🔗 [Ссылка на обмен]({details['url']})\n\n"
                message += f"📦 *Предлагают:* {offered_count} карт\n"
                for card in details['offered_cards']:
                    message += f"  • [Карта]({card['url']})\n"
                message += f"\n📤 *Вы отдаёте:* {required_count} карт\n"
                for card in details['required_cards']:
                    message += f"  • [Карта]({card['url']})\n"
                message += f"\n{result_msg}"

                try:
                    bot.send_message(chat_id, message, parse_mode='Markdown', disable_web_page_preview=True)
                except Exception as e:
                    print(f"Ошибка отправки: {e}")

            # Пауза
            for _ in range(CHECK_INTERVAL):
                if not monitoring_active:
                    break
                if limiter.is_resting:
                    break
                time.sleep(1)
                if _ % 5 == 0:
                    time.sleep(random.uniform(0.1, 0.5))
                    
        except Exception as e:
            print(f"[TRADE-MONITOR] Ошибка: {e}")
            time.sleep(10)

    # Остановка WebSocket
    stop_ws()
    bot.send_message(chat_id, "🔕 Мониторинг обменов остановлен.")


# ==================== КОМАНДЫ БОТА ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "🤖 Бот для автоматического обмена картами на mangabuff.ru\n\n"
        "📊 Лимит: 24 обмена в минуту, затем 1 минута отдыха.\n\n"
        "Команды:\n"
        "/login email password – войти в аккаунт\n"
        "/logout – выйти\n"
        "/status – проверить авторизацию\n"
        "/monitor_start – запустить мониторинг обменов\n"
        "/monitor_stop – остановить мониторинг\n\n"
        "Используйте кнопки для управления.",
        reply_markup=get_keyboard()
    )

@bot.message_handler(commands=['login'])
def cmd_login(message):
    chat_id = message.chat.id
    args = message.text.split()
    if len(args) < 3:
        bot.send_message(chat_id, "❌ Использование: /login email password")
        return
    email = args[1]
    password = args[2]

    bot.send_message(chat_id, "⏳ Выполняю вход...")
    auth = MangaBuffAuth()
    success, result = auth.login(email, password)

    if success:
        user_id = result['user_id']
        save_user_session(chat_id, user_id, result['cookies'])
        bot.send_message(chat_id, f"✅ Успешный вход!\nВаш user_id: {user_id}\nСессия сохранена.")
    else:
        bot.send_message(chat_id, f"❌ Ошибка входа: {result}")

@bot.message_handler(commands=['logout'])
def cmd_logout(message):
    chat_id = message.chat.id
    clear_user_session(chat_id)
    stop_ws()
    bot.send_message(chat_id, "👋 Вы вышли. Сессия очищена.")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    chat_id = message.chat.id
    auth = get_auth_for_user(chat_id)
    
    lines = []
    if auth.is_authenticated():
        user_id = auth.get_user_id()
        lines.append(f"🟢 Авторизован (ID: {user_id})")
    else:
        lines.append("🔴 Не авторизован")
    
    lines.append(f"Мониторинг: {'🔄 запущен' if monitoring_active else '⏹ остановлен'}")
    lines.append(f"WebSocket: {'🔗 подключён' if ws_is_active() else '🔌 отключён'}")
    
    stats = limiter.get_stats()
    lines.append(f"📊 {stats['status']}")
    
    bot.send_message(chat_id, "\n".join(lines), parse_mode='Markdown')

@bot.message_handler(commands=['monitor_start'])
def cmd_monitor_start(message):
    global monitoring_active, monitoring_thread, limiter
    chat_id = message.chat.id
    if monitoring_active:
        bot.send_message(chat_id, "⚠️ Мониторинг уже запущен.")
        return
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        return
    
    # Проверяем, не на паузе ли аккаунт из-за капчи
    account = sessions.get(str(chat_id))
    if captcha_handler.under_captcha(account):
        bot.send_message(chat_id, "⏸ Аккаунт на паузе из-за капчи. Пройдите проверку на сайте и нажмите кнопку «Я прошёл капчу».")
        return
    
    limiter = TradeLimiter()
    monitoring_active = True
    monitoring_thread = threading.Thread(target=monitoring_loop, args=(chat_id,), daemon=True)
    monitoring_thread.start()
    
    bot.send_message(
        chat_id,
        f"✅ Мониторинг обменов запущен.\n"
        f"📊 Лимит: {limiter.MAX_TRADES_PER_MINUTE} обменов/мин, затем {limiter.REST_MINUTES} мин отдыха.\n"
        f"🔗 WebSocket подключается...\n"
        f"📡 HTTP-опрос работает как резервный канал."
    )

@bot.message_handler(commands=['monitor_stop'])
def cmd_monitor_stop(message):
    global monitoring_active
    chat_id = message.chat.id
    if not monitoring_active:
        bot.send_message(chat_id, "ℹ️ Мониторинг не запущен.")
        return
    monitoring_active = False
    stop_ws()
    bot.send_message(chat_id, "⏹ Мониторинг остановлен.")

@bot.message_handler(func=lambda m: m.text in ["🔁 Мониторинг обменов", "📊 Статус"])
def handle_buttons(message):
    text = message.text
    chat_id = message.chat.id
    if text == "🔁 Мониторинг обменов":
        if monitoring_active:
            bot.send_message(chat_id, "⚠️ Мониторинг уже запущен. Используйте /monitor_stop для остановки.")
        else:
            cmd_monitor_start(message)
    elif text == "📊 Статус":
        cmd_status(message)


def run_bot():
    print("✅ Торговый бот запущен. Нажмите Ctrl+C для остановки.")
    print(f"📊 Лимит: {limiter.MAX_TRADES_PER_MINUTE} обменов/мин, затем {limiter.REST_MINUTES} мин отдыха")
    print("📡 Поддерживаются каналы: HTTP-опрос + WebSocket")
    bot.infinity_polling(timeout=60, long_polling_timeout=60)

if __name__ == '__main__':
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\n⏹ Бот остановлен.")
        sys.exit(0)