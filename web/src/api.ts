// Typed fetchers for the public, read-only API (api/app.py). Every number
// here is shareable by design: equity indexed to 100, weights in %, bps.

export type Point = [string, number];

export interface SleeveStatus {
  mode: string;
  sleeve: string;
  heartbeat: string | null;
  last_bar_close: string | null;
  data_age_seconds: number | null;
  paused: boolean;
  kill_switches: Record<string, { tripped: boolean; since: string | null }>;
  last_reconciliation: { at: string; ok: boolean } | null;
}

export interface Status {
  as_of: string;
  sleeves: SleeveStatus[];
  disclaimer: string;
}

export interface Equity {
  sleeve: string;
  mode: string;
  equity_index: Point[];
  drawdown_pct: Point[];
}

export interface Positions {
  sleeve: string;
  mode: string;
  as_of: string;
  weights_pct: Record<string, number>;
}

export interface Trade {
  time: string;
  sleeve: string;
  asset: string;
  side: "buy" | "sell";
  decision_price: number | null;
  fill_price: number;
  slippage_bps: number | null;
  fee_bps: number | null;
  shortfall_bps: number | null;
}

export interface StrategySummary {
  id: string;
  sleeve: string;
  run_id: string;
  generated_at: string;
  headline: {
    dsr: number | null;
    oos_sharpe: number | null;
    oos_cagr: number | null;
    oos_max_drawdown: number | null;
    baseline: string;
    baseline_oos_sharpe: number | null;
    pbo: number | null;
    sharpe_ci: [number | null, number | null];
    n_trials: number;
    sharp_peak: boolean;
  };
  verdict: { survives: boolean; checks: Record<string, boolean> };
  oos: Record<string, number | null>;
  holdout: { evaluated: boolean; contaminated: boolean };
}

export interface Report {
  strategy: string;
  sleeve: string;
  curves: { oos: Point[]; baseline: Point[]; oos_drawdown: Point[] };
  cost_sensitivity: { multiplier: number; sharpe: number | null; cagr: number | null }[];
  heatmap: { x_key: string | null; y_key: string | null; cells: { x: string; y: string; sharpe: number | null }[] };
  walk_forward: { windows: unknown[]; oos_start: string; oos_end: string };
}

export interface Tracking {
  sleeve: string;
  mode: string;
  shadow: {
    status: string;
    curve?: [string, number, number][];
    final_gap_points?: number | null;
    gap_pct_annualised?: number | null;
    max_abs_gap_points?: number | null;
    n_bars?: number;
  };
  implementation_shortfall_bps: { n_fills: number; mean: number | null; max: number | null };
}

async function get<T>(path: string): Promise<T> {
  const r = await fetch(`/api${path}`);
  if (!r.ok) throw new Error(`${r.status} ${r.statusText} for ${path}`);
  return (await r.json()) as T;
}

export const api = {
  status: () => get<Status>("/status"),
  equity: (sleeve: string, mode = "sim") => get<Equity>(`/equity?sleeve=${sleeve}&mode=${mode}`),
  positions: (mode = "sim") => get<Positions[]>(`/positions?mode=${mode}`),
  trades: (limit = 100, mode = "sim") => get<Trade[]>(`/trades?limit=${limit}&mode=${mode}`),
  strategies: () => get<StrategySummary[]>("/strategies"),
  report: (id: string, sleeve: string) => get<Report>(`/strategies/${id}/report?sleeve=${sleeve}`),
  tracking: (sleeve: string, mode = "sim") => get<Tracking>(`/tracking?sleeve=${sleeve}&mode=${mode}`),
};

export const fmt = {
  num: (x: number | null | undefined, d = 2) => (x == null || Number.isNaN(x) ? "—" : x.toFixed(d)),
  pct: (x: number | null | undefined, d = 1) =>
    x == null || Number.isNaN(x) ? "—" : `${x >= 0 ? "+" : ""}${(x * 100).toFixed(d)}%`,
  age: (s: number | null | undefined) => {
    if (s == null) return "—";
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400 * 2) return `${(s / 3600).toFixed(1)} h`;
    return `${(s / 86400).toFixed(1)} d`;
  },
};
