"""Render a validation report (the JSON from ``research.validation``) as
one self-contained HTML file: inline SVG charts, no JavaScript, no
external assets except the font. Report language follows spec Section
7, step 5: lead with the DSR and the out-of-sample result, then
in-sample numbers — never the other way round.
"""

from __future__ import annotations

import html
import math
from typing import Any

CSS = """
:root{--bg:#0f0d0a;--panel:#1a1712;--ink:#efe6d6;--mute:#a89c87;--line:#3a3328;
--gold:#e0a43a;--gold2:#f5c66b;--good:#6fbf73;--bad:#e06a5a;--blue:#7aa7d9}
@media (prefers-color-scheme: light){:root:not([data-theme="dark"]){--bg:#faf7f1;--panel:#fff;
--ink:#221d14;--mute:#6b604f;--line:#e2dacb;--gold:#b37a12;--gold2:#8f600c;--good:#2e7d32;--bad:#c0392b;--blue:#2f6aa8}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:'Plus Jakarta Sans',system-ui,sans-serif;line-height:1.5}
main{max-width:1040px;margin:0 auto;padding:24px 16px 64px}
h1{font-size:1.6rem;margin:.2em 0}h2{font-size:1.15rem;margin:2em 0 .6em;color:var(--gold2)}
.mute{color:var(--mute)}.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.tile .v{font-size:1.5rem;font-weight:700}.tile .k{font-size:.8rem;color:var(--mute)}
table{border-collapse:collapse;width:100%;font-size:.88rem}th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:right}
th:first-child,td:first-child{text-align:left}th{color:var(--mute);font-weight:600}
.pill{display:inline-block;padding:2px 10px;border-radius:99px;font-weight:700;font-size:.85rem}
.pass{background:color-mix(in srgb,var(--good) 20%,transparent);color:var(--good)}
.fail{background:color-mix(in srgb,var(--bad) 20%,transparent);color:var(--bad)}
.warn{border-left:4px solid var(--bad);padding:8px 12px;background:color-mix(in srgb,var(--bad) 10%,transparent)}
.scroll{overflow-x:auto}svg text{fill:var(--mute);font-size:11px;font-family:inherit}
"""


def _f(x: Any, fmt: str = "{:.2f}") -> str:
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return "—"
    return fmt.format(x)


def _pct(x: Any, digits: int = 1) -> str:
    return _f(x, "{:+." + str(digits) + "%}") if x is not None else "—"


def _line_chart(
    series: dict[str, list[list[Any]]],
    colors: dict[str, str],
    height: int = 220,
    pct_axis: bool = False,
) -> str:
    w, h, pad = 1000, height, 36
    pts = [p for s in series.values() for p in s]
    if not pts:
        return "<p class='mute'>no data</p>"
    xs = sorted({p[0] for p in pts})
    x_index = {x: i for i, x in enumerate(xs)}
    ys = [p[1] for p in pts if p[1] is not None]
    lo, hi = min(ys), max(ys)
    if hi == lo:
        hi = lo + 1

    def sx(x: str) -> float:
        return pad + (w - 2 * pad) * x_index[x] / max(1, len(xs) - 1)

    def sy(y: float) -> float:
        return float(h - pad + (y - lo) / (hi - lo) * -(h - 2 * pad))

    parts = [f"<svg viewBox='0 0 {w} {h}' width='100%' role='img'>"]
    for frac in (0, 0.5, 1):
        y = lo + (hi - lo) * frac
        label = f"{y:.0%}" if pct_axis else f"{y:.0f}"
        parts.append(
            f"<line x1='{pad}' x2='{w - pad}' y1='{sy(y):.1f}' y2='{sy(y):.1f}' stroke='var(--line)'/>"
        )
        parts.append(f"<text x='2' y='{sy(y) + 4:.1f}'>{label}</text>")
    for x in (xs[0], xs[len(xs) // 2], xs[-1]):
        parts.append(f"<text x='{sx(x) - 30:.1f}' y='{h - 8}'>{html.escape(x[:10])}</text>")
    for name, s in series.items():
        d = " ".join(
            f"{'M' if i == 0 else 'L'}{sx(p[0]):.1f},{sy(p[1]):.1f}"
            for i, p in enumerate(s)
            if p[1] is not None
        )
        parts.append(
            f"<path d='{d}' fill='none' stroke='{colors.get(name, 'var(--gold)')}' stroke-width='1.8'/>"
        )
    parts.append("</svg>")
    legend = " ".join(
        f"<span style='color:{colors.get(k, 'var(--gold)')}'>━ {html.escape(k)}</span>"
        for k in series
    )
    return "".join(parts) + f"<div class='mute' style='font-size:.85rem'>{legend}</div>"


def _heat_table(heatmap: dict[str, Any]) -> str:
    cells = heatmap.get("cells") or []
    if not cells or heatmap.get("x_key") is None:
        return "<p class='mute'>Single configuration — no grid to show.</p>"
    xs = list(dict.fromkeys(c["x"] for c in cells))
    ys = list(dict.fromkeys(c["y"] for c in cells))
    vals = [c["sharpe"] for c in cells if c["sharpe"] is not None]
    lo, hi = (min(vals), max(vals)) if vals else (0, 1)
    lookup = {(c["x"], c["y"]): c["sharpe"] for c in cells}
    head = "".join(f"<th>{html.escape(y) or '—'}</th>" for y in ys)
    rows = []
    for x in xs:
        tds = []
        for y in ys:
            v = lookup.get((x, y))
            t = 0.0 if v is None or hi == lo else (v - lo) / (hi - lo)
            tds.append(
                f"<td style='background:color-mix(in srgb,var(--gold) {int(10 + 60 * t)}%,transparent)'>{_f(v)}</td>"
            )
        rows.append(f"<tr><td>{html.escape(x)}</td>{''.join(tds)}</tr>")
    return (
        f"<div class='scroll'><table><tr><th>{html.escape(str(heatmap['x_key']))} ↓ / "
        f"{html.escape(str(heatmap.get('y_key') or ''))} →</th>{head}</tr>{''.join(rows)}</table></div>"
    )


def _stats_table(rows: dict[str, dict[str, Any]], cols: list[tuple[str, str, str]]) -> str:
    head = "".join(f"<th>{label}</th>" for _, label, _ in cols)
    body = []
    for name, r in rows.items():
        tds = "".join(
            f"<td>{_pct(r.get(k)) if kind == 'pct' else _f(r.get(k), '{:.2f}') if kind == 'num' else _f(r.get(k), '{:.0f}')}</td>"
            for k, _, kind in cols
        )
        body.append(f"<tr><td>{html.escape(name)}</td>{tds}</tr>")
    return f"<div class='scroll'><table><tr><th></th>{head}</tr>{''.join(body)}</table></div>"


def render(report: dict[str, Any]) -> str:
    h = report["headline"]
    ho = report["holdout"]
    verdict = report["verdict"]
    title = f"{report['strategy']} · {report['sleeve']}"
    pill = (
        "<span class='pill pass'>survives</span>"
        if verdict["survives"]
        else "<span class='pill fail'>does not survive</span>"
    )
    warn = ""
    if ho.get("contaminated"):
        warn = "<p class='warn'><b>HOLD-OUT CONTAMINATED.</b> The hold-out for this strategy and sleeve has been looked at more than once. Its numbers are no longer out-of-sample evidence.</p>"
    tiles = [
        (
            "Deflated Sharpe",
            _f(h["dsr"]),
            f"P(true Sharpe beats the best-of-{h['n_trials']} luck hurdle)",
        ),
        (
            "OOS Sharpe",
            _f(h["oos_sharpe"]),
            f"95% CI {_f(h['sharpe_ci'][0])} … {_f(h['sharpe_ci'][1])}",
        ),
        (f"Baseline ({h['baseline']})", _f(h["baseline_oos_sharpe"]), "same period, same costs"),
        ("OOS CAGR", _pct(h["oos_cagr"]), f"max drawdown {_pct(h['oos_max_drawdown'])}"),
        ("PBO", _f(h["pbo"]), "≥ 0.5: selection no better than chance"),
    ]
    tiles_html = "".join(
        f"<div class='panel tile'><div class='k'>{k}</div><div class='v'>{v}</div><div class='k'>{s}</div></div>"
        for k, v, s in tiles
    )
    checks = "".join(
        f"<li>{'✅' if ok else '❌'} {html.escape(name.replace('_', ' '))}</li>"
        for name, ok in verdict["checks"].items()
    )
    oos = report["oos"]
    wf = report["walk_forward"]
    windows = "".join(
        f"<tr><td>{w['test_start'][:10]} → {w['test_end'][:10]}</td><td>{html.escape(', '.join(f'{k}={v}' for k, v in w['chosen_params'].items()) or '—')}</td>"
        f"<td>{_f(w['train_sharpe'])}</td><td>{_f(w['test_sharpe'])}</td><td>{_pct(w['test_return'])}</td></tr>"
        for w in wf["windows"]
    )
    cs = report["cost_sensitivity"]
    cs_rows = "".join(
        f"<tr><td>{c['multiplier']:g}x</td><td>{_f(c['sharpe'])}</td><td>{_pct(c['cagr'])}</td></tr>"
        for c in cs
    )
    buf = report["buffering"]
    ho_html = "<p class='mute'>Not evaluated in this run. Run <code>kestrel validate … --holdout</code> exactly once when the strategy is final.</p>"
    if ho.get("evaluated"):
        hs = ho["summary"]
        ho_html = (
            f"<p>Params chosen on the whole pre-hold-out period: <code>{html.escape(str(ho.get('params')))}</code>. "
            f"Previous accesses: {ho['prior_accesses']}.</p>"
            f"<div class='tiles'><div class='panel tile'><div class='k'>Hold-out Sharpe</div><div class='v'>{_f(hs['sharpe'])}</div></div>"
            f"<div class='panel tile'><div class='k'>Baseline Sharpe</div><div class='v'>{_f(ho['baseline_summary']['sharpe'])}</div></div>"
            f"<div class='panel tile'><div class='k'>Hold-out CAGR</div><div class='v'>{_pct(hs['cagr'])}</div></div>"
            f"<div class='panel tile'><div class='k'>Hold-out max DD</div><div class='v'>{_pct(hs['max_drawdown'])}</div></div></div>"
            + _line_chart(
                report["curves"]["holdout"], {"strategy": "var(--gold)", "baseline": "var(--blue)"}
            )
        )
    assumptions = "".join(
        f"<tr><td>{html.escape(a)}</td><td>{html.escape(c.get('fee_pct', ''))}</td><td>{html.escape(c.get('min_fee', ''))}</td>"
        f"<td>{html.escape(c.get('spread_bps', ''))}</td><td>{html.escape(c.get('tob_pct', ''))}</td><td style='text-align:left'>{html.escape(c.get('notes', ''))}</td></tr>"
        for a, c in report["cost_assumptions"].items()
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com"><link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body><main>
<p class="mute">kestrel validation report · run {html.escape(report["run_id"][:8])} · git {html.escape((report.get("git_sha") or "—")[:10])} · data {html.escape(report["data_hash"])}</p>
<h1>{html.escape(title)} {pill}</h1>
{warn}
<p>Out-of-sample means: parameters were chosen on earlier data only, then judged on the next six months, repeatedly ({len(wf["windows"])} windows, {wf["oos_start"][:10]} → {wf["oos_end"][:10]}). Costs included throughout. The most recent 25% of history is a locked hold-out, shown at the end.</p>
<div class="tiles">{tiles_html}</div>
<ul>{checks}</ul>

<h2>Out-of-sample equity (indexed to 100) vs baseline</h2>
<div class="panel">{_line_chart({"strategy": report["curves"]["oos"], "baseline": report["curves"]["baseline"]}, {"strategy": "var(--gold)", "baseline": "var(--blue)"})}</div>
<h2>Out-of-sample drawdown</h2>
<div class="panel">{_line_chart({"drawdown": report["curves"]["oos_drawdown"]}, {"drawdown": "var(--bad)"}, 160, pct_axis=True)}</div>

<h2>Out-of-sample statistics</h2>
<div class="panel">{_stats_table({"strategy": oos, "baseline": report["baseline_oos"]}, [("cagr", "CAGR", "pct"), ("annualized_vol", "Vol", "pct"), ("sharpe", "Sharpe", "num"), ("sortino", "Sortino", "num"), ("calmar", "Calmar", "num"), ("max_drawdown", "Max DD", "pct"), ("turnover_annual", "Turnover/yr", "num"), ("cost_drag_annual", "Cost drag/yr", "pct")])}
<p class="mute">Gross CAGR {_pct(oos["gross_cagr"])} → net {_pct(oos["cagr"])}. Break-even cost {_pct(oos["break_even_cost"], 2)} per unit of turnover (the per-trade cost at which net Sharpe hits zero). Max-drawdown duration {_f(oos["max_drawdown_duration_bars"], "{:.0f}")} bars.</p></div>

<h2>Deflated Sharpe and overfitting</h2>
<div class="panel"><p>Observed per-bar Sharpe {_f(report["dsr"]["observed_sr_per_period"], "{:.4f}")} vs the hurdle {_f(report["dsr"]["hurdle_sr_per_period"], "{:.4f}")} (≈ {_f(report["dsr"]["hurdle_sr_annual"])} annualised) that the best of {report["dsr"]["n_trials"]} zero-skill trials would reach by luck, given the Sharpe dispersion the trial ledger has seen. DSR = {_f(report["dsr"]["dsr"])}.</p>
<p>PBO = {_f(report["pbo"]["pbo"])} over {report["pbo"]["n_splits"]} combinatorial splits of {report["pbo"]["n_trials"]} configurations.
Block-bootstrap Sharpe {_f(report["bootstrap"]["sharpe"])}, {report["bootstrap"]["confidence"]:.0%} CI [{_f(report["bootstrap"]["lower"])}, {_f(report["bootstrap"]["upper"])}] ({report["bootstrap"]["resamples"]} resamples, {report["bootstrap"]["block_bars"]}-bar blocks).</p></div>

<h2>Walk-forward windows</h2>
<div class="panel scroll"><table><tr><th>Test window</th><th style="text-align:left">Chosen in train window</th><th>Train Sharpe</th><th>Test Sharpe</th><th>Test return</th></tr>{windows}</table></div>

<h2>Cost sensitivity (out-of-sample)</h2>
<div class="panel"><table><tr><th>Costs</th><th>Sharpe</th><th>CAGR</th></tr>{cs_rows}</table></div>

<h2>Per year (out-of-sample)</h2>
<div class="panel">{_stats_table(report["per_year"], [("return", "Return", "pct"), ("vol", "Vol", "pct"), ("sharpe", "Sharpe", "num"), ("max_drawdown", "Max DD", "pct"), ("bars", "Bars", "int")])}</div>
<h2>Per regime (benchmark's trailing 200-day trend)</h2>
<div class="panel">{_stats_table(report["per_regime"], [("share_of_bars", "Share", "pct"), ("ann_mean_return", "Ann. mean return", "pct"), ("sharpe", "Sharpe", "num"), ("max_drawdown", "Max DD", "pct")])}</div>

<h2>Parameter stability (in-sample Sharpe across the grid)</h2>
<div class="panel">{_heat_table(report["heatmap"])}
<p class="mute">Sharp peak: <b>{"yes — the best point stands alone; treat it as luck" if report["sharp_peak"].get("flag") else "no"}</b>. In-sample best (hindsight, for comparison only): {html.escape(str(report["in_sample_best"]["params"]))}, Sharpe {_f(report["in_sample_best"]["sharpe"])}.</p></div>

<h2>Position buffering</h2>
<div class="panel"><p>Buffer {buf["buffer_pct"]:.0%}: turnover {_f(buf["turnover_annual_unbuffered"])}x/yr → {_f(buf["turnover_annual_buffered"])}x/yr; Sharpe {_f(buf["sharpe_unbuffered"])} → {_f(buf["sharpe_buffered"])}.</p></div>

<h2>Hold-out (most recent 25%, {ho["start"][:10]} → {ho["end"][:10]})</h2>
<div class="panel">{ho_html}</div>

<h2>Cost assumptions</h2>
<div class="panel scroll"><table><tr><th>Asset</th><th>Fee</th><th>Min fee</th><th>Spread bps</th><th>TOB</th><th style="text-align:left">Notes</th></tr>{assumptions}</table>
<p class="mute">Equities are researched on US ETFs and traded as their UCITS twins; costs are the twins'. TOB rates are the author's tax situation, not advice.</p></div>
<p class="mute">Not financial advice. Educational project.</p>
</main></body></html>"""
