import { useEffect, useRef } from "react";
import { ColorType, createChart, type IChartApi, type UTCTimestamp } from "lightweight-charts";
import type { Point } from "../api";

export interface Series {
  name: string;
  points: Point[];
  color: string;
  area?: boolean;
}

const css = (v: string) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();

function toData(points: Point[]) {
  const seen = new Set<number>();
  const out: { time: UTCTimestamp; value: number }[] = [];
  for (const [t, v] of points) {
    const ts = Math.floor(new Date(t).getTime() / 1000) as UTCTimestamp;
    if (!seen.has(ts)) {
      seen.add(ts);
      out.push({ time: ts, value: v });
    }
  }
  return out.sort((a, b) => a.time - b.time);
}

/** Lightweight Charts by TradingView (attribution logo kept on, per its licence). */
export function LineChart({ series, height = 260, percent = false }: { series: Series[]; height?: number; percent?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = createChart(ref.current, {
      height,
      layout: {
        background: { type: ColorType.Solid, color: "transparent" },
        textColor: css("--mute"),
        fontFamily: "Plus Jakarta Sans, system-ui, sans-serif",
        attributionLogo: true,
      },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line") },
      timeScale: { borderColor: css("--line"), timeVisible: true },
      localization: percent ? { priceFormatter: (p: number) => `${p.toFixed(1)}%` } : undefined,
      autoSize: true,
    });
    chartRef.current = chart;
    for (const s of series) {
      const line = s.area
        ? chart.addAreaSeries({ lineColor: s.color, topColor: `${s.color}55`, bottomColor: `${s.color}05`, lineWidth: 2 })
        : chart.addLineSeries({ color: s.color, lineWidth: 2 });
      line.setData(toData(s.points));
    }
    chart.timeScale().fitContent();
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, [series, height, percent]);

  return (
    <div>
      <div ref={ref} style={{ width: "100%", height }} />
      <div className="legend">
        {series.map((s) => (
          <span key={s.name} style={{ color: s.color }}>
            ━ {s.name}
          </span>
        ))}
      </div>
    </div>
  );
}
