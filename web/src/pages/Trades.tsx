import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api, fmt } from "../api";

export function Trades() {
  const [sleeve, setSleeve] = useState("all");
  const [side, setSide] = useState("all");
  const { data, isLoading } = useQuery({ queryKey: ["trades"], queryFn: () => api.trades(500), refetchInterval: 60_000 });
  const rows = useMemo(
    () => (data ?? []).filter((t) => (sleeve === "all" || t.sleeve === sleeve) && (side === "all" || t.side === side)),
    [data, sleeve, side],
  );
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Trade blotter</h2>
        <div className="filters">
          <select value={sleeve} onChange={(e) => setSleeve(e.target.value)} aria-label="sleeve">
            <option value="all">all sleeves</option>
            <option value="crypto">crypto</option>
            <option value="equities">equities</option>
          </select>
          <select value={side} onChange={(e) => setSide(e.target.value)} aria-label="side">
            <option value="all">buys &amp; sells</option>
            <option value="buy">buys</option>
            <option value="sell">sells</option>
          </select>
        </div>
      </div>
      <p className="mute">
        Slippage = fill price vs the price when the decision was made (positive = worse). Implementation shortfall adds
        fees and taxes, in basis points of the trade.
      </p>
      {isLoading ? (
        <p>loading…</p>
      ) : rows.length === 0 ? (
        <p className="mute">No fills yet. The strategies trade rarely by design (position buffering).</p>
      ) : (
        <div className="scroll">
          <table>
            <thead>
              <tr>
                <th>time (UTC)</th>
                <th>sleeve</th>
                <th>asset</th>
                <th>side</th>
                <th>decision</th>
                <th>fill</th>
                <th>slippage</th>
                <th>fees</th>
                <th>shortfall</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((t, i) => (
                <tr key={i}>
                  <td>{t.time.replace("T", " ").slice(0, 16)}</td>
                  <td>{t.sleeve}</td>
                  <td>{t.asset}</td>
                  <td className={t.side}>{t.side}</td>
                  <td>{fmt.num(t.decision_price)}</td>
                  <td>{fmt.num(t.fill_price)}</td>
                  <td>{fmt.num(t.slippage_bps, 1)} bps</td>
                  <td>{fmt.num(t.fee_bps, 1)} bps</td>
                  <td>{fmt.num(t.shortfall_bps, 1)} bps</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
