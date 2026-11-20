import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, fmt, type StrategySummary } from "../api";
import { LineChart } from "../components/LineChart";

const IDEAS: Record<string, string> = {
  trend_vol_target:
    "Ride trends, size by risk. Buy when fast moving averages are above slow ones, at three speeds, and size each position so it contributes a fixed amount of volatility. Flat, never short, when the trend is down.",
  xs_momentum:
    "Once a month, hold the few assets that rose most over the past year, but only those that actually rose; the rest of the money sits in cash.",
  mean_reversion:
    "Buy a sharp 2–5 day dip in a broad index, sell on the bounce. Its job here is to show how trading costs and the Belgian transaction tax destroy high-turnover edges.",
  buy_and_hold: "Buy once, never trade. The baseline every active idea has to beat.",
  equal_weight_rebalanced: "Hold every asset in equal parts, rebalancing only when weights drift past the buffer.",
};

function Card({ s }: { s: StrategySummary }) {
  const [open, setOpen] = useState(false);
  const report = useQuery({ queryKey: ["report", s.id, s.sleeve], queryFn: () => api.report(s.id, s.sleeve), enabled: open });
  const h = s.headline;
  return (
    <article className={`panel card ${s.verdict.survives ? "survives" : ""}`}>
      <div className="panel-head">
        <h3>
          {s.id.replaceAll("_", " ")} · <span className="mute">{s.sleeve}</span>
        </h3>
        <span className={`pill ${s.verdict.survives ? "good" : "bad"}`}>{s.verdict.survives ? "survives" : "does not survive"}</span>
      </div>
      <p>{IDEAS[s.id] ?? ""}</p>
      <div className="tiles">
        <div title="Deflated Sharpe Ratio: probability the true Sharpe beats the luck hurdle of all trials tried">
          <span>DSR</span>
          <b>{fmt.num(h.dsr)}</b>
        </div>
        <div title="Walk-forward out-of-sample Sharpe">
          <span>OOS Sharpe</span>
          <b>{fmt.num(h.oos_sharpe)}</b>
        </div>
        <div>
          <span>{h.baseline.replaceAll("_", " ")}</span>
          <b>{fmt.num(h.baseline_oos_sharpe)}</b>
        </div>
        <div title="Probability of Backtest Overfitting (≥ 0.5: selection no better than chance)">
          <span>PBO</span>
          <b>{fmt.num(h.pbo)}</b>
        </div>
        <div>
          <span>max DD</span>
          <b>{fmt.pct(h.oos_max_drawdown)}</b>
        </div>
      </div>
      {s.holdout.contaminated && <p className="bad">Hold-out contaminated: viewed more than once.</p>}
      <button onClick={() => setOpen(!open)}>{open ? "hide details" : "out-of-sample curve, costs, parameters"}</button>
      {open && report.data && (
        <div className="details">
          <LineChart
            series={[
              { name: "strategy (out-of-sample)", points: report.data.curves.oos, color: "#e0a43a" },
              { name: "baseline", points: report.data.curves.baseline, color: "#7aa7d9" },
            ]}
          />
          <h4>Cost sensitivity</h4>
          <div className="bars">
            {report.data.cost_sensitivity.map((c) => (
              <div key={c.multiplier}>
                <span>{c.multiplier}× costs</span>
                <div className="bar">
                  <i className={(c.sharpe ?? 0) < 0 ? "neg" : ""} style={{ width: `${Math.min(100, Math.abs(c.sharpe ?? 0) * 50)}%` }} />
                </div>
                <b>{fmt.num(c.sharpe)}</b>
              </div>
            ))}
          </div>
          {report.data.heatmap.x_key && (
            <>
              <h4>
                Parameter stability ({report.data.heatmap.x_key} × {report.data.heatmap.y_key})
              </h4>
              <Heatmap cells={report.data.heatmap.cells} />
            </>
          )}
        </div>
      )}
    </article>
  );
}

function Heatmap({ cells }: { cells: { x: string; y: string; sharpe: number | null }[] }) {
  const xs = [...new Set(cells.map((c) => c.x))];
  const ys = [...new Set(cells.map((c) => c.y))];
  const vals = cells.map((c) => c.sharpe ?? 0);
  const lo = Math.min(...vals);
  const hi = Math.max(...vals);
  return (
    <div className="scroll">
      <table className="heat">
        <tbody>
          {xs.map((x) => (
            <tr key={x}>
              <th>{x}</th>
              {ys.map((y) => {
                const v = cells.find((c) => c.x === x && c.y === y)?.sharpe ?? null;
                const t = v == null || hi === lo ? 0 : (v - lo) / (hi - lo);
                return (
                  <td key={y} style={{ background: `rgba(224,164,58,${0.1 + 0.6 * t})` }} title={`${y}`}>
                    {fmt.num(v)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Strategies() {
  const { data, isLoading } = useQuery({ queryKey: ["strategies"], queryFn: api.strategies });
  if (isLoading) return <p>loading…</p>;
  const survivors = (data ?? []).filter((s) => s.verdict.survives).length;
  return (
    <>
      <p className="lede">
        Every strategy is judged walk-forward: parameters are picked on past data only, then tested on the next six months,
        repeatedly, with all costs. {survivors} of {data?.length ?? 0} pass all four checks (DSR &gt; 0.95, PBO &lt; 0.5,
        beats its baseline, no lucky parameter spike). Most don't, and saying so is the point.
      </p>
      <div className="grid">
        {(data ?? []).map((s) => (
          <Card key={`${s.id}/${s.sleeve}`} s={s} />
        ))}
      </div>
    </>
  );
}
