// ============================================================================
// TimeMapper — слой маппинга time → X
// ============================================================================

class TimeMapper {

    constructor(chart, series) {
        this.chart = chart;
        this.series = series;
        this.tfSeconds = 60; // default
    }

    setTimeframe(tfSeconds) {
        this.tfSeconds = tfSeconds;
    }

    bucketTime(time) {
        if (!time || !this.tfSeconds) return null;
        return Math.floor(time / this.tfSeconds) * this.tfSeconds;
    }

    getX(time) {

    if (!time) return null;

    // нормализуем время под текущий TF
    const bucket = this.bucketTime(time);

    const x = this.chart.timeScale().timeToCoordinate(bucket);

    if (x !== null) return x;

    // fallback если бар не найден
    return this.chart.timeScale().timeToCoordinate(time);
}




}

window.TimeMapper = TimeMapper;