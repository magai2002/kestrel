import { useQueries, useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, fmt } from "../api";
import { LineChart } from "../components/LineChart";

const SLEEVES = ["crypto", "equities"] as const;

export function Overview() {
  const equity = useQueries({
    queries: SLEEVES.flatMap((sleeve) =>
      ["sim", "sandbox"].map((mode) => ({
        queryKey: ["equity", sleeve, mode],
        queryFn: () => api.equity(sleeve, mode),
        refetchInterval: 60_000,
      })),
    ),
  });
  const positions = useQuery({ queryKey: ["positions"], queryFn: () => api.positions("sim"), refetchInterval: 60_000 });

  return (
    <>
      <p className="lede">
        Two sleeves trading on paper: crypto (BTC, ETH; 4-hour bars) and ETFs (ten asset classes; daily bars). Equity is
        indexed to 100 at the start, so no balances are shown.
      </p>
      {SLEEVES.map((sleeve, i) => {
        const sim = equity[i * 2]?.data;
        const sandbox = equity[i * 2 + 1]?.data;
        const last = sim?.equity_index.at(-1)?.[1];
        const prev = sim?.equity_index.at(-2)?.[1];
        const w = positions.data?.find((p) => p.sleeve === sleeve);
        return (
          <section key={sleeve} className="panel">
            <div className="panel-head">
              <h2>
                {sleeve}{" "}
                <Link className="why-link" to={`/decisions?sleeve=${sleeve}`}>
                  why these positions? →
                </Link>
              </h2>
              <div className="tiles">
                <div>
                  <span>index</span>
                  <b>{fmt.num(last, 1)}</b>
                </div>
                <div>
                  <span>last bar</span>
                  <b>{last && prev ? fmt.pct(last / prev - 1, 2) : "—"}</b>
                </div>
                <div>
                  <span>drawdown</span>
                  <b>{fmt.num(sim?.drawdown_pct.at(-1)?.[1], 1)}%</b>
                </div>
              </div>
            </div>
            {sim && sim.equity_index.length > 1 ? (
              <LineChart
                series={[
                  { name: "sim (real prices, our fills)", points: sim.equity_index, color: "#e0a43a", area: true },
                  ...(sandbox && sandbox.equity_index.length > 1
                    ? [{ name: "sandbox (testnet / IBKR paper)", points: sandbox.equity_index, color: "#7aa7d9" }]
                    : []),
                ]}
              />
            ) : equity[i * 2]?.isLoading ? (
              <div className="skeleton" style={{ height: 260 }} />
            ) : (
              <p className="mute">Waiting for the first few bars of live history…</p>
            )}
            {sim && sim.drawdown_pct.length > 1 && (
              <LineChart height={120} percent series={[{ name: "drawdown", points: sim.drawdown_pct, color: "#e06a5a" }]} />
            )}
            {w && Object.values(w.weights_pct).every((x) => x === 0) && (
              <p className="mute small">
                Fully in cash right now: no asset has a strong enough signal to hold.{" "}
                <Link to={`/how?sleeve=${sleeve}`}>See exactly why</Link>.
              </p>
            )}
            {w && (
              <div className="weights">
                {Object.entries(w.weights_pct).map(([asset, pct]) => (
                  <div key={asset} title={`${asset}: ${pct}%`}>
                    <span>{asset}</span>
                    <div className="bar">
                      <i style={{ width: `${Math.min(100, pct)}%` }} />
                    </div>
                    <b>{pct.toFixed(1)}%</b>
                  </div>
                ))}
              </div>
            )}
          </section>
        );
      })}
    </>
  );
}
