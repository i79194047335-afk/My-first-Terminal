"""
Получение и проверка user_hash. Python 3.10.

На /login стоит reCAPTCHA — единственное место площадки, где нужен браузер.
Дальше он не нужен вовсе: все прочие вызовы авторизуются парой полей формы
user_id + user_hash, cookie не участвуют (проверено HAR-записью).

Отсюда стратегия: хеш добывается РУКАМИ один раз и кладётся в .env. Автомата
с обходом капчи здесь нет и не будет — и потому, что это гонка, которую не
выиграть, и потому, что автоматизация против правил площадки (на демо
безразлично, на реальном счёте — риск блокировки).

Открытый вопрос, от которого зависит вся эксплуатация: СКОЛЬКО ЖИВЁТ ХЕШ.
Проверяется просто — сохранить и через сутки дёрнуть balance.php. Если живёт
долго, браузер нужен раз в жизни; если протухает за часы, придётся
возвращаться к Playwright. Функция probe() ниже как раз для такой проверки.
"""

from __future__ import annotations

from typing import Optional

from bot.api.client import IntradeClient
from bot.api.models import Credentials, PlatformError, mask_hash
from bot.api.parsers import extract_credentials
from core.logfmt import setup as _log_setup

log = _log_setup("bot-auth")

# Инструкция для человека. Держим в коде, а не только в документации:
# читать её будут в момент отказа, когда лезть в README некогда.
MANUAL_STEPS = """
Как добыть user_hash (делается один раз):

  1. Зайти на https://intrade35.bar в обычном браузере, залогиниться.
  2. F12 → вкладка Console.
  3. Выполнить:   console.log(user_id, user_hash)
     (обе переменные объявлены в HTML торговой страницы)
  4. Записать значения в .env рядом с проектом:

         INTRADE_USER_ID=30169
         INTRADE_USER_HASH=<32 hex-символа>

  5. Проверить:   python3.10 -m bot.run check

Хеш — это ключ от аккаунта. В репозиторий не коммитить, в чат не вставлять,
в логах он всегда маскируется до первых 6 символов.
"""


def credentials_from_config(config) -> Optional[Credentials]:
    """Собрать Credentials из конфигурации.

    Args:
        config: BotConfig с полями user_id и user_hash.

    Returns:
        Credentials либо None, если пара не задана целиком.
    """
    if not config.user_id or not config.user_hash:
        return None
    return Credentials(user_id=int(config.user_id), user_hash=str(config.user_hash))


def credentials_from_html(html: str) -> Optional[Credentials]:
    """Выковырять пару из сохранённой HTML торговой страницы.

    Запасной путь, если удобнее сохранить страницу целиком (Ctrl+S), чем
    копировать переменные из консоли.

    Args:
        html: HTML торговой страницы.

    Returns:
        Credentials либо None, если в странице нет обеих переменных.
    """
    user_id, user_hash = extract_credentials(html)
    if not user_id or not user_hash:
        return None
    return Credentials(user_id=user_id, user_hash=user_hash)


def probe(client: IntradeClient) -> tuple:
    """Проверить, жив ли хеш, дёрнув balance.php.

    Это же и есть ответ на открытый вопрос о сроке жизни хеша: сохранить
    значение, вызвать probe через сутки и записать результат в LOG.md.

    Args:
        client: Клиент с установленными учётными данными.

    Returns:
        Кортеж (жив ли хеш: bool, описание: str).
    """
    try:
        balance = client.balance()
    except PlatformError as err:
        log.warning("хеш %s не прошёл проверку: %s",
                    mask_hash(client.credentials.user_hash if client.credentials else None),
                    err)
        return False, str(err)

    log.info("хеш %s жив, баланс %.2f %s",
             mask_hash(client.credentials.user_hash),
             balance.amount, balance.currency)
    return True, f"баланс {balance.amount:.2f} {balance.currency}"
