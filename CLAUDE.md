# Project: Terminal
Web trading terminal (like TradingView, custom-built).

## Stack
- index_split.html — main UI (browser)
- server.py — websocket server (ALWAYS RUNNING, do not restart without permission)
- market_engine.py — market analysis, candles
- pattern_memory.py — patterns and signals
- signal_engine.py — signal generator
- signal_tracker.py — active signal tracker
- structure_engine.py — market structure detection

## Data files (do not delete)
- market_memory.json — data cache
- signal_log.json — signal log
- signal_stats.json — signal statistics

## Rules
- NEVER restart or kill server.py without explicit instruction
- Always add docstrings to Python functions (format: summary, Args, Returns)
- Comments in code: Russian or English, be consistent
- Before any edit to a live file: confirm with user first

## Known issue
- index_split.html breaks via file:// in Chrome/Opera
- Fix: python -m http.server 8080

## Downtime window (safe to edit)
- 21:00–23:00 UTC daily
- 21:00 Fri – 23:00 Sun UTC
