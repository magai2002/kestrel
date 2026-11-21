import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import { PipelineExplorer, type Ctx } from "../components/PipelineExplorer";
import { useNow } from "../hooks";

const REPO = "https://github.com/magai2002/kestrel";
const SLEEVES = ["crypto", "equities"] as const;

/** Rolling walk-forward windows (research/validation.py: fit on the
 * previous 2-3 years, score on the next 6 months, roll by 6 months),
 * then the locked hold-out (config/validation.yaml: newest 25%). */
function ValidationPicture() {
  const windows = 5;
  return (
    <div className="wf" role="img" aria-label="Rolling walk-forward: each window fits on the previous years and is scored on the next six months; the newest quarter of history is locked away.">
      {Array.from({ length: windows }, (_, i) => (
        <div key={i} className="wf-row">
          <span className="mute small">window {i + 1}</span>
          <div className="wf-track">
            <i className="fit" style={{ left: `${i * 9}%`, width: "30%" }} />
            <i className="test" style={{ left: `${30 + i * 9}%`, width: "9%" }} />
            <i className="lock" />
          </div>
        </div>
      ))}
      <div className="legend small">
        <span>
          <i className="sw fit" /> fit on the previous 2–3 years
        </span>
        <span>
          <i className="sw test" /> score the next 6 months, all costs in
        </span>
        <span>
          <i className="sw lock" /> hold-out: newest 25%, looked at once
        </span>
      </div>
    </div>
  );
}

export function HowItWorks() {
  const [params, setParams] = useSearchParams();
  const sleeve = (params.get("sleeve") as (typeof SLEEVES)[number]) || "crypto";
  const now = useNow(15_000);
  const dec = useQuery({ queryKey: ["decisions", sleeve, "sim"], queryFn: () => api.decisions(sleeve, "sim"), refetchInterval: 60_000 });
  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 30_000 });
  const assets = Object.keys(dec.data?.latest_decided?.assets ?? {});
  const [asset, setAsset] = useState("");
  useEffect(() => setAsset(""), [sleeve]);
  const a = asset || assets[0] || "";

  const ctx: Ctx = {
    sleeve,
    asset: a,
    decided: dec.data?.latest_decided ?? null,
    latest: dec.data?.latest ?? null,
    a: dec.data?.latest_decided?.assets?.[a] ?? null,
    status: status.data?.sleeves.find((s) => s.sleeve === sleeve && s.mode === "sim"),
    now,
  };

  return (
    <>
      <section className="hero">
        <h1>How one decision is made</h1>
        <p className="lede">
          Every time a bar closes, the bot runs the same loop: nine steps from a new price to an order. Follow a real asset
          through the latest real decision. Every number below comes from that cycle's recorded trace, not a re-run.
        </p>
      </section>

      <div className="filters decisions-bar">
        <div className="tabs" role="tablist" aria-label="sleeve">
          {SLEEVES.map((s) => (
            <button
              key={s}
              role="tab"
              aria-selected={s === sleeve}
              className={s === sleeve ? "active" : ""}
              onClick={() => setParams((p) => (p.set("sleeve", s), p), { replace: true })}
            >
              {s}
            </button>
          ))}
        </div>
        {assets.length > 0 && (
          <label className="inline">
            <span className="mute small">follow</span>
            <select value={a} onChange={(e) => setAsset(e.target.value)}>
              {assets.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
        )}
      </div>

      {dec.isLoading ? <div className="panel skeleton" style={{ height: 380 }} /> : <PipelineExplorer c={ctx} />}

      <section className="panel">
        <h2>How a strategy earns its place</h2>
        <p>
          Before a strategy trades on paper it has to survive validation. The newest quarter of history is locked away and
          looked at once. On the rest: walk-forward testing with all costs, every parameter set tried written to a ledger, and
          the Deflated Sharpe Ratio and Probability of Backtest Overfitting to correct for how hard we searched.
        </p>
        <ValidationPicture />
        <p className="small">
          Results per strategy are on the Strategies page;{" "}
          <a href={`${REPO}/blob/main/docs/validation-methodology.md`}>the methodology</a> explains each test.
        </p>
      </section>

      <section className="panel">
        <h2>Read more</h2>
        <ul>
          <li>
            <a href={REPO}>Source code on GitHub</a>
          </li>
          <li>
            <a href={`${REPO}/tree/main/docs/adr`}>Architecture decision records</a>: why things are built the way they are
          </li>
          <li>
            <a href={`${REPO}/blob/main/docs/runbook.md`}>Runbook</a>: operating the bot, resetting kill switches
          </li>
        </ul>
      </section>
    </>
  );
}
