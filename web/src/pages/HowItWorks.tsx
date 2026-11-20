const REPO = "https://github.com/magai2002/kestrel";

const STEPS = [
  ["Market data", "Binance and Yahoo history for research; Kraken, Yahoo and IBKR bars live. Every bar the bot sees is stored."],
  ["Strategy", "Turns closed bars into target weights. Never sees an unfinished bar, never places an order itself."],
  ["Portfolio", "Caps and scales the weights to the risk limits, ignores changes smaller than 10 points, rounds to real lot sizes."],
  ["Order manager", "Trades only the difference between target and holdings, with deterministic order ids, so a restart can't double-trade."],
  ["Risk", "Per-order checks (weight caps, fat-finger size, price band, minimum size) and six kill switches that stop the bot until a human resets them."],
  ["Broker", "One interface; simulated, Binance testnet, IBKR paper. Live trading is locked behind a code-enforced checklist."],
  ["Reconciler", "After every bar: does the venue hold what our ledger says? If not, stop and alert."],
];

export function HowItWorks() {
  return (
    <>
      <section className="panel">
        <h2>The pipeline</h2>
        <ol className="flow">
          {STEPS.map(([title, text]) => (
            <li key={title}>
              <b>{title}</b>
              <span>{text}</span>
            </li>
          ))}
        </ol>
        <p className="mute">
          The same strategy, portfolio, risk and order-manager code runs in backtest, paper and (one day) live. Only the clock,
          data feed and broker change. A test drives the live paper loop bar by bar and checks that it reproduces the backtest
          exactly.
        </p>
      </section>
      <section className="panel">
        <h2>How a strategy earns its place</h2>
        <p>
          The newest quarter of history is locked away and looked at once. On the rest: walk-forward testing with all costs,
          every parameter set tried written to a ledger, and the Deflated Sharpe Ratio and Probability of Backtest Overfitting
          to correct for how hard we searched.{" "}
          <a href={`${REPO}/blob/main/docs/validation-methodology.md`}>Validation methodology</a>.
        </p>
      </section>
      <section className="panel">
        <h2>Read more</h2>
        <ul>
          <li><a href={REPO}>Source code on GitHub</a></li>
          <li><a href={`${REPO}/tree/main/docs/adr`}>Architecture decision records</a></li>
          <li><a href={`${REPO}/blob/main/docs/runbook.md`}>Runbook</a></li>
        </ul>
      </section>
    </>
  );
}
