# Terminal — Web Trading Terminal

## Что это
Browser-based веб-терминаль для TradingView (custom built).
Показывает тиковые данные брокера в реальном времени.
Stack: HTML/JS frontend + Python websocket server.

## Расположение на VPS
/root/projects/terminal/

## Структура файлов
```
terminal/
├── History/             # история сделок
├── data/                 # тиковые данные с лайв-servera
├── js/                  # JavaScript модули
├── vel_log/             # логи волатильности
├── index_split.html     # главный UI (браузер)
├── server.py           # websocket сервер, связь с брокером
├── market_engine.py     # анализ рынка, кандлер
├── pattern_memory.py    # паттерны (сигналы)
├── signal_engine.py     # генератор сигналов
├── signal_tracker.py    # трекер активных сигналов
├── structure_engine.py  # выявление рыночной структуры
├── market_memory.json   # кэш данных (7.3 MB)
├── signal_log.json      # лог сигналов (2.9 MB)
└── signal_stats.json    # статистика сигналов
```

## Как запустить
```bash
cd /root/projects/terminal
python server.py
# Открыть браузер: http://localhost:8080
```

## Известные проблемы
> index_split.html не работает через file:// в Chrome/Opera
> Фикс: python -m http.server 8080 — запускать через localhost!
> Не открывать файл двойным кликом.

## Текущие задачи
- [ ] улучшить UX кнопок
- [ ] добавить звуковые алерты
- [ ] улучшить производительность

## Как начать чат с AI
```
Read my context file AI_LEARNING_ROADMAP_v3.md from Google Drive.
I am working on Terminal project.
Project files: /root/projects/terminal/
Today I want to: [задача]
```
