// The "How it works" pipeline: one decision cycle drawn as the loop it
// is, with every stage showing the numbers from the latest real decision
// trace (live/trace.py via /api/decisions). Nothing here recomputes a
// trading decision; the only maths is presentation, and the buffer
// "try it" slider is clearly labelled as a what-if.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import {
  fmt,
  type AssetTrace,
  type DecisionTrace,
  type MomentumAsset,
  type SleeveStatus,
  type StrategyTrace,
  type TrendAsset,
} from "../api";
import { Meter } from "./Meter";

const REPO = "https://github.com/magai2002/kestrel/blob/main";
const label = (s: string) => s.replaceAll("_", " ");
const when = (t: string | null | undefined) =>
  t ? new Date(t).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit", month: "short", day: "numeric" }) : "—";
const price = (x: number | null | undefined) => (x == null ? "—" : x.toLocaleString(undefined, { maximumFractionDigits: 2 }));
const pp = (x: number) => `${x >= 0 ? "+" : "−"}${Math.abs(x * 100).toFixed(1)}pp`;

export function until(iso: string | null | undefined, now: number): string {
  if (!iso) return "—";
  const s = Math.round((new Date(iso).getTime() - now) / 1000);
  if (s <= 0) return "due now";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${Math.max(1, m)}m`;
}

const SLEEVE_FACTS = {
  crypto: {
    bar: "4-hour",
    source: "Kraken's 4-hour bars (older history backfilled from Binance)",
    delay: "30 seconds",
    strategies: "trend + volatility targeting (100% of the sleeve)",
  },
  equities: {
    bar: "daily",
    source: "daily bars of the UCITS ETFs on the London Stock Exchange (Yahoo for now)",
    delay: "15 minutes",
    strategies: "trend + volatility targeting (70%) and cross-sectional momentum (30%)",
  },
} as const;

const KILL_SWITCHES: [string, string][] = [
  ["daily_loss", "the sleeve lost more than its daily limit since yesterday's close"],
  ["drawdown", "equity fell more than the limit from its peak"],
  ["stale_data", "no fresh bar for too long: never trade on old prices"],
  ["reconcile_mismatch", "the venue holds something our ledger doesn't (or vice versa)"],
  ["error_burst", "too many errors in a short window"],
  ["clock_drift", "the server clock disagrees with the venue's"],
];

export interface Ctx {
  sleeve: "crypto" | "equities";
  asset: string;
  decided: DecisionTrace | null;
  latest: DecisionTrace | null;
  a: AssetTrace | null;
  status: SleeveStatus | undefined;
  now: number;
}

interface Stage {
  id: string;
  title: string;
  icon: string;
  chip: (c: Ctx) => string;
  what: string;
  why: ReactNode;
  code: [string, string][];
  Live: (p: { c: Ctx }) => ReactNode;
}

// --- per-stage live views ---------------------------------------------------

function NoTrace() {
  return <p className="mute">No decision recorded for this sleeve yet; the numbers appear after its first cycle.</p>;
}

function BarLive({ c }: { c: Ctx }) {
  const r = c.latest?.risk;
  const f = SLEEVE_FACTS[c.sleeve];
  return (
    <>
      <div className="tiles">
        <Tile k={`${c.asset} close`} v={price(c.a?.price)} />
        <Tile k="bar closed" v={when(c.latest?.time)} />
        <Tile k="next decision" v={until(c.status?.next_decision, c.now)} hint={when(c.status?.next_decision)} />
      </div>
      {r && (
        <div className="meters">
          <Meter
            name="data age"
            used={r.data.stale_after_seconds ? (c.now / 1000 - new Date(r.data.bar_close).getTime() / 1000) / r.data.stale_after_seconds : null}
            text={fmt.age(c.now / 1000 - new Date(r.data.bar_close).getTime() / 1000)}
            limit={`stale after ${r.data.stale_after}`}
          />
        </div>
      )}
      <p className="mute small">
        Source: {f.source}. The bot wakes {f.delay} after each {f.bar} close so the venue has published the final bar, then
        checks the bar it expected is really there.
      </p>
    </>
  );
}

function SettleLive({ c }: { c: Ctx }) {
  const rec = c.status?.last_reconciliation;
  return (
    <>
      <Timeline sleeve={c.sleeve} />
      <div className="tiles">
        <Tile k={`${c.asset} held now`} v={fmt.w(c.a?.current)} hint="Share of the sleeve's equity, after this bar's fills" />
        <Tile
          k="reconciliation"
          v={c.status?.mode === "sim" ? "n/a in sim" : rec ? (rec.ok ? "✓ matches" : "✗ mismatch") : "—"}
          hint={c.status?.mode === "sim" ? "In sim the ledger is the venue: nothing external to compare with." : undefined}
        />
      </div>
    </>
  );
}

function KillLive({ c }: { c: Ctx }) {
  const ks = c.status?.kill_switches ?? {};
  const r = c.latest?.risk;
  return (
    <>
      <ul className="switches">
        {KILL_SWITCHES.map(([name, text]) => {
          const on = ks[name]?.tripped;
          return (
            <li key={name} className={on ? "on" : ""}>
              <i aria-hidden="true" />
              <b>{label(name)}</b>
              <span className="mute">{text}</span>
              <em>{on ? "TRIPPED" : "ok"}</em>
            </li>
          );
        })}
      </ul>
      {r && (
        <div className="meters">
          <Meter
            name="today's P&L"
            used={Math.max(0, -r.daily_pnl.value) / -(r.daily_pnl.limit ?? -1)}
            text={fmt.pct(r.daily_pnl.value, 2)}
            limit={fmt.pct(r.daily_pnl.limit, 1)}
          />
          <Meter
            name="drawdown"
            used={Math.max(0, -r.drawdown.value) / -(r.drawdown.limit ?? -1)}
            text={fmt.pct(r.drawdown.value, 2)}
            limit={fmt.pct(r.drawdown.limit, 0)}
          />
          <Meter name="errors" used={r.errors.recent / r.errors.limit} text={String(r.errors.recent)} limit={`${r.errors.limit} in ${r.errors.window_minutes} min`} />
        </div>
      )}
    </>
  );
}

function ForecastBars({ t }: { t: TrendAsset }) {
  const rows: [string, number | null][] = [
    ...t.speeds.map((s) => [`${s.fast_days}/${s.slow_days} days`, s.forecast] as [string, number | null]),
    ["combined", t.forecast],
  ];
  return (
    <div className="fbars" role="img" aria-label="Trend forecast at each speed, from −20 to +20">
      {rows.map(([k, f]) => (
        <div key={k} className={k === "combined" ? "total" : ""}>
          <span>{k}</span>
          <div className="fbar">
            <i
              className={f == null ? "" : f >= 0 ? "pos" : "neg"}
              style={f == null ? {} : { width: `${(Math.min(20, Math.abs(f)) / 20) * 50}%`, [f >= 0 ? "left" : "right"]: "50%" }}
            />
          </div>
          <b className={f == null ? "mute" : f >= 0 ? "good-text" : "bad-text"}>{fmt.num(f, 1)}</b>
        </div>
      ))}
      <div className="fscale mute">
        <span>−20 (strong downtrend)</span>
        <span>0</span>
        <span>+20</span>
      </div>
    </div>
  );
}

function TrendMath({ t, s }: { t: TrendAsset; s: StrategyTrace }) {
  const p = s.params as { forecast_target?: number; target_vol_annual?: number } | undefined;
  const ft = p?.forecast_target ?? 10;
  return (
    <p className="formula">
      weight = max(0, <b>{fmt.num(t.forecast, 1)}</b>) ÷ {ft} × (<b>{fmt.w(p?.target_vol_annual, 0)}</b> target vol ÷{" "}
      <b>{fmt.w(t.vol_annual)}</b> asset vol) × <b>{fmt.w(t.instrument_weight, 0)}</b> instrument weight × IDM{" "}
      {fmt.num(t.idm, 2)} = <b className="gold">{fmt.w(t.weight)}</b>
      {t.forecast != null && t.forecast < 0 && <span className="mute"> (negative forecast: flat, never short)</span>}
    </p>
  );
}

function MomentumRank({ s, asset }: { s: StrategyTrace; asset: string }) {
  const p = s.params as { lookback_months?: number; top_n?: number } | undefined;
  const rows = Object.entries((s.assets ?? {}) as Record<string, MomentumAsset>).sort(([, a], [, b]) => (a.rank ?? 99) - (b.rank ?? 99));
  const max = Math.max(0.01, ...rows.map(([, a]) => Math.abs(a.lookback_return ?? 0)));
  return (
    <>
      <p className="mute small">
        Ranked by {p?.lookback_months ?? 12}-month return on the first trading day of the month; the top {p?.top_n ?? 3} that
        actually rose are held, weighted by inverse volatility. Last rebalance {when(s.rebalanced_at)}.
      </p>
      <div className="rank">
        {rows.map(([aid, a]) => (
          <div key={aid} className={`${aid === asset ? "me" : ""} ${a.weight > 0 ? "held" : ""}`}>
            <span>
              {a.rank ?? "—"}. {aid}
            </span>
            <div className="fbar">
              <i
                className={(a.lookback_return ?? 0) >= 0 ? "pos" : "neg"}
                style={{ width: `${(Math.abs(a.lookback_return ?? 0) / max) * 50}%`, [(a.lookback_return ?? 0) >= 0 ? "left" : "right"]: "50%" }}
              />
            </div>
            <b>{a.weight > 0 ? fmt.w(a.weight) : <span className="mute">—</span>}</b>
          </div>
        ))}
      </div>
    </>
  );
}

function StrategyLive({ c }: { c: Ctx }) {
  if (!c.a || !c.decided) return <NoTrace />;
  const strategies = (c.decided.strategies ?? []).filter((s) => !s.error);
  return (
    <>
      {strategies.map((s) => {
        const mine = s.assets?.[c.asset];
        return (
          <div key={s.strategy} className="substage">
            <h4>
              {label(s.strategy)} · {fmt.w(s.allocation, 0)} of the sleeve
            </h4>
            {s.kind === "trend_vol_target" && mine ? (
              <>
                <ForecastBars t={mine as TrendAsset} />
                <TrendMath t={mine as TrendAsset} s={s} />
              </>
            ) : s.kind === "xs_momentum" ? (
              <MomentumRank s={s} asset={c.asset} />
            ) : (
              <p>wants {fmt.w(s.weights?.[c.asset])}</p>
            )}
          </div>
        );
      })}
      {strategies.length > 1 && (
        <p className="formula">
          {strategies.map((s, i) => (
            <span key={s.strategy}>
              {i > 0 && " + "}
              {fmt.w(s.allocation, 0)} × {fmt.w(s.weights?.[c.asset])} <span className="mute">({label(s.strategy)})</span>
            </span>
          ))}{" "}
          = <b className="gold">{fmt.w(c.a.raw)}</b> raw target
        </p>
      )}
    </>
  );
}

function Gauge({ marks, cap, band }: { marks: { at: number; label: string; tone: string }[]; cap?: { at: number; label: string }; band?: [number, number] }) {
  const x = (v: number) => `${Math.max(0, Math.min(1, v)) * 100}%`;
  return (
    <div className="gauge" aria-hidden="true">
      <div className="track">
        {band && <div className="band" style={{ left: x(band[0]), width: `calc(${x(band[1])} - ${x(band[0])})` }} />}
        {cap && (
          <div className="cap" style={{ left: x(cap.at) }}>
            <span>{cap.label}</span>
          </div>
        )}
        {marks.map((m, i) => (
          <div key={m.label} className={`mark ${m.tone}`} style={{ left: x(m.at) }}>
            <span className={i % 2 ? "below" : ""}>{m.label}</span>
          </div>
        ))}
      </div>
      <div className="ticks mute">
        <span>0%</span>
        <span>50%</span>
        <span>100%</span>
      </div>
    </div>
  );
}

function LimitsLive({ c }: { c: Ctx }) {
  const r = c.latest?.risk ?? c.decided?.risk;
  if (!c.a || !r) return <NoTrace />;
  const group = r.groups.find((g) => g.members.includes(c.asset));
  return (
    <>
      <Gauge
        cap={{ at: r.instrument_cap, label: `cap ${fmt.w(r.instrument_cap, 0)}` }}
        marks={[
          { at: c.a.raw, label: `asked ${fmt.w(c.a.raw)}`, tone: "hollow" },
          { at: c.a.constrained, label: `allowed ${fmt.w(c.a.constrained)}`, tone: "gold" },
        ]}
      />
      <p>
        {c.a.constraint ? (
          <>
            Bound by the <b>{c.a.constraint}</b>: {fmt.w(c.a.raw)} → {fmt.w(c.a.constrained)}.
          </>
        ) : (
          <>No limit binds {c.asset} right now: the target passes through at {fmt.w(c.a.constrained)}.</>
        )}
      </p>
      <ul className="mute small">
        <li>one asset: at most {fmt.w(r.instrument_cap, 0)} of the sleeve</li>
        {group && (
          <li>
            group {label(group.name)} ({group.members.join(", ")}): at most {fmt.w(group.cap, 0)} together; now {fmt.w(group.value)}
          </li>
        )}
        <li>
          everything together (gross): at most {fmt.w(r.gross.cap, 0)}; now {fmt.w(r.gross.value)}
        </li>
      </ul>
    </>
  );
}

function BufferLive({ c }: { c: Ctx }) {
  const b = c.decided?.buffer_pct ?? 0.1;
  const live = c.a?.constrained ?? 0;
  const held = c.a?.current ?? 0;
  const [tryT, setTryT] = useState<number | null>(null);
  useEffect(() => setTryT(null), [c.asset, c.sleeve]);
  if (!c.a) return <NoTrace />;
  const t = tryT ?? live;
  const trades = Math.abs(t - held) > b;
  return (
    <>
      <Gauge
        band={[held - b, held + b]}
        marks={[
          { at: held, label: `held ${fmt.w(held)}`, tone: "blue" },
          { at: t, label: `${tryT == null ? "target" : "what if"} ${fmt.w(t)}`, tone: tryT == null ? "gold" : "hollow" },
        ]}
      />
      <p>
        The shaded band is ±{fmt.w(b, 0)} around what is held. Gap {pp(t - held)}:{" "}
        {trades ? <b className="good-text">outside the band → trade to {fmt.w(t)}</b> : <b>inside the band → hold, no order</b>}.
        {tryT == null && c.a.trimmed_to_fit_limits && <> Held positions also get trimmed if price drift pushed them over a limit.</>}
      </p>
      <label className="try">
        <span>
          Try it: drag a different target <span className="mute">(changes this picture only, not the bot)</span>
        </span>
        <input type="range" min={0} max={100} step={0.5} value={t * 100} onChange={(e) => setTryT(Number(e.target.value) / 100)} />
      </label>
      {tryT != null && (
        <button className="link" onClick={() => setTryT(null)}>
          back to the real target ({fmt.w(live)})
        </button>
      )}
    </>
  );
}

function OrdersLive({ c }: { c: Ctx }) {
  if (!c.a || !c.decided) return <NoTrace />;
  const o = c.a.order;
  return (
    <>
      <div className="tiles">
        <Tile k="action" v={c.a.action} />
        <Tile k="order" v={o ? `${o.side} ${fmt.w(o.size_pct_of_equity)} of equity` : "none"} />
        <Tile k="whole sleeve" v={`${c.decided.n_orders ?? 0} order${c.decided.n_orders === 1 ? "" : "s"}`} />
      </div>
      <p className="mute small">
        Only the difference between the new aim and what is held is traded, rounded to the venue's lot size in exact decimals.
        Each order gets a deterministic id (a UUIDv5 of sleeve, bar and asset), so a crash and restart looks the order up
        instead of sending it twice.
      </p>
    </>
  );
}

function RiskLive({ c }: { c: Ctx }) {
  const r = c.latest?.risk ?? c.decided?.risk;
  if (!c.a || !r) return <NoTrace />;
  const o = c.a.order;
  const checks = [
    `${c.asset} stays under ${fmt.w(r.instrument_cap, 0)} of the sleeve`,
    ...r.groups.filter((g) => g.members.includes(c.asset)).map((g) => `group ${label(g.name)} stays under ${fmt.w(g.cap, 0)}`),
    `total exposure stays under ${fmt.w(r.gross.cap, 0)}`,
    `order no bigger than ${fmt.w(r.max_order_pct, 0)} of equity (fat-finger guard)`,
    `limit price within ${fmt.w(r.price_band_pct, 0)} of the last price`,
    "above the venue's minimum order size",
  ];
  return (
    <>
      <p>
        {o ? (
          o.approved ? (
            <b className="good-text">✓ approved: every check passed</b>
          ) : (
            <b className="bad-text">✗ rejected: {o.reason}</b>
          )
        ) : (
          <span className="mute">No order for {c.asset} this cycle, so nothing to check.</span>
        )}
      </p>
      <ul className={`checks ${o ? (o.approved ? "pass" : "fail") : "idle"}`}>
        {checks.map((t) => (
          <li key={t}>{t}</li>
        ))}
      </ul>
      <p className="mute small">
        First breach wins and the order is dropped, never resized. Exposure checks never block a sell: selling only reduces risk.
      </p>
    </>
  );
}

const MODES: [string, string, string, string][] = [
  ["backtest", "replayed bar by bar", "stored history", "simulated fills"],
  ["sim", "real time", "real prices", "simulated fills, same cost model"],
  ["sandbox", "real time", "venue test data", "Binance testnet / IBKR paper account"],
  ["live", "real time", "real prices", "Kraken / IBKR: locked behind the go-live checks"],
];

function ExecuteLive({ c }: { c: Ctx }) {
  const mode = c.status?.mode ?? "sim";
  return (
    <>
      <p>
        {c.a?.order ? (
          <>
            The {c.a.order.side} order for {c.asset} fills at the next bar's opening price, booked at the next cycle (
            {until(c.status?.next_decision, c.now)}).
          </>
        ) : (
          <>Nothing queued for {c.asset}. Next cycle in {until(c.status?.next_decision, c.now)}.</>
        )}
      </p>
      <div className="scroll">
        <table className="modes">
          <thead>
            <tr>
              <th>mode</th>
              <th>clock</th>
              <th>prices</th>
              <th>broker</th>
            </tr>
          </thead>
          <tbody>
            {MODES.map(([m, clock, data, broker]) => (
              <tr key={m} className={m === mode ? "current" : ""}>
                <td>
                  <b>{m}</b>
                  {m === mode && <span className="pill good">running</span>}
                </td>
                <td>{clock}</td>
                <td>{data}</td>
                <td>{broker}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mute small">
        Everything before this step is the same code in all four modes; only the clock, the price feed and the broker are
        swapped. A test replays the paper loop bar by bar and checks it reproduces the backtest exactly.
      </p>
    </>
  );
}

function Tile({ k, v, hint }: { k: string; v: ReactNode; hint?: string }) {
  return (
    <div title={hint}>
      <span>{k}</span>
      <b>{v}</b>
    </div>
  );
}

/** Bar t's decision fills at bar t+1's open: never in the same step. */
function Timeline({ sleeve }: { sleeve: "crypto" | "equities" }) {
  const f = SLEEVE_FACTS[sleeve];
  return (
    <ol className="timeline" aria-label="When a decision's order fills">
      <li>
        <b>bar t closes</b>
        <span>decide {f.delay} later</span>
      </li>
      <li>
        <b>bar t+1 opens</b>
        <span>the order's fill price</span>
      </li>
      <li className="now">
        <b>bar t+1 closes</b>
        <span>fill booked here, then the next decision</span>
      </li>
    </ol>
  );
}

// --- the stages ---------------------------------------------------------------

export const STAGES: Stage[] = [
  {
    id: "bar",
    title: "New bar",
    icon: "◷",
    chip: (c) => (c.a ? `${price(c.a.price)} · next ${until(c.status?.next_decision, c.now)}` : "waiting"),
    what: "A bar closes and the bot wakes up. It only ever sees finished bars, never one still trading.",
    why: "Deciding on an unfinished bar is the classic way a backtest cheats. Waiting for the final bar costs a few seconds and removes that whole class of bug.",
    code: [
      ["live/schedule.py", "when bars close (exchange calendars for ETFs)"],
      ["live/feeds.py", "price feeds per mode"],
    ],
    Live: BarLive,
  },
  {
    id: "settle",
    title: "Settle",
    icon: "⇄",
    chip: (c) => (c.a ? `held ${fmt.w(c.a.current)}` : "—"),
    what: "Orders from the previous decision fill now, at this bar's opening price. Then the ledger is checked against the venue.",
    why: "A decision and the fill it causes never happen in the same step. Getting that wrong is the easiest way to sneak tomorrow's price into today's trade.",
    code: [
      ["live/sleeve_runner.py", "one cycle, start to finish"],
      ["live/reconcile.py", "ledger vs venue"],
      ["execution/fills.py", "simulated fills: spread, slippage, fees"],
    ],
    Live: SettleLive,
  },
  {
    id: "kill",
    title: "Kill switches",
    icon: "⏻",
    chip: (c) => {
      const n = Object.values(c.status?.kill_switches ?? {}).filter((k) => k.tripped).length;
      return n ? `${n} tripped: halted` : "0 of 6 tripped";
    },
    what: "Equity is marked to the latest close and six independent switches are checked. If any trips: cancel open orders, alert, and hold.",
    why: "They never reset themselves. Daily loss is measured from yesterday's close, so it can fire on daily bars too. A human has to look and reset by hand.",
    code: [
      ["risk/kill_switch.py", "the six checks"],
      ["docs/adr/0005-kill-switch-hold-vs-flatten.md", "why hold, not sell everything"],
    ],
    Live: KillLive,
  },
  {
    id: "strategy",
    title: "Strategy",
    icon: "∿",
    chip: (c) => (c.a ? `wants ${fmt.w(c.a.raw)}` : "—"),
    what: "Closed prices in, target weights out. A strategy never sees holdings or cash and never places an order.",
    why: "Each strategy has one vectorised implementation used for research and one bar-by-bar implementation used live; tests check they agree on every bar.",
    code: [
      ["strategies/trend_vol_target.py", "trend + volatility targeting"],
      ["strategies/xs_momentum.py", "cross-sectional momentum"],
      ["strategies/overlay.py", "combining strategies by allocation"],
    ],
    Live: StrategyLive,
  },
  {
    id: "limits",
    title: "Limits",
    icon: "⊓",
    chip: (c) => (c.a ? (c.a.constraint ? `capped → ${fmt.w(c.a.constrained)}` : `${fmt.w(c.a.constrained)} · no cap hit`) : "—"),
    what: "Targets are clipped to the per-asset cap, then groups and the whole sleeve are scaled down to fit their caps.",
    why: "Limits only ever scale down, never up. The pre-trade risk check later is a last-line guard: if it ever rejects a cap breach, this step was skipped.",
    code: [["portfolio/constraints.py", "clip, group caps, gross cap"]],
    Live: LimitsLive,
  },
  {
    id: "buffer",
    title: "Buffer",
    icon: "⇹",
    chip: (c) => (c.a ? `gap ${pp(c.a.gap)} → ${c.a.outside_buffer ? "trade" : "hold"}` : "—"),
    what: "Trade only if the target is more than 10 points away from what is held; otherwise keep the exact held quantity.",
    why: "Small changes cost fees and spread for almost no benefit. Without the band the bot would churn every bar chasing noise.",
    code: [
      ["portfolio/sizing.py", "should_trade: the band"],
      ["portfolio/rebalance.py", "fit_to_limits: trimming drifted positions"],
    ],
    Live: BufferLive,
  },
  {
    id: "orders",
    title: "Orders",
    icon: "≡",
    chip: (c) => (c.a?.order ? `${c.a.order.side} ${fmt.w(c.a.order.size_pct_of_equity)}` : c.a ? "no order" : "—"),
    what: "Weights become exact quantities, rounded to the venue's lot size, and only the difference from holdings becomes an order.",
    why: "Money is Decimal, never floating point, from this step on. Deterministic order ids make a restart safe: the bot asks the venue before resending.",
    code: [
      ["portfolio/construction.py", "weight → Decimal quantity"],
      ["execution/order_manager.py", "diff-based orders, deterministic ids"],
    ],
    Live: OrdersLive,
  },
  {
    id: "risk",
    title: "Risk check",
    icon: "✓",
    chip: (c) => (c.a?.order ? (c.a.order.approved ? "approved" : "rejected") : c.a ? "nothing to check" : "—"),
    what: "Every order passes per-order checks: weight caps, size, price band and venue minimums. The first breach rejects it.",
    why: "Defence in depth: the limits step should already guarantee the caps, so this guard catching something means a bug upstream, and it stops the order.",
    code: [["risk/pre_trade.py", "per-order checks"]],
    Live: RiskLive,
  },
  {
    id: "execute",
    title: "Execute",
    icon: "➜",
    chip: (c) => (c.a?.order ? "fills next open" : `next in ${until(c.status?.next_decision, c.now)}`),
    what: "Approved orders go to the broker. In paper mode they fill at the next bar's open with the full cost model; then the loop starts again.",
    why: "On a real venue: a marketable limit order, a timeout, at most two re-quotes, and a journal entry written before every network call.",
    code: [
      ["execution/live_execution.py", "marketable limit, timeout, re-quote"],
      ["execution/brokers/sim.py", "the paper broker"],
      ["live/gates.py", "the go-live checks"],
    ],
    Live: ExecuteLive,
  },
];

// --- the diagram ----------------------------------------------------------------

interface Layout {
  w: number;
  h: number;
  nodeW: number;
  nodeH: number;
  nodes: [number, number][];
  path: string;
  loopLabel: [number, number];
}

const WIDE: Layout = {
  w: 1000,
  h: 400,
  nodeW: 176,
  nodeH: 92,
  nodes: [
    [110, 85],
    [305, 85],
    [500, 85],
    [695, 85],
    [890, 85],
    [890, 315],
    [695, 315],
    [500, 315],
    [305, 315],
  ],
  path: "M110,85 L890,85 C985,85 985,315 890,315 L160,315 C25,315 25,85 110,85",
  loopLabel: [118, 215],
};

const NARROW: Layout = {
  w: 400,
  h: 1080,
  nodeW: 176,
  nodeH: 120,
  nodes: [
    [104, 110],
    [104, 300],
    [104, 490],
    [104, 680],
    [104, 870],
    [296, 870],
    [296, 680],
    [296, 490],
    [296, 300],
  ],
  path: "M104,110 L104,870 C104,1010 296,1010 296,870 L296,170 C296,10 104,10 104,110",
  loopLabel: [296, 130],
};

function useNarrow(): boolean {
  const q = "(max-width: 680px)";
  const [narrow, setNarrow] = useState(() => typeof window !== "undefined" && window.matchMedia(q).matches);
  useEffect(() => {
    const m = window.matchMedia(q);
    const on = () => setNarrow(m.matches);
    m.addEventListener("change", on);
    return () => m.removeEventListener("change", on);
  }, []);
  return narrow;
}

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function Diagram({ c, active, onPick, halted }: { c: Ctx; active: number; onPick: (i: number) => void; halted: boolean }) {
  const L = useNarrow() ? NARROW : WIDE;
  const pathRef = useRef<SVGPathElement>(null);
  const dotRef = useRef<SVGCircleElement>(null);
  const [geo, setGeo] = useState<{ total: number; at: number[]; arrows: { x: number; y: number; deg: number }[] } | null>(null);
  const pos = useRef(0);

  // Where each node sits along the loop, and arrowheads between them.
  useLayoutEffect(() => {
    const p = pathRef.current;
    if (!p || typeof p.getTotalLength !== "function") return;
    const total = p.getTotalLength();
    const samples = Array.from({ length: 600 }, (_, i) => {
      const d = (i / 600) * total;
      const pt = p.getPointAtLength(d);
      return { d, x: pt.x, y: pt.y };
    });
    const at = L.nodes.map(([x, y]) => samples.reduce((best, s) => (Math.hypot(s.x - x, s.y - y) < Math.hypot(best.x - x, best.y - y) ? s : best)).d);
    at[0] = 0;
    const arrows = at.map((d, i) => {
      const next = i + 1 < at.length ? at[i + 1] : total;
      const mid = (d + next) / 2;
      const a = p.getPointAtLength(mid);
      const b = p.getPointAtLength(Math.min(total, mid + 1));
      return { x: a.x, y: a.y, deg: (Math.atan2(b.y - a.y, b.x - a.x) * 180) / Math.PI };
    });
    setGeo({ total, at, arrows });
    pos.current = at[active] ?? 0;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [L]);

  // Move the "bar" token forward along the loop to the active stage.
  useEffect(() => {
    const p = pathRef.current;
    const dot = dotRef.current;
    if (!geo || !p || !dot) return;
    const from = pos.current;
    let to = geo.at[active];
    if (to < from - 0.5) to += geo.total; // always forward: it's a loop
    const place = (d: number) => {
      const pt = p.getPointAtLength(d % geo.total);
      dot.setAttribute("cx", String(pt.x));
      dot.setAttribute("cy", String(pt.y));
    };
    if (reducedMotion() || Math.abs(to - from) < 1) {
      place(to);
      pos.current = to % geo.total;
      return;
    }
    const ms = Math.min(1400, 250 + (to - from) * 1.1);
    const t0 = performance.now();
    let raf = 0;
    const step = (t: number) => {
      const k = Math.min(1, (t - t0) / ms);
      const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      const d = from + (to - from) * e;
      place(d);
      pos.current = d % geo.total;
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [active, geo]);

  return (
    <div className={`diagram ${L === NARROW ? "narrow" : "wide"}`} style={{ aspectRatio: `${L.w} / ${L.h}` }}>
      <svg viewBox={`0 0 ${L.w} ${L.h}`} aria-hidden="true">
        <path ref={pathRef} d={L.path} className="loop" />
        <path d={L.path} className="loop-flow" />
        {geo?.arrows.map((a, i) => (
          <path key={i} d="M-7,-6 L3,0 L-7,6" className="chev" transform={`translate(${a.x} ${a.y}) rotate(${a.deg})`} />
        ))}
        <text x={L.loopLabel[0]} y={L.loopLabel[1]} className="loop-label" textAnchor="middle">
          <tspan x={L.loopLabel[0]} dy="0">
            ↺ next bar
          </tspan>
          <tspan x={L.loopLabel[0]} dy="1.25em">
            {SLEEVE_FACTS[c.sleeve].bar}
          </tspan>
        </text>
        <circle ref={dotRef} r="9" className="token" cx={L.nodes[0][0]} cy={L.nodes[0][1]} />
      </svg>
      {STAGES.map((s, i) => {
        const [x, y] = L.nodes[i];
        const dim = halted && i > 2;
        return (
          <button
            key={s.id}
            className={`node ${i === active ? "active" : ""} ${dim ? "dim" : ""} ${s.id === "kill" && halted ? "alarm" : ""}`}
            style={{
              left: `${(x / L.w) * 100}%`,
              top: `${(y / L.h) * 100}%`,
              width: `${(L.nodeW / L.w) * 100}%`,
              minHeight: `${(L.nodeH / L.h) * 100}%`,
            }}
            onClick={() => onPick(i)}
            aria-pressed={i === active}
            aria-label={`Step ${i + 1}: ${s.title}. ${s.chip(c)}`}
          >
            <span className="n">
              {i + 1} <span aria-hidden="true">{s.icon}</span>
            </span>
            <b>{s.title}</b>
            <small>{s.chip(c)}</small>
          </button>
        );
      })}
    </div>
  );
}

export function PipelineExplorer({ c }: { c: Ctx }) {
  const [active, setActive] = useState(0);
  const [playing, setPlaying] = useState(false);
  const halted = c.latest != null && c.latest.status !== "decided";
  const go = useCallback((i: number) => setActive((i + STAGES.length) % STAGES.length), []);

  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => {
      setActive((i) => {
        if (i + 1 >= STAGES.length) {
          setPlaying(false);
          return i;
        }
        return i + 1;
      });
    }, 3200);
    return () => window.clearInterval(id);
  }, [playing]);

  const s = STAGES[active];
  const Live = s.Live;
  const key = useMemo(() => `${c.sleeve}/${c.asset}`, [c.sleeve, c.asset]);

  return (
    <div
      className="explorer"
      onKeyDown={(e) => {
        if (e.target instanceof HTMLInputElement) return;
        if (e.key === "ArrowRight" || e.key === "ArrowDown") (e.preventDefault(), go(active + 1));
        if (e.key === "ArrowLeft" || e.key === "ArrowUp") (e.preventDefault(), go(active - 1));
      }}
    >
      <div className="controls">
        <button
          onClick={() => {
            if (!playing && active === STAGES.length - 1) setActive(0);
            setPlaying(!playing);
          }}
          aria-pressed={playing}
        >
          {playing ? "❚❚ pause" : active === 0 ? "▶ walk through one cycle" : "▶ continue"}
        </button>
        <button onClick={() => (setPlaying(false), go(active - 1))} aria-label="previous step">
          ←
        </button>
        <button onClick={() => (setPlaying(false), go(active + 1))} aria-label="next step">
          →
        </button>
        <span className="mute small">or click any step · arrow keys work too</span>
      </div>
      {halted && (
        <p className="halt-note">
          This sleeve is halted ({c.latest?.tripped.join(", ") || c.latest?.status}): the cycle stops at the kill switches. Steps
          4–9 show the last full decision.
        </p>
      )}
      <Diagram c={c} active={active} onPick={(i) => (setPlaying(false), go(i))} halted={halted} />

      <section className="stage panel" aria-live="polite" key={`${s.id}/${key}`}>
        <div className="panel-head">
          <h3>
            <span className="mute">
              {active + 1}/{STAGES.length}
            </span>{" "}
            {s.title}
          </h3>
          <span className="mute small">
            following <b>{c.asset}</b> in {c.sleeve}
          </span>
        </div>
        <p className="what">{s.what}</p>
        <div className="live">
          <div className="live-tag">live · decision of {when(c.decided?.time)}</div>
          <Live c={c} />
        </div>
        <details className="why">
          <summary>Why it's built this way</summary>
          <p>{s.why}</p>
          <ul className="small">
            {s.code.map(([file, what]) => (
              <li key={file}>
                <a href={`${REPO}/${file}`}>
                  <code>{file}</code>
                </a>{" "}
                <span className="mute">{what}</span>
              </li>
            ))}
          </ul>
        </details>
        {s.id === "execute" && (
          <p className="small">
            See every cycle's numbers for all assets on the <Link to={`/decisions?sleeve=${c.sleeve}`}>Decisions page</Link>.
          </p>
        )}
      </section>
    </div>
  );
}
