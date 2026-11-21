import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, fmt } from "../api";
import { useNow } from "../hooks";
import { until } from "./PipelineExplorer";

const MODE_HINT: Record<string, string> = {
  sim: "paper trading on real prices with simulated fills",
  sandbox: "venue test accounts (Binance testnet, IBKR paper)",
  live: "real money",
};

export function StatusStrip() {
  const now = useNow(30_000);
  const { data, error } = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 30_000 });
  if (error) return <div className="strip"><div className="pill-box bad">API unreachable: {String(error)}</div></div>;
  if (!data) return <div className="strip"><div className="pill-box skeleton" style={{ width: 320, height: 34 }} /></div>;
  return (
    <div className="strip" aria-label="bot status">
      {data.sleeves.map((s) => {
        const tripped = Object.entries(s.kill_switches).filter(([, v]) => v.tripped).map(([k]) => k);
        const recon = s.last_reconciliation;
        const age = s.last_bar_close ? (now - new Date(s.last_bar_close).getTime()) / 1000 : s.data_age_seconds;
        return (
          <Link
            key={`${s.mode}/${s.sleeve}`}
            to={`/decisions?sleeve=${s.sleeve}&mode=${s.mode}`}
            className={`pill-box ${tripped.length ? "bad" : s.paused ? "warn" : "good"}`}
            title={`${s.sleeve} · ${s.mode}: ${MODE_HINT[s.mode] ?? s.mode}. Click for the latest decision.`}
          >
            <span className={`dot ${tripped.length ? "bad" : s.paused ? "warn" : "good"}`} aria-hidden="true" />
            <b>
              {s.sleeve} <span className="mute">· {s.mode}</span>
            </b>
            <span>bar {fmt.age(age)} ago</span>
            {s.next_decision && !tripped.length && !s.paused && <span>next decision in {until(s.next_decision, now)}</span>}
            <span>{tripped.length ? `halted: ${tripped.join(", ")}` : s.paused ? "paused" : "running"}</span>
            {recon && <span>reconciled {recon.ok ? "✓" : "✗"}</span>}
          </Link>
        );
      })}
    </div>
  );
}
