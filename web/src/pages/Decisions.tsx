import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  api,
  fmt,
  type AssetTrace,
  type DecisionTrace,
  type Decisions as DecisionsData,
  type Metrics,
  type MomentumAsset,
  type RiskTrace,
  type StrategyTrace,
  type TrendAsset,
} from "../api";
import { LineChart } from "../components/LineChart";
import { Meter } from "../components/Meter";

const SLEEVES = ["crypto", "equities"] as const;
const MODES = ["sim", "sandbox"] as const;
const label = (s: string) => s.replaceAll("_", " ");
const time = (t: string | undefined | null) =>
  t ? new Date(t).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "—";

function RiskPanel({ risk, tripped }: { risk: RiskTrace; tripped: string[] }) {
  const loss = Math.max(0, -risk.daily_pnl.value);
  const dd = Math.max(0, -risk.drawdown.value);
  const age = risk.data.age_seconds;
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Risk limits and kill switches</h3>
        <span className={`pill ${tripped.length ? "bad" : "good"}`}>
          {tripped.length ? `tripped: ${tripped.join(", ")}` : "no kill switch tripped"}
        </span>
      </div>
      <p className="mute">
        How close each limit is to firing. A kill switch halts the sleeve (cancel open orders, alert, hold) until it is reset
        by hand; nothing resets itself.
      </p>
      <div className="meters">
        <Meter
          name="today's P&L"
          used={loss / -(risk.daily_pnl.limit ?? -1)}
          text={fmt.pct(risk.daily_pnl.value, 2)}
          limit={`${fmt.pct(risk.daily_pnl.limit, 1)}${risk.daily_pnl.absolute_limit_set ? " and an absolute cap" : ""}`}
          hint="Measured from the previous close (yesterday's last mark)."
        />
        <Meter
          name="drawdown from peak"
          used={dd / -(risk.drawdown.limit ?? -1)}
          text={fmt.pct(risk.drawdown.value, 2)}
          limit={fmt.pct(risk.drawdown.limit, 0)}
        />
        <Meter
          name="gross exposure"
          used={Math.max(risk.gross.value, risk.gross.after ?? 0) / (risk.gross.cap ?? 1)}
          text={`${fmt.w(risk.gross.value)}${risk.gross.after != null ? ` → ${fmt.w(risk.gross.after)}` : ""}`}
          limit={fmt.w(risk.gross.cap, 0)}
          hint="Now → after this decision's orders."
        />
        {risk.groups.map((g) => (
          <Meter
            key={g.name}
            name={`group: ${label(g.name)}`}
            used={Math.max(g.value, g.after ?? 0) / (g.cap ?? 1)}
            text={`${fmt.w(g.value)}${g.after != null ? ` → ${fmt.w(g.after)}` : ""}`}
            limit={fmt.w(g.cap, 0)}
            hint={g.members.join(", ")}
          />
        ))}
        <Meter
          name="data age"
          used={risk.data.stale_after_seconds ? age / risk.data.stale_after_seconds : null}
          text={fmt.age(age)}
          limit={`stale after ${risk.data.stale_after}`}
          hint={`Latest closed bar: ${time(risk.data.bar_close)}`}
        />
        <Meter
          name="errors (burst)"
          used={risk.errors.recent / risk.errors.limit}
          text={String(risk.errors.recent)}
          limit={`${risk.errors.limit} in ${risk.errors.window_minutes} min`}
        />
      </div>
      <p className="mute small">
        Per order: at most {fmt.w(risk.instrument_cap, 0)} of the sleeve in one asset, orders at most{" "}
        {fmt.w(risk.max_order_pct, 0)} of equity, price within {fmt.w(risk.price_band_pct, 0)} of the last trade.
      </p>
    </section>
  );
}

function Pipeline({ d }: { d: DecisionTrace }) {
  const assets = Object.entries(d.assets ?? {});
  const strategies = (d.strategies ?? []).filter((s) => !s.error);
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>From signal to order</h3>
        <span className="mute">
          {d.n_approved ?? 0} of {d.n_orders ?? 0} proposed orders approved
        </span>
      </div>
      <p className="mute">
        Each asset's target, step by step: what each strategy asks for (scaled by its share of the sleeve), the sum, the
        sleeve's limits applied, then the buffer: trade only when the target is more than {fmt.w(d.buffer_pct, 0)} away from
        what is held. Orders fill at the next bar's open.
      </p>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>asset</th>
              <th>price</th>
              {strategies.length > 1 &&
                strategies.map((s) => (
                  <th key={s.strategy} title={`${fmt.w(s.allocation, 0)} of the sleeve`}>
                    {label(s.strategy)}
                  </th>
                ))}
              <th title="Sum of the strategies' targets">raw target</th>
              <th title="After the per-asset, group and gross caps">after limits</th>
              <th>held</th>
              <th title="after limits − held">gap</th>
              <th>action</th>
              <th>order → risk check</th>
            </tr>
          </thead>
          <tbody>
            {assets.map(([aid, a]) => (
              <PipelineRow key={aid} aid={aid} a={a} strategies={strategies} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function PipelineRow({ aid, a, strategies }: { aid: string; a: AssetTrace; strategies: StrategyTrace[] }) {
  const o = a.order;
  return (
    <tr>
      <td>
        <b>{aid}</b>
      </td>
      <td>{a.price.toLocaleString(undefined, { maximumFractionDigits: 2 })}</td>
      {strategies.length > 1 && strategies.map((s) => <td key={s.strategy}>{fmt.w(a.by_strategy[s.strategy])}</td>)}
      <td>{fmt.w(a.raw)}</td>
      <td title={a.constraint ?? "no limit bound"}>
        {fmt.w(a.constrained)}
        {a.constraint && <sup className="warn-text"> capped</sup>}
      </td>
      <td>{fmt.w(a.current)}</td>
      <td className={a.outside_buffer ? "" : "mute"}>
        {a.gap >= 0 ? "+" : ""}
        {(a.gap * 100).toFixed(1)}pp
      </td>
      <td className={o ? o.side : "mute"}>{a.action}</td>
      <td>
        {o ? (
          <span className={o.approved ? "good-text" : "bad-text"} title={o.reason ?? ""}>
            {o.side} {fmt.w(o.size_pct_of_equity)} of equity {o.approved ? "✓ approved" : `✗ ${o.reason ?? "rejected"}`}
          </span>
        ) : (
          <span className="mute">—</span>
        )}
      </td>
    </tr>
  );
}

function TrendPanel({ s }: { s: StrategyTrace }) {
  const assets = Object.entries((s.assets ?? {}) as Record<string, TrendAsset>);
  const p = s.params as { target_vol_annual: number; forecast_target: number; forecast_cap: number; vol_span_days: number } | undefined;
  const speeds = assets[0]?.[1].speeds ?? [];
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Trend + volatility targeting · {fmt.w(s.allocation, 0)} of the sleeve</h3>
      </div>
      <p className="mute">
        For each speed, the fast moving average minus the slow one, divided by the price's volatility, is rescaled so that its
        typical size is {p?.forecast_target ?? 10} and capped at ±{p?.forecast_cap ?? 20}. The speeds are averaged; a negative
        forecast means flat, never short. Size: weight = forecast⁺ / {p?.forecast_target ?? 10} × ({fmt.w(p?.target_vol_annual, 0)}{" "}
        target vol ÷ asset vol) × instrument weight × IDM, capped at 100%.
      </p>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>asset</th>
              <th title={`EWMA of returns, ${p?.vol_span_days ?? 35}-day span, annualised`}>vol (ann.)</th>
              {speeds.map((sp) => (
                <th key={`${sp.fast_days}/${sp.slow_days}`} title="capped forecast at this speed">
                  {sp.fast_days}/{sp.slow_days}d
                </th>
              ))}
              <th>forecast</th>
              <th title="target vol ÷ asset vol: the weight at an average-strength forecast">vol scalar</th>
              <th>instr. weight</th>
              <th>weight</th>
            </tr>
          </thead>
          <tbody>
            {assets.map(([aid, a]) => (
              <tr key={aid}>
                <td>
                  <b>{aid}</b>
                </td>
                <td>{fmt.w(a.vol_annual)}</td>
                {a.speeds.map((sp) => (
                  <td
                    key={`${sp.fast_days}/${sp.slow_days}`}
                    className={sp.forecast == null ? "mute" : sp.forecast > 0 ? "buy" : "sell"}
                    title={`crossover ${fmt.num(sp.crossover, 3)} × scalar ${fmt.num(sp.scalar, 2)}`}
                  >
                    {fmt.num(sp.forecast, 1)}
                  </td>
                ))}
                <td className={a.forecast_long_only ? "buy" : "mute"} title={`before the long-only floor: ${fmt.num(a.forecast, 2)}`}>
                  {fmt.num(a.forecast, 1)}
                  {a.forecast != null && a.forecast < 0 && " → 0"}
                </td>
                <td>{fmt.num(a.vol_target_weight, 2)}×</td>
                <td title={`IDM ${a.idm}`}>{fmt.w(a.instrument_weight * a.idm, 0)}</td>
                <td>
                  <b>{fmt.w(a.weight)}</b>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function MomentumPanel({ s }: { s: StrategyTrace }) {
  const p = s.params as { lookback_months: number; top_n: number } | undefined;
  const assets = Object.entries((s.assets ?? {}) as Record<string, MomentumAsset>).sort(
    ([, a], [, b]) => (a.rank ?? 99) - (b.rank ?? 99),
  );
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Cross-sectional momentum · {fmt.w(s.allocation, 0)} of the sleeve</h3>
        <span className="mute">
          last rebalance {time(s.rebalanced_at)}
          {s.rebalanced_today ? " (this bar)" : ""}
        </span>
      </div>
      <p className="mute">
        On the first trading day of each month, rank by {p?.lookback_months ?? 12}-month return and keep the top {p?.top_n ?? 3}{" "}
        that actually went up. Survivors are weighted by inverse volatility, and each empty slot stays in cash. Held unchanged
        until the next month.
      </p>
      <div className="scroll">
        <table>
          <thead>
            <tr>
              <th>rank</th>
              <th>asset</th>
              <th>{p?.lookback_months ?? 12}m return</th>
              <th>top {p?.top_n ?? 3}</th>
              <th>rose</th>
              <th>vol (ann.)</th>
              <th>weight</th>
            </tr>
          </thead>
          <tbody>
            {assets.map(([aid, a]) => (
              <tr key={aid} className={a.weight > 0 ? "" : "dim"}>
                <td>{a.rank ?? "—"}</td>
                <td>
                  <b>{aid}</b>
                </td>
                <td className={(a.lookback_return ?? 0) > 0 ? "buy" : "sell"}>{fmt.pct(a.lookback_return)}</td>
                <td>{a.top_n ? "✓" : ""}</td>
                <td>{a.positive ? "✓" : "✗"}</td>
                <td>{fmt.w(a.vol_annual)}</td>
                <td>
                  <b>{fmt.w(a.weight)}</b>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const PALETTE = ["#e0a43a", "#7aa7d9", "#6fbf73", "#c98bd9", "#e06a5a"];

function History({ data }: { data: DecisionsData }) {
  const assets = Object.keys(data.latest_decided?.assets ?? {});
  const [pick, setPick] = useState<string>("");
  const asset = pick || assets[0];
  const series = useMemo(() => {
    if (!asset) return [];
    const decided = data.series.filter((r) => r.status === "decided");
    const pts = (k: "target" | "held") =>
      decided.filter((r) => r[k][asset] != null).map((r) => [r.time, (r[k][asset] as number) * 100] as [string, number]);
    return [
      { name: "target (after limits)", points: pts("target"), color: PALETTE[0] },
      { name: "held", points: pts("held"), color: PALETTE[1], area: true },
    ];
  }, [data, asset]);
  if (!asset) return null;
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Target vs held, recent decisions</h3>
        <select value={asset} onChange={(e) => setPick(e.target.value)}>
          {assets.map((a) => (
            <option key={a}>{a}</option>
          ))}
        </select>
      </div>
      {series[0].points.length > 1 ? (
        <LineChart percent height={220} series={series} />
      ) : (
        <p className="mute">Needs a few decisions of history.</p>
      )}
    </section>
  );
}

function MetricsPanel({ m }: { m: Metrics }) {
  const tiles: [string, string, string?][] = [
    ["total return", fmt.pct(m.total_return, 2)],
    ["CAGR", m.cagr == null ? "—" : fmt.pct(m.cagr, 1), "Only shown after 30 days; annualising less is mostly noise."],
    ["volatility (ann.)", fmt.w(m.vol_annual)],
    ["Sharpe", fmt.num(m.sharpe)],
    ["Sortino", fmt.num(m.sortino)],
    ["max drawdown", fmt.pct(m.max_drawdown, 1)],
    ["drawdown now", fmt.pct(m.current_drawdown, 1)],
    ["avg exposure", fmt.w(m.exposure_avg, 0)],
    ["turnover / yr", m.turnover_annual == null ? "—" : `${m.turnover_annual.toFixed(1)}×`, "Sum of |weight changes|, annualised."],
    ["fills", String(m.n_fills ?? 0)],
    ["best bar", fmt.pct(m.best_bar, 2)],
    ["worst bar", fmt.pct(m.worst_bar, 2)],
  ];
  return (
    <section className="panel">
      <div className="panel-head">
        <h3>Realised so far</h3>
        <span className="mute">
          {m.n_bars} bars{m.days != null ? ` over ${m.days.toFixed(1)} days` : ""}, since {time(m.since)}
        </span>
      </div>
      {m.n_bars < 2 ? (
        <p className="mute">Needs at least two bars of history.</p>
      ) : (
        <>
          <div className="tiles">
            {tiles.map(([k, v, hint]) => (
              <div key={k} title={hint}>
                <span>{k}</span>
                <b>{v}</b>
              </div>
            ))}
          </div>
          {(m.days ?? 0) < 90 && (
            <p className="mute small">
              A few weeks of returns say little about a strategy; the validated, walk-forward numbers are on the Strategies
              page. These are here to check that the bot behaves as designed.
            </p>
          )}
        </>
      )}
    </section>
  );
}

export function Decisions() {
  const [params, setParams] = useSearchParams();
  const sleeve = (params.get("sleeve") as (typeof SLEEVES)[number]) || "crypto";
  const mode = (params.get("mode") as (typeof MODES)[number]) || "sim";
  const set = (k: string, v: string) => setParams((p) => (p.set(k, v), p), { replace: true });

  const dec = useQuery({ queryKey: ["decisions", sleeve, mode], queryFn: () => api.decisions(sleeve, mode), refetchInterval: 60_000 });
  const met = useQuery({ queryKey: ["metrics", sleeve, mode], queryFn: () => api.metrics(sleeve, mode), refetchInterval: 300_000 });
  const latest = dec.data?.latest;
  const decided = dec.data?.latest_decided;

  return (
    <>
      <p className="lede">
        Why the bot holds what it holds: each cycle's full decision, from raw prices through the strategies' signals,
        the sleeve's limits and the trading buffer to the orders and risk checks. Weights are fractions of the sleeve,
        so no balances are shown. New here? <Link to="/how">See how the pipeline works</Link>.
      </p>
      <div className="filters decisions-bar">
        <div className="tabs" role="tablist">
          {SLEEVES.map((s) => (
            <button key={s} role="tab" aria-selected={s === sleeve} className={s === sleeve ? "active" : ""} onClick={() => set("sleeve", s)}>
              {s}
            </button>
          ))}
        </div>
        <select value={mode} onChange={(e) => set("mode", e.target.value)}>
          {MODES.map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
      </div>

      {dec.isLoading && <p className="mute">loading…</p>}
      {dec.error && <p className="bad">{String(dec.error)}</p>}
      {dec.data && !latest && (
        <p className="panel mute">
          No decisions recorded for {mode}/{sleeve} yet. The trace starts with the first cycle after this version was deployed.
        </p>
      )}

      {latest && (
        <section className="panel">
          <div className="panel-head">
            <h2>{sleeve}</h2>
            <span className={`pill ${latest.status === "decided" ? "good" : "bad"}`}>{latest.status}</span>
          </div>
          <div className="tiles">
            <div>
              <span>latest bar</span>
              <b>{time(latest.time)}</b>
            </div>
            <div>
              <span>strategies</span>
              <b>
                {(decided?.strategies ?? []).map((s) => `${label(s.strategy)} ${fmt.w(s.allocation, 0)}`).join(" + ") || "—"}
              </b>
            </div>
            {decided && decided.time !== latest.time && (
              <div>
                <span>last full decision</span>
                <b>{time(decided.time)}</b>
              </div>
            )}
          </div>
          {latest.status !== "decided" && (
            <p className="bad">
              {latest.status === "paused" ? "Paused by an operator" : `Halted by kill switch: ${latest.tripped.join(", ")}`}. No
              new orders until it is {latest.status === "paused" ? "resumed" : "reset by hand"}; the last full decision is shown
              below.
            </p>
          )}
        </section>
      )}

      {latest?.risk && <RiskPanel risk={latest.risk} tripped={latest.tripped} />}
      {decided && <Pipeline d={decided} />}
      {decided?.strategies?.map((s) =>
        s.error ? (
          <p key={s.strategy} className="panel bad">
            {label(s.strategy)}: trace failed ({s.error})
          </p>
        ) : s.kind === "trend_vol_target" ? (
          <TrendPanel key={s.strategy} s={s} />
        ) : s.kind === "xs_momentum" ? (
          <MomentumPanel key={s.strategy} s={s} />
        ) : null,
      )}
      {dec.data && decided && <History data={dec.data} />}
      {met.data && <MetricsPanel m={met.data} />}
    </>
  );
}
