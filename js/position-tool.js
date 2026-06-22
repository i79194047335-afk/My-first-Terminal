// ============================================================================
// Position Tool — расчёт позиции в стиле TradingView (Long / Short).
// Объект позиции: { type:"position", side, paneId, t1, entry, stop, target,
//                   barsWidth }. Глобальные настройки депозита/риска лежат в
// localStorage и общие для всех позиций.
// ============================================================================

const PositionCalc = (function () {

    const SETTINGS_KEY = "positionCalcSettings";

    /**
     * Прочитать глобальные настройки калькулятора.
     *
     * Returns:
     *   Object: { deposit:number, riskPct:number } — депозит в USD и % риска.
     */
    function getSettings() {
        try {
            const s = JSON.parse(localStorage.getItem(SETTINGS_KEY));
            if (s && typeof s.deposit === "number" && typeof s.riskPct === "number") {
                return s;
            }
        } catch (e) {}
        return { deposit: 10000, riskPct: 1 };
    }

    /**
     * Сохранить глобальные настройки калькулятора.
     *
     * Args:
     *   s (Object): { deposit:number, riskPct:number }.
     */
    function saveSettings(s) {
        localStorage.setItem(SETTINGS_KEY, JSON.stringify(s));
    }

    /**
     * Размер пипса для символа (JPY-пары — 0.01, остальные — 0.0001).
     *
     * Args:
     *   symbol (string): например "EUR/USD".
     * Returns:
     *   number: цена одного пипса.
     */
    function pipSize(symbol) {
        return /JPY/i.test(symbol || "") ? 0.01 : 0.0001;
    }

    /**
     * Рассчитать метрики позиции для отрисовки и плашки.
     *
     * Модель «фиксированный риск депозита»: размер позиции подбирается так,
     * чтобы при срабатывании стопа потерять ровно deposit * riskPct%.
     * Депозит считается в USD; конверсия учитывает сторону USD в паре.
     *
     * Args:
     *   obj (Object): объект позиции с полями entry/stop/target/side.
     *   symbol (string): торговый символ, например "USD/JPY".
     * Returns:
     *   Object: { riskUSD, rewardUSD, rr, riskPips, rewardPips, units, lots,
     *             riskPctPrice, rewardPctPrice, deposit, riskPct }.
     */
    function compute(obj, symbol) {
        const s   = getSettings();
        const pip = pipSize(symbol);

        const entry  = obj.entry;
        const stop   = obj.stop;
        const target = obj.target;

        const riskDist   = Math.abs(entry - stop);
        const rewardDist = Math.abs(target - entry);

        const riskPips   = riskDist / pip;
        const rewardPips = rewardDist / pip;
        const rr         = riskDist > 0 ? rewardDist / riskDist : 0;

        const riskUSD = s.deposit * s.riskPct / 100;

        const parts = (symbol || "").split("/");
        const base  = parts[0];
        const quote = parts[1];

        let units = 0;
        if (riskDist > 0) {
            if (quote === "USD") {
                // котировка в USD: убыток на 1 юнит = riskDist (в USD)
                units = riskUSD / riskDist;
            } else if (base === "USD") {
                // база USD: убыток на 1 юнит в quote, конверсия через цену
                units = riskUSD * entry / riskDist;
            } else {
                // кросс без USD — приблизительно (для текущих символов не нужен)
                units = riskUSD / riskDist;
            }
        }

        const lots      = units / 100000;   // стандартный лот = 100k базовой валюты
        const rewardUSD = riskUSD * rr;

        const riskPctPrice   = entry > 0 ? riskDist   / entry * 100 : 0;
        const rewardPctPrice = entry > 0 ? rewardDist / entry * 100 : 0;

        return {
            riskUSD, rewardUSD, rr,
            riskPips, rewardPips,
            units, lots,
            riskPctPrice, rewardPctPrice,
            deposit: s.deposit, riskPct: s.riskPct
        };
    }

    /**
     * Создать новый объект позиции по клику (дефолтный R:R = 1:2, риск 20 пипсов).
     *
     * Args:
     *   side (string): "long" | "short".
     *   paneId (number): id панели.
     *   t1 (number): unix-время левого края.
     *   entry (number): цена входа (точка клика).
     *   symbol (string): символ для расчёта пипса.
     * Returns:
     *   Object: новый объект позиции.
     */
    function createPosition(side, paneId, t1, entry, symbol) {
        const pip  = pipSize(symbol);
        const risk = 20 * pip;
        const stop   = side === "long" ? entry - risk     : entry + risk;
        const target = side === "long" ? entry + risk * 2 : entry - risk * 2;
        return {
            type: "position",
            side, paneId,
            t1, entry, stop, target,
            barsWidth: 40,
            selected: false
        };
    }

    return { getSettings, saveSettings, pipSize, compute, createPosition };
})();

window.PositionCalc = PositionCalc;
