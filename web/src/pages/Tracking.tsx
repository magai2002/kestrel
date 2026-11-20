import { useQueries } from "@tanstack/react-query";
import { api, fmt, type Point } from "../api";
import { LineChart } from "../components/LineChart";

export function Tracking() {
  const results = useQueries({
    queries: ["crypto", "equities"].map((sleeve) => ({ queryKey: ["tracking", sleeve], queryFn: () => api.tracking(sleeve) })),
  });
  return (
    <>
      <p className="lede">
        Every night the backtester replays exactly the bars the live paper bot saw, over the same period, with the same
        strategy and costs: a <i>shadow backtest</i>. If the bot is honest, the two curves sit on top of each other. Gaps come
        from bars missed while the bot was down, kill-switch halts, prices the venue revised after the fact, and (in sandbox)
        real fills differing from the model's.
      </p>
      {results.map(({ data }, i) => {
        const sleeve = ["crypto", "equities"][i];
        const curve = data?.shadow.curve ?? [];
        const live: Point[] = curve.map(([t, l]) => [t, l]);
        const shadow: Point[] = curve.map(([t, , s]) => [t, s]);
        return (
          <section key={sleeve} className="panel">
            <div className="panel-head">
              <h2>{sleeve}</h2>
              <div className="tiles">
                <div>
                  <span>gap now</span>
                  <b>{fmt.num(data?.shadow.final_gap_points, 2)} pts</b>
                </div>
                <div>
                  <span>max gap</span>
                  <b>{fmt.num(data?.shadow.max_abs_gap_points, 2)} pts</b>
                </div>
                <div>
                  <span>avg shortfall</span>
                  <b>{fmt.num(data?.implementation_shortfall_bps.mean, 1)} bps</b>
                </div>
              </div>
            </div>
            {curve.length > 1 ? (
              <LineChart
                series={[
                  { name: "live (sim)", points: live, color: "#e0a43a" },
                  { name: "shadow backtest", points: shadow, color: "#7aa7d9" },
                ]}
              />
            ) : (
              <p className="mute">The first shadow backtest runs at 00:20 UTC once there are a few bars of live history.</p>
            )}
          </section>
        );
      })}
    </>
  );
}
