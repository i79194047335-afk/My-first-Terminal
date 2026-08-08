"""
Конфигурация бота: bot_config.json + .env. Python 3.10.

Разделение жёсткое и намеренное:
  * bot_config.json — параметры поведения (режим, ставки, ограничители).
    Файл в .gitignore, но по сути не секретный.
  * .env — только секреты (email, пароль, user_hash). В репозиторий не
    попадает никогда, в логи — тоже (user_hash маскируется).

Режим по умолчанию — самый безопасный (dry, к площадке не ходит вовсе).
Забытый в конфиге режим не должен означать «торгуй по-настоящему», поэтому
любое неизвестное значение тоже сводится к dry, а не принимается на веру.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Optional

# Режимы работы (§8 ТЗ). Порядок — по возрастанию опасности.
MODES = ("dry", "shadow", "demo", "live")

DEFAULT_CONFIG_PATH = "bot_config.json"

# Умолчания повторяют §13 ТЗ. Живут в коде, а не только в примере конфига:
# бот должен подниматься и без файла, в самом безопасном виде.
DEFAULTS = {
    "mode": "dry",
    "account": "demo",
    "base_url": "https://intrade35.bar",
    "quotes_url": "https://intrade.bar/price_now",
    "time_ws_url": "wss://intrade35.bar/req_info",
    "user_id": None,
    "user_hash": None,
    "default_investment": 1,
    "default_expiry_minutes": 1,
    "symbol_whitelist": ["AUD/USD", "BTC/USDT", "EUR/USD", "USD/CAD", "USD/JPY"],
    # Минимальная экспирация по инструменту, когда площадка не принимает
    # общую. BTC/USDT на 1 и 3 минутах отвечает error_time_btc и открывается
    # только с 5 (проверено живьём 2026-08-08, id 224849601). Форекс в этом
    # словаре не нужен — ему хватает default_expiry_minutes.
    "min_expiry_minutes": {"BTC/USDT": 5},
    "sources": ["manual"],
    "risk": {
        "max_trades_per_day": 20,
        "max_concurrent": 1,
        "cooldown_sec": 60,
        "max_consecutive_losses": 3,
        "max_daily_loss": 20,
        "min_payout_percent": 75,
        "allowed_hours": [[6, 18]],
    },
    # Адрес панели. По умолчанию только localhost: панель умеет открывать
    # сделки, и пароля у неё нет. "0.0.0.0" открывает её всему интернету —
    # осознанный выбор, который стоит делать только в режиме dry.
    "panel_host": "127.0.0.1",
    "panel_port": 8788,
    # Токен панели. Пока пусто — панель без пароля (только localhost).
    # При заданном токене панель принимает соединение только после
    # {cmd: "auth", token: ...} в первые AUTH_TIMEOUT секунд; состояние
    # и команды до авторизации не отдаются. Токен — секрет, жить должен
    # в .env (INTRADE_PANEL_TOKEN), а не в bot_config.json.
    "panel_token": None,
    "db_path": "bot_journal.db",
    "stop_file": "bot/STOP",

    # ПРЕДОХРАНИТЕЛЬ ПО БАЛАНСУ. Площадка НЕ сообщает через API, какой счёт
    # активен — демо или реальный: balance.php один на оба и отдаёт баланс
    # того, что выбран в браузере. Переключение делается в кабинете и на
    # user_hash никак не отражается.
    #
    # Отсюда дыра, которую поле "mode" закрыть не может: конфиг говорит
    # "demo", на площадке активен реал — и ставки уходят с реальных денег,
    # а все три рубежа защиты live спокойно спят.
    #
    # Единственный доступный признак — величина баланса. Демо у intrade.bar
    # исчисляется тысячами, реальный счёт владельца — единицами долларов.
    # Поэтому: перед КАЖДОЙ реальной ставкой баланс сверяется с этим
    # порогом, и если он ниже — бот отказывается торговать.
    #
    # Значение подобрано по факту: демо ≈ 9363 $, реал ≈ 11.56 $ (2026-08-06).
    # Это груборезкий, но работающий рубеж; точнее площадка знать не даёт.
    "min_balance_for_demo": 1000.0,
}


@dataclass
class RiskConfig:
    """Пороги ограничителей (§7 ТЗ).

    Attributes:
        max_trades_per_day:     Потолок числа сделок в сутки.
        max_concurrent:         Сколько сделок может быть открыто одновременно.
        cooldown_sec:           Минимальный интервал между сделками.
        max_consecutive_losses: Стоп после N убытков подряд.
        max_daily_loss:         Стоп при просадке за день.
        min_payout_percent:     Не входить, если выплата ниже порога.
        allowed_hours:          Окна [[от, до], …] в часах UTC.
    """

    max_trades_per_day: int = 20
    max_concurrent: int = 1
    cooldown_sec: int = 60
    max_consecutive_losses: int = 3
    max_daily_loss: float = 20.0
    min_payout_percent: int = 75
    allowed_hours: list = field(default_factory=lambda: [[6, 18]])


@dataclass
class BotConfig:
    """Полная конфигурация бота.

    Attributes:
        mode:                   dry / shadow / demo / live.
        account:                demo / real — какой счёт подразумевается.
        base_url:               Торговый домен (intrade35.bar).
        quotes_url:             Котировки — ДРУГОЙ домен (intrade.bar).
        time_ws_url:            WS времени сервера.
        user_id:                Идентификатор пользователя площадки.
        user_hash:              Ключ от аккаунта; в логи только маскированным.
        default_investment:     Ставка по умолчанию.
        default_expiry_minutes: Экспирация по умолчанию, минуты.
        symbol_whitelist:       Разрешённые инструменты (со слэшем).
        min_expiry_minutes:     Минимальная экспирация по инструменту:
                                {"BTC/USDT": 5}. Площадка отвергает более
                                короткие (error_time_btc).
        sources:                Имена включённых источников сигналов.
        risk:                   Пороги ограничителей.
        panel_host:             Адрес панели. 127.0.0.1 — только локально
                                (умолчание); 0.0.0.0 — открыть наружу.
                                Наружу панель пускается ТОЛЬКО вместе с
                                panel_token, иначе Call/Put доступны всем.
        panel_port:             Порт панели наблюдения.
        panel_token:            Секрет для авторизации панели; None — без
                                пароля (локально). Обычно из .env.
        db_path:                Файл журнала (НЕ market.db).
        stop_file:              Путь kill-switch: есть файл — входы запрещены.
    """

    mode: str = "dry"
    account: str = "demo"
    base_url: str = DEFAULTS["base_url"]
    quotes_url: str = DEFAULTS["quotes_url"]
    time_ws_url: str = DEFAULTS["time_ws_url"]
    user_id: Optional[int] = None
    user_hash: Optional[str] = None
    default_investment: float = 1.0
    default_expiry_minutes: int = 1
    symbol_whitelist: list = field(default_factory=lambda: list(DEFAULTS["symbol_whitelist"]))
    min_expiry_minutes: dict = field(
        default_factory=lambda: dict(DEFAULTS["min_expiry_minutes"]))
    sources: list = field(default_factory=lambda: ["manual"])
    risk: RiskConfig = field(default_factory=RiskConfig)
    panel_host: str = "127.0.0.1"
    panel_port: int = 8788
    panel_token: Optional[str] = None
    min_balance_for_demo: float = 1000.0
    db_path: str = "bot_journal.db"
    stop_file: str = "bot/STOP"

    def expiry_for(self, symbol: str) -> int:
        """Экспирация для инструмента с учётом минимума площадки.

        У BTC/USDT минимальная экспирация 5 минут: на 1 и 3 площадка
        отвечает error_time_btc (проверено живьём 2026-08-08). Общая
        default_expiry_minutes для него не годится, поэтому берём большее
        из общей и минимальной для инструмента.

        Args:
            symbol: Инструмент со слэшем, например "BTC/USDT".

        Returns:
            Экспирация в минутах.
        """
        minimal = self.min_expiry_minutes.get(symbol, 0)
        return max(self.default_expiry_minutes, int(minimal or 0))

    @property
    def is_live(self) -> bool:
        """Идёт ли речь о реальных деньгах.

        Returns:
            True только для режима live.
        """
        return self.mode == "live"

    @property
    def touches_platform(self) -> bool:
        """Ходит ли бот в этом режиме к площадке торговыми запросами.

        dry не ходит вовсе, shadow читает котировки, но не ставит.
        Свойство нужно исполнителю, чтобы одно место решало «слать или нет».

        Returns:
            True для demo и live.
        """
        return self.mode in ("demo", "live")


def load_env(path: str = ".env") -> dict:
    """Прочитать .env в словарь, не трогая os.environ.

    Свой разбор, а не python-dotenv: зависимость ради двадцати строк не
    нужна, а формат файла здесь простейший (KEY=VALUE, # — комментарий).
    Значения из настоящего окружения имеют приоритет над файлом — так
    systemd EnvironmentFile и ручной запуск ведут себя одинаково.

    Args:
        path: Путь к файлу .env.

    Returns:
        Словарь переменных; пустой, если файла нет.
    """
    values = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return values


def load(path: str = DEFAULT_CONFIG_PATH, env_path: str = ".env") -> BotConfig:
    """Собрать конфигурацию из файла, .env и умолчаний.

    Приоритет (по возрастанию): DEFAULTS → bot_config.json → .env/окружение.
    Секреты берутся ТОЛЬКО из окружения и .env: user_hash в bot_config.json
    допустим (файл в .gitignore), но переменная окружения его перекрывает.

    Args:
        path:     Путь к bot_config.json. Отсутствие файла — не ошибка.
        env_path: Путь к .env.

    Returns:
        BotConfig с проверенными значениями.
    """
    data = dict(DEFAULTS)

    try:
        with open(path, "r", encoding="utf-8") as handle:
            data.update(json.load(handle) or {})
    except OSError:
        pass  # Нет файла — работаем на умолчаниях, это штатный сценарий.
    except ValueError as err:
        raise ValueError(f"{path}: битый JSON ({err})") from err

    env = load_env(env_path)

    def from_env(key: str):
        """Взять значение из окружения, затем из .env.

        Args:
            key: Имя переменной.

        Returns:
            Строка или None.
        """
        return os.environ.get(key) or env.get(key)

    risk_data = dict(DEFAULTS["risk"])
    risk_data.update(data.get("risk") or {})
    # Отбрасываем незнакомые ключи: опечатка в конфиге не должна валить старт,
    # но и молча становиться «настройкой» тоже не должна — она просто игнор.
    known = RiskConfig().__dict__.keys()
    risk = RiskConfig(**{k: v for k, v in risk_data.items() if k in known})

    user_id = from_env("INTRADE_USER_ID") or data.get("user_id")
    user_hash = from_env("INTRADE_USER_HASH") or data.get("user_hash")

    mode = str(data.get("mode", "dry")).lower()
    if mode not in MODES:
        # Неизвестный режим — это опечатка, и трактовать её надо в
        # безопасную сторону, а не в сторону реальных ставок.
        mode = "dry"

    config = BotConfig(
        mode=mode,
        account=str(data.get("account", "demo")),
        base_url=str(data.get("base_url") or DEFAULTS["base_url"]).rstrip("/"),
        quotes_url=str(data.get("quotes_url") or DEFAULTS["quotes_url"]),
        time_ws_url=str(data.get("time_ws_url") or DEFAULTS["time_ws_url"]),
        user_id=int(user_id) if user_id else None,
        user_hash=str(user_hash) if user_hash else None,
        default_investment=float(data.get("default_investment", 1)),
        default_expiry_minutes=int(data.get("default_expiry_minutes", 1)),
        symbol_whitelist=list(data.get("symbol_whitelist") or []),
        min_expiry_minutes=dict(data.get("min_expiry_minutes")
                                or DEFAULTS["min_expiry_minutes"]),
        sources=list(data.get("sources") or ["manual"]),
        risk=risk,
        panel_host=str(data.get("panel_host") or DEFAULTS["panel_host"]),
        panel_port=int(data.get("panel_port", 8788)),
        # Токен — секрет: окружение и .env важнее конфига. Пустая строка
        # из окружения (случайно) НЕ должна отключать токен из конфига.
        panel_token=(from_env("INTRADE_PANEL_TOKEN")
                     or data.get("panel_token") or None),
        min_balance_for_demo=float(data.get("min_balance_for_demo",
                                            DEFAULTS["min_balance_for_demo"])),
        db_path=str(data.get("db_path") or "bot_journal.db"),
        stop_file=str(data.get("stop_file") or "bot/STOP"),
    )
    validate(config)
    return config


def validate(config: BotConfig) -> None:
    """Проверить конфигурацию на противоречия.

    Отдельная функция, а не проверки по месту: правила режима live должны
    читаться одним куском, иначе легко ослабить их незаметно.

    Args:
        config: Собранная конфигурация.

    Returns:
        None.

    Raises:
        ValueError: Конфигурация запрещена (например, live без разрешений).
    """
    if config.default_investment <= 0:
        raise ValueError("default_investment должен быть больше нуля")
    if config.default_expiry_minutes <= 0:
        raise ValueError("default_expiry_minutes должен быть больше нуля")

    if config.is_live:
        # Тройная защита из §8 ТЗ: флага в конфиге мало, нужна ещё и
        # переменная окружения. Третий рубеж — подтверждение в панели —
        # живёт в исполнителе, здесь его проверить нельзя.
        if os.environ.get("INTRADE_ALLOW_LIVE") != "yes":
            raise ValueError(
                "режим live запрещён: нет INTRADE_ALLOW_LIVE=yes в окружении. "
                "В текущей задаче live не включается вовсе"
            )

        # Реальные деньги и панель без пароля в открытом интернете — это
        # сочетание не должно существовать даже случайно.
        if config.panel_host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(
                f"режим live с панелью на {config.panel_host} запрещён: "
                "у панели нет пароля, кнопка Call доступна всем. "
                "Для live оставьте panel_host = 127.0.0.1 и ходите "
                "через ssh-туннель"
            )

    # Панель наружу (не localhost) и без токена — открытая дверь к Call/Put.
    # Токен — единственный замок: без него состояние и команды получает любой,
    # кто достучался до порта. Действует во всех режимах, включая demo.
    if config.panel_host not in ("127.0.0.1", "localhost", "::1") \
            and not config.panel_token:
        raise ValueError(
            f"панель на {config.panel_host} без panel_token запрещена: "
            "у панели нет пароля, кнопка Call доступна всем. "
            "Задайте INTRADE_PANEL_TOKEN (в .env) или оставьте "
            "panel_host = 127.0.0.1 и ходите через ssh-туннель"
        )
