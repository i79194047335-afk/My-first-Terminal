"""Разбор сырого потока Lighter: нормализация сделок и дедупликация.

Портировано из бота (`Litr_v_odnogo/src/collector/lighter_ticks.py`) — тот код
отработал 8 суток в проде без сбоев, поэтому логика переносится как есть,
без «улучшений». Отличия от оригинала только в зависимостях: здесь стандартный
`logging` вместо `loguru`, чтобы не тянуть в терминал лишний пакет.

ПОЧЕМУ ЭТОТ ФАЙЛ В `feeds/`, А НЕ В `core/`:
`core/*` обязан парситься на Python 3.7 — там же бежит фид FXCM (forexconnect
требует 3.7). Аннотации `dict | None` (PEP 604) и `list[int]` (PEP 585) на 3.7
дают SyntaxError ещё на импорте. Этот модуль импортирует только py3.10-процесс
(`feeds/lighter_feed.py`), поэтому живёт рядом с ним.

Проверенные живьём факты о протоколе (не перепроверять, не додумывать):
  - На WS `price`/`size` — СТРОКИ, в REST — числа. Приводить пофайлово через
    float(), единый тип не предполагать.
  - `t` у сделок — МИЛЛИСЕКУНДЫ. Конвертация в секунды — на границе фида,
    перед отправкой в шину (`core.bus.validate` ловит мс как ошибку).
  - Сервер шлёт `{"type":"ping"}`, надо отвечать `{"type":"pong"}`, иначе
    рвёт соединение примерно через 2 минуты.
  - `subscribed/trade` содержит снапшот последних ~50 сделок; они продублируются
    через `update/trade`, поэтому снапшот пропускается целиком.
"""

from __future__ import annotations

import logging
from collections import deque

log = logging.getLogger("lighter_raw")

# Сколько последних trade_id помнить на рынок. 50 000 — примерно час истории
# по BTC (пик ~15 сделок/сек), памяти это стоит копейки.
DEDUP_WINDOW_PER_MARKET = 50000


def _get_or_none(raw: dict, *keys):
    """Достать первое присутствующее поле из нескольких вариантов имени.

    Lighter в разных местах зовёт одно и то же по-разному (`trade_id` / `tid`,
    `size` / `base_amount`), поэтому имена перебираются по очереди.

    Проверка идёт через `is not None`, а НЕ на истинность: размер `0` —
    валидное значение, а `if raw.get(k)` его бы отбросил.

    Args:
        raw:  Сырой dict сделки.
        keys: Имена полей в порядке предпочтения.

    Returns:
        Значение первого найденного поля или None.
    """
    for k in keys:
        v = raw.get(k)
        if v is not None:
            return v
    return None


def normalize_trade(market_id: int, raw: dict):
    """Привести сырую сделку к плоской записи фиксированной структуры.

    Кривая сделка не роняет процесс: функция возвращает None и пишет
    предупреждение — поток рыночных данных важнее одной записи.

    Args:
        market_id: ID рынка Lighter (число, не тикер).
        raw:       Сырой dict сделки из кадра `update/trade`.

    Returns:
        Dict {"m","p","s","t","side","tid"} либо None, если сделка невалидна.
        Время `t` — МИЛЛИСЕКУНДЫ (как отдаёт биржа); конвертирует вызывающий.
        `side` — сторона АГРЕССОРА: "sell", если мейкером стоял аск.
    """
    tid_v   = _get_or_none(raw, "trade_id", "tid", "id")
    price_v = _get_or_none(raw, "price", "px")
    size_v  = _get_or_none(raw, "size", "base_amount", "sz")
    ts_v    = _get_or_none(raw, "timestamp", "ts", "time")

    if tid_v is None or price_v is None or size_v is None or ts_v is None:
        log.warning("сделка без обязательного поля, рынок %s: %r", market_id, raw)
        return None

    # Сторона агрессора выводится из is_maker_ask. Значение по умолчанию тут
    # НЕДОПУСТИМО: неверная сторона молча испортит дельту и профиль объёма,
    # поэтому сделка без явного bool отбрасывается.
    ima = raw.get("is_maker_ask")
    if not isinstance(ima, bool):
        log.warning("сделка без is_maker_ask (%r), рынок %s — пропуск",
                    ima, market_id)
        return None

    try:
        return {
            "m":    market_id,
            "p":    float(price_v),
            "s":    float(size_v),
            "t":    int(ts_v),
            "side": "sell" if ima else "buy",
            "tid":  int(tid_v),
        }
    except (TypeError, ValueError) as err:
        log.warning("не привести типы сделки, рынок %s: %r (%s)",
                    market_id, raw, err)
        return None


class Deduper:
    """Дедупликация сделок по trade_id с ограниченным окном.

    Множество даёт проверку за O(1), очередь хранит порядок поступления —
    при переполнении вытесняется самый старый идентификатор. Без ограничения
    размера множество росло бы бесконечно: процесс живёт месяцами.
    """

    def __init__(self, max_size: int = DEDUP_WINDOW_PER_MARKET):
        """Создать дедупликатор.

        Args:
            max_size: Сколько последних trade_id помнить.

        Returns:
            None.
        """
        self._seen = set()
        self._order = deque()
        self._max = max_size

    def is_new(self, tid: int) -> bool:
        """Проверить сделку и запомнить её.

        Args:
            tid: Идентификатор сделки.

        Returns:
            True, если сделка встречена впервые; False, если это дубль.
        """
        if tid in self._seen:
            return False

        self._seen.add(tid)
        self._order.append(tid)
        if len(self._order) > self._max:
            evicted = self._order.popleft()
            self._seen.discard(evicted)
        return True
