"""
Агент брифинга: вызов DeepSeek и разбор ответа.

Слой 3 подпроекта briefing/. Единственное место, где ходим в LLM. Отделено от
сборки промпта (prompt.py) и источников (sources.py): модель/ключ/парсинг —
одна ответственность, промпт — другая.

Модель — DeepSeek напрямую (openai SDK, base_url api.deepseek.com), ключ
DEEPSEEK_API_KEY из окружения. Смена модели/канала — только здесь.
"""

import json
import os

from openai import OpenAI

DEEPSEEK_BASE    = "https://api.deepseek.com"
# deepseek-v4-pro — умнее flash (=алиас deepseek-chat): тоньше видит расхождения
# консенсуса с техникой, глубже reasoning (сравнение 2026-07-18). Медленнее
# (~80с vs 18с), но для крон-задачи 3×/сутки это неважно; токены дешёвые.
DEEPSEEK_MODEL   = "deepseek-v4-pro"
# Потолок ответа. Pro реально пишет ~7000 токенов на 4 пары (finish_reason=stop);
# 7500 — запас от обрыва JSON, не цель. Платим за реально сгенерированное.
DEFAULT_MAX_TOKENS = 7500


class AgentError(Exception):
    """Ошибка вызова LLM или разбора её ответа."""


def generate(system_prompt, user_prompt, max_tokens=DEFAULT_MAX_TOKENS,
             temperature=0.3):
    """Отправить промпты в DeepSeek и вернуть разобранный JSON брифинга.

    Args:
        system_prompt: Системный промпт (роль, формат).
        user_prompt:   Пользовательский промпт (данные).
        max_tokens:    Потолок ответа (страховка от обрыва JSON, не цель).
        temperature:   Низкая — брифинг аналитический, не творческий.

    Returns:
        Dict разобранного ответа модели.

    Raises:
        AgentError: нет ключа, ошибка API, или ответ — не валидный JSON.
    """
    # Ключ читается В МОМЕНТ ВЫЗОВА, а не при импорте. Раньше он лежал в
    # константе модуля, и это тихо убивало брифинг из крона: run.py делает
    # `from briefing.agent import generate` РАНЬШЕ, чем зовёт load_dotenv(),
    # так что на момент импорта .env ещё не прочитан и константа замерзала
    # пустой. Вручную работало — там ключ был в окружении до старта.
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise AgentError("нет DEEPSEEK_API_KEY в окружении")

    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE)
    try:
        resp = client.chat.completions.create(
            model=DEEPSEEK_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        raw = resp.choices[0].message.content.strip()
    except Exception as err:
        raise AgentError("DeepSeek API: %r" % (err,))

    return _parse_json(raw)


def _parse_json(raw):
    """Разобрать JSON из ответа модели, сняв markdown-обёртку при наличии.

    Модель иногда оборачивает JSON в ```json … ``` вопреки инструкции — снимаем.

    Args:
        raw: Сырой текст ответа.

    Returns:
        Dict.

    Raises:
        AgentError: если после очистки это не JSON.
    """
    text = raw.strip()
    if text.startswith("```"):
        # ```json\n{...}\n```  →  {...}
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as err:
        raise AgentError("ответ не JSON: %s | начало: %s" % (err, text[:300]))
