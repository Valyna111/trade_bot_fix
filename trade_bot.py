#!/usr/bin/env python3
"""
Telegram бот для автоматического принятия выгодных обменов на mangabuff.ru
Принимает предложения, где:
Вы отдаёте 1 карту, а получаете 2 и более (2:1, 3:1, 4:1, ...)
"""

import os
import sys
import json
import re
import time
import threading
import html
import random  # ДОБАВЛЕНО для случайной задержки
from pathlib import Path
from urllib.parse import unquote

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
    print("[WARN] curl_cffi не установлен, используется requests. Возможны проблемы с Cloudflare.")

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

# ==================== КЛАСС АВТОРИЗАЦИИ ====================
class MangaBuffAuth:
    BASE_URL = "https://mangabuff.ru"

    def __init__(self, proxy: dict = None, impersonate: str = "chrome131"):
        self.impersonate = impersonate
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
            user_id = match.group(1)
            cookies = []
            for name, value in self.session.cookies.items():
                cookies.append({'name': name, 'value': value, 'domain': 'mangabuff.ru'})
            return True, {'user_id': user_id, 'cookies': cookies}
        else:
            return False, 'User ID not found after login'

    def load_cookies(self, cookies_list: list):
        for c in cookies_list:
            name = c.get('name')
            value = c.get('value')
            domain = c.get('domain', 'mangabuff.ru')
            if name and value:
                self.session.cookies.set(name, value, domain=domain)

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

def accept_trade(auth: MangaBuffAuth, trade_id: str, max_retries: int = 2):  # ИЗМЕНЕНО: было 3, стало 2
    """
    Принимает обмен с повторными попытками при ошибке
    max_retries - максимальное количество попыток (по умолчанию 2)
    """
    for attempt in range(max_retries):
        if attempt > 0:
            print(f"[RETRY] Попытка {attempt + 1}/{max_retries} для обмена {trade_id}")
            time.sleep(5)  # ИЗМЕНЕНО: было 10, стало 5
        
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

CHECK_INTERVAL = 10  # ИЗМЕНЕНО: было 30, стало 15 (оптимальный баланс скорости и безопасности)
SESSIONS_FILE = Path(__file__).parent / "tg_sessions.json"
PROCESSED_TRADES_FILE = Path(__file__).parent / "processed_trades.json"

sessions = {}
processed_trades = set()
monitoring_active = False
monitoring_thread = None

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

# ==================== МОНИТОРИНГ ====================
def monitoring_loop(chat_id):
    global monitoring_active
    print(f"[TRADE-MONITOR] Запуск для чата {chat_id}")
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        monitoring_active = False
        return

    bot.send_message(chat_id, f"🔁 Мониторинг обменов запущен. Проверка каждые {CHECK_INTERVAL} сек.\nПринимаются обмены, где вы отдаёте ровно 1 карту и получаете 2 или более карт (2:1, 3:1, 4:1, ...).")

    while monitoring_active:
        try:
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

                # УСЛОВИЕ: отдаём ровно 1 карту, получаем 2 или более
                accept = (required_count == 1 and offered_count >= 2)
                result_msg = ""
                
                if accept:
                    success, msg = accept_trade(auth, trade['trade_id'], max_retries=2)  # ИЗМЕНЕНО: было 3, стало 2
                    if success:
                        result_msg = "✅ **Обмен автоматически ПРИНЯТ!**"
                    else:
                        result_msg = f"❌ **Не удалось принять обмен после 2 попыток**: {msg}"
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

            # ИЗМЕНЕНО: добавлена случайная задержка для имитации человека
            for _ in range(CHECK_INTERVAL):
                if not monitoring_active:
                    break
                time.sleep(1)
                # Добавляем случайную микро-задержку каждые 5 секунд
                if _ % 5 == 0:
                    time.sleep(random.uniform(0.1, 0.5))
                    
        except Exception as e:
            print(f"[TRADE-MONITOR] Ошибка: {e}")
            time.sleep(10)

    bot.send_message(chat_id, "🔕 Мониторинг обменов остановлен.")

# ==================== КОМАНДЫ БОТА ====================
@bot.message_handler(commands=['start'])
def cmd_start(message):
    bot.send_message(
        message.chat.id,
        "🤖 Бот для автоматического обмена картами на mangabuff.ru\n\n"
        "Команды:\n"
        "/login email password – войти в аккаунт\n"
        "/logout – выйти\n"
        "/status – проверить авторизацию\n"
        "/monitor_start – запустить мониторинг обменов (автопринятие, если вы отдаёте 1 карту, а получаете 2+)\n"
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
    bot.send_message(chat_id, "👋 Вы вышли. Сессия очищена.")

@bot.message_handler(commands=['status'])
def cmd_status(message):
    chat_id = message.chat.id
    auth = get_auth_for_user(chat_id)
    if auth.is_authenticated():
        user_id = auth.get_user_id()
        bot.send_message(chat_id, f"🟢 Вы авторизованы\nUser ID: {user_id}")
    else:
        bot.send_message(chat_id, "🔴 Вы не авторизованы. Используйте /login")

@bot.message_handler(commands=['monitor_start'])
def cmd_monitor_start(message):
    global monitoring_active, monitoring_thread
    chat_id = message.chat.id
    if monitoring_active:
        bot.send_message(chat_id, "⚠️ Мониторинг уже запущен.")
        return
    auth = get_auth_for_user(chat_id)
    if not auth.is_authenticated():
        bot.send_message(chat_id, "❌ Вы не авторизованы. Используйте /login")
        return
    monitoring_active = True
    monitoring_thread = threading.Thread(target=monitoring_loop, args=(chat_id,), daemon=True)
    monitoring_thread.start()
    bot.send_message(chat_id, "✅ Мониторинг обменов запущен.")

@bot.message_handler(commands=['monitor_stop'])
def cmd_monitor_stop(message):
    global monitoring_active
    if not monitoring_active:
        bot.send_message(message.chat.id, "ℹ️ Мониторинг не запущен.")
        return
    monitoring_active = False
    bot.send_message(message.chat.id, "⏹ Мониторинг остановлен.")

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
    while True:
        try:
            print("✅ Торговый бот запущен. Нажмите Ctrl+C для остановки.")
            bot.infinity_polling(timeout=60, long_polling_timeout=60)
        except Exception as e:
            print(f"❌ Ошибка соединения: {e}. Переподключение через 10 секунд...")
            time.sleep(10)

if __name__ == '__main__':
    run_bot()