#!/usr/bin/env python3
# ============================================================================
#  УВЕДОМЛЕНИЕ О КАПЧЕ ЧЕРЕЗ TELEGRAM — «мягкая» деградация, БЕЗ обхода.
# ============================================================================
#  ИДЕЯ: бот капчу НЕ решает и НЕ обходит. Когда сайт показывает капчу — бот:
#    1) обнаруживает её (по редиректу на страницу капчи или по разметке),
#    2) СТАВИТ аккаунт на паузу (не долбит сайт впустую),
#    3) шлёт владельцу в Telegram уведомление с кнопкой «Я прошёл капчу»,
#    4) человек САМ проходит капчу на сайте и жмёт кнопку → бот снимает паузу.
# ============================================================================

import re
import time
from typing import Optional, Dict, Any, Callable

# ============================================================================
#  ДЕТЕКТ (чистые функции, без зависимостей)
# ============================================================================

def is_captcha_url(url: str) -> bool:
    """
    КАПЧА ПО URL — определяющий сигнал: сайт редиректит на /security/page-captcha.
    """
    return bool(re.search(r'/security/(?:page-)?captcha|/page-captcha', str(url or ''), re.IGNORECASE))


def has_captcha_in_html(html: str) -> bool:
    """
    Признаки капчи В РАЗМЕТКЕ — запасной путь.
    """
    h = str(html or '')
    patterns = [
        r'g-recaptcha', r'grecaptcha', r'google\.com/recaptcha',
        r'data-sitekey', r'h-captcha', r'hcaptcha',
        r'не\s*робот', r'подтвердите[,\s]+что\s*вы',
        r'проверка\s*браузера', r'checking\s*your\s*browser'
    ]
    for pattern in patterns:
        if re.search(pattern, h, re.IGNORECASE):
            return True
    return False


def captcha_should_notify(account: Optional[Dict]) -> bool:
    """Уведомлять о капче СЕЙЧАС? Только если ещё не уведомляли."""
    if not account:
        return True
    notified = account.get('captcha_notified')
    return not (notified == 1 or notified is True)


# ============================================================================
#  ОСНОВНОЙ КЛАСС
# ============================================================================

class CaptchaHandler:
    DEFAULT_PAUSE_MS = 15 * 60 * 1000  # 15 минут

    def __init__(
        self,
        db: Optional[Dict[str, Any]] = None,
        notify: Optional[Callable] = None,
        get_runtime: Optional[Callable] = None,
        tag: Optional[Callable] = None,
        pause_ms: int = None
    ):
        self.db = db or {}
        self.notify = notify
        self.get_runtime = get_runtime or (lambda _: {})
        self.tag = tag or (lambda _: '')
        self.pause_ms = pause_ms or self.DEFAULT_PAUSE_MS
        self._state_store: Dict[int, Dict] = {}

    def _get_state(self, acc_id: int) -> Dict:
        if self.get_runtime:
            return self.get_runtime(acc_id)
        if acc_id not in self._state_store:
            self._state_store[acc_id] = {}
        return self._state_store[acc_id]

    def _update_account(self, acc_id: int, patch: Dict) -> None:
        if self.db and hasattr(self.db, 'update_account'):
            self.db.update_account(acc_id, patch)
        elif self.db and 'update_account' in self.db:
            self.db['update_account'](acc_id, patch)

    def _get_account(self, acc_id: int) -> Optional[Dict]:
        if self.db and hasattr(self.db, 'get_account'):
            return self.db.get_account(acc_id)
        elif self.db and 'get_account' in self.db:
            return self.db['get_account'](acc_id)
        return None

    def _now(self) -> int:
        if self.db and hasattr(self.db, 'now'):
            return self.db.now()
        return int(time.time() * 1000)

    def flag_captcha(self, account: Dict, state: Dict = None) -> None:
        acc_id = account.get('id')
        tg_id = account.get('tg_id')
        if not acc_id:
            return

        until = self._now() + self.pause_ms
        state_obj = state or self._get_state(acc_id)
        state_obj['captcha_until'] = until

        self._update_account(acc_id, {
            'captcha_until': until,
            'captcha_notified': 1,
            'last_status': 'капча на сайте — нужен ручной вход',
            'last_run': self._now()
        })

        if captcha_should_notify(account) and self.notify:
            name = self.tag(account)
            pause_minutes = round(self.pause_ms / 60000)
            text = (
                f"🤖 {name}сайт показывает капчу. Бот приостановил аккаунт.\n\n"
                f"Зайди на mangabuff.ru вручную, пройди проверку, "
                f"потом нажми кнопку ниже (или подожди {pause_minutes} мин)."
            )
            reply_markup = {
                'inline_keyboard': [[{
                    'text': '✅ Я прошёл капчу — продолжить',
                    'callback_data': f'cap_ok:{acc_id}'
                }]]
            }
            try:
                self.notify(tg_id, text, reply_markup)
            except Exception as e:
                print(f"[CAPTCHA] Ошибка отправки: {e}")

    def resume_from_captcha(self, acc_id: int) -> None:
        state = self._get_state(acc_id)
        if state:
            state['captcha_until'] = 0
            state['next_run'] = 0
            state['captcha_date'] = None

        try:
            self._update_account(acc_id, {'captcha_until': 0, 'captcha_notified': 0})
        except Exception as e:
            print(f"[CAPTCHA] Ошибка снятия паузы: {e}")

    def under_captcha(self, account: Dict) -> bool:
        if not account:
            return False
        captcha_until = account.get('captcha_until', 0)
        return bool(captcha_until and self._now() < captcha_until)

    def is_captcha_response(self, response, chat_id: int = None) -> bool:
        url = getattr(response, 'url', '')
        html = getattr(response, 'text', '')

        if is_captcha_url(url):
            account = None
            if self.db and chat_id:
                if hasattr(self.db, 'get_account_by_tg'):
                    account = self.db.get_account_by_tg(chat_id)
                elif 'get_account_by_tg' in self.db:
                    account = self.db['get_account_by_tg'](chat_id)
            if account:
                self.flag_captcha(account)
            elif chat_id and self.notify:
                self.notify(chat_id, "⚠️ Обнаружена капча! Зайдите на сайт вручную.", None)
            return True

        if has_captcha_in_html(html) and 'window.user_id' not in html:
            account = None
            if self.db and chat_id:
                if hasattr(self.db, 'get_account_by_tg'):
                    account = self.db.get_account_by_tg(chat_id)
                elif 'get_account_by_tg' in self.db:
                    account = self.db['get_account_by_tg'](chat_id)
            if account:
                self.flag_captcha(account)
            elif chat_id and self.notify:
                self.notify(chat_id, "⚠️ Обнаружена капча! Зайдите на сайт вручную.", None)
            return True

        return False


# ============================================================================
#  ФАБРИКА ДЛЯ СОЗДАНИЯ
# ============================================================================

def create_captcha_handler(
    db: Dict = None,
    notify: Callable = None,
    get_runtime: Callable = None,
    tag: Callable = None,
    pause_ms: int = 15 * 60 * 1000
) -> CaptchaHandler:
    return CaptchaHandler(db=db, notify=notify, get_runtime=get_runtime, tag=tag, pause_ms=pause_ms)