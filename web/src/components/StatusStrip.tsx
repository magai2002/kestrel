import { useQuery } from "@tanstack/react-query";
import { api, fmt } from "../api";

export function StatusStrip() {
  const { data, error } = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 30_000 });
  if (error) return <div className="strip bad">API unreachable: {String(error)}</div>;
  if (!data) return <div className="strip">loading status…</div>;
  return (
    <div className="strip">
      {data.sleeves.map((s) => {
        const tripped = Object.entries(s.kill_switches).filter(([, v]) => v.tripped).map(([k]) => k);
        const recon = s.last_reconciliation;
        return (
          <div key={`${s.mode}/${s.sleeve}`} className={`pill-box ${tripped.length ? "bad" : "good"}`}>
            <b>
              {s.mode}/{s.sleeve}
            </b>
            <span>data {fmt.age(s.data_age_seconds)} old</span>
            <span>{tripped.length ? `kill switch: ${tripped.join(", ")}` : "no kill switch"}</span>
            {s.paused && <span>paused</span>}
            {recon && <span>reconciled {recon.ok ? "✓" : "✗"}</span>}
          </div>
        );
      })}
    </div>
  );
}
