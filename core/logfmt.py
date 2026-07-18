"""
Единая настройка логирования для хаба и фида (Фаза 4 — structured-логи).

Заменяет россыпь print("[hub] …") на модуль logging с уровнями (INFO/WARNING/
ERROR): деградацию теперь видно фильтром по уровню в journald, а не глазами по
тексту. Формат единый и парсируемый.

journald уже проставляет свой timestamp, поэтому в самом формате время НЕ
дублируем — только уровень и компонент. Синтаксис ограничен Python 3.7.
"""

import logging
import sys


def setup(component, level=logging.INFO):
    """Настроить корневой логгер процесса и вернуть логгер компонента.

    Зовётся один раз при старте (hub.main / feed.main). Пишет в stdout —
    systemd/journald перехватывает его сам (как раньше print).

    Args:
        component: Имя компонента для префикса ("hub", "feed").
        level:     Минимальный уровень (logging.INFO по умолчанию).

    Returns:
        logging.Logger — логгер компонента.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s [%(name)s] %(message)s"))
        root.addHandler(handler)
    root.setLevel(level)
    return logging.getLogger(component)
