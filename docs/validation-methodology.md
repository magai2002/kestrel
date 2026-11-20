# How kestrel decides whether a strategy is real

Most trading backtests are lies. Usually nobody means them to be: the
author tried many ideas, kept the one that looked best, and reported its
numbers as if it were the only idea they ever had. This document
explains, step by step and without assuming a finance background, what
`kestrel validate` does to avoid fooling itself.

The code is `research/validation.py`, and every statistic below is unit-tested
against a hand-worked value in `tests/unit/research/`.

## The problem in one paragraph

Give a hundred monkeys a hundred random strategies. A few will look
brilliant on historical data, through luck alone. Pick the best monkey's
strategy and it'll probably do no better than the rest from now on. In
backtesting, *you* are the one picking. Every parameter you try, every
variant you reject, every "let me just tweak the lookback" is another
monkey. The defences below all ask the same question: **how good would
this have to look, given how hard we searched, before we believe it?**

## Step 1: lock away the most recent quarter of history

Before anything else, the newest 25% of each sleeve's data is set aside
as a **hold-out**. Nothing in steps 2–6 can see it. The code never even
computes signals on those bars until the hold-out is explicitly
unlocked, and a test changes every hold-out price and checks that
nothing in the main report moves.

The hold-out is **looked at once**, when the strategy is final
(`kestrel validate <strategy> --holdout`). Every look is recorded in
Postgres. If you look, change something, and look again, the hold-out
has become part of your search, so the second look prints a loud
warning and every report from then on is stamped **"hold-out
contaminated"**. The Phase 7 live-trading gate refuses contaminated
strategies.

*Analogy:* the hold-out is the final exam. You can study as much as you
like, but you sit the exam once. Resitting after seeing the answers
doesn't measure anything.

## Step 2: try every parameter set, and write each one down

Each strategy has a small grid of parameter choices declared up front in
`config/validation.yaml` (for example three trend speeds × three
volatility spans = 9 combinations). Every combination is backtested
over the pre-hold-out period **with the full cost model**:

- **Commission** with its per-order minimum. On a $10,000 sleeve, IBKR's
  $1.70 minimum is a bigger cost than its 0.05% rate on any trade under
  $3,400.
- **Belgian stock-exchange tax (TOB)**.
- **Half the bid-ask spread.**
- **Slippage** that grows with volatility.

Every combination becomes a row in the **trial ledger**. The ledger
counts *distinct* parameter sets ever tried for that strategy and
sleeve, across every run. Re-running the same grid doesn't inflate the
count; widening the grid after seeing a result does, which is the
behaviour it should punish.

## Step 3: walk-forward, the honest out-of-sample curve

A single backtest over all the history lets the parameter choice "know"
the future. Walk-forward removes that:

1. Take a **training window** (3 years for daily ETFs, 2 years for 4-hour
   crypto) and find the parameter set that did best *in that window only*.
2. Use that choice, untouched, for the **next six months**. That's one
   out-of-sample (OOS) segment.
3. Slide both windows forward six months and repeat.

Stitching the OOS segments together gives an equity curve where every
bar was traded with parameters chosen using only data from before it.
**This is the curve the report leads with.** The in-sample best
(chosen with hindsight over the whole period) is shown later, labelled
as hindsight.

Two honest caveats are documented in code: each segment starts already
holding whatever that parameter set would have held, and the cost of
*switching* parameter sets at a window boundary isn't charged.

## Step 4: the Deflated Sharpe Ratio (DSR)

The **Sharpe ratio** is return per unit of risk: average return ÷
volatility. Above 1 is good; 0 means no edge.

The **Probabilistic Sharpe Ratio** asks: given how many observations we
have, and how lopsided and fat-tailed the returns are, what's the
probability the *true* Sharpe is above some hurdle? A Sharpe of 1.0 over
three months is much weaker evidence than 1.0 over ten years, and a
strategy that earns small gains with occasional disasters is weaker
evidence than its average suggests.

The **Deflated** Sharpe Ratio (Bailey & López de Prado, 2014) sets that
hurdle to *the Sharpe you'd expect from the luckiest of N strategies
that have no edge at all*, where N is the trial count from the ledger
and the spread of their Sharpes comes from the ledger too. **DSR is the
probability that the strategy beats that hurdle.** The report requires
DSR > 0.95.

*Analogy:* if one person flips ten heads in a row, be impressed. If you
asked 1,000 people to try and one did, don't be.

## Step 5: the Probability of Backtest Overfitting (PBO)

PBO (Bailey, Borwein, López de Prado & Zhu, 2015) tests the *selection
process* rather than one strategy. Cut the history into 16 chunks. For
every one of the 12,870 ways to pick 8 of them as "training" and the
other 8 as "testing", find the parameter set that wins in training and
see where it ranks in testing. **PBO is the fraction of splits where the
training winner ranks in the bottom half in testing.**

PBO near 0: whatever wins in training keeps winning, so the selection
is meaningful. PBO near 0.5 or above: picking the best performer is no
better than picking at random, so the "best" parameters are noise.

## Step 6: everything else in the report

- **Bootstrap confidence interval for Sharpe.** Resample the OOS
  returns thousands of times in 20-day *blocks*, not single days, so the
  way bad days cluster together is preserved. The spread of the
  resampled Sharpes is a 95% interval. If it comfortably includes 0, the
  data can't distinguish the strategy from no edge.
- **Cost sensitivity.** Re-run the OOS curve at 0×, 1×, 2× and 3× costs.
  An edge that disappears at 2× costs is one bad fee assumption away
  from not existing. The **break-even cost** is the per-trade cost at
  which net Sharpe hits zero.
- **Per year.** Is the result one lucky year?
- **Per regime.** Each bar is labelled bull, bear or sideways from the
  benchmark's own trailing 200-day trend, known at the time. A strategy
  that only works in bull markets is a bull-market bet, not an edge.
- **Parameter stability.** A heatmap of Sharpe across the grid. Robust
  edges sit on a plateau, where neighbouring parameters are nearly as
  good. Lucky ones sit on a spike. The report flags the best grid point
  if it beats its neighbours' median by more than 0.3 Sharpe.
- **Buffering.** Turnover and Sharpe with and without the 10% position
  buffer ("don't trade unless the target moved by more than 10 percentage
  points"). At retail size with per-order minimum fees, this is often
  the difference between a strategy and a fee-generating machine.
- **The baseline, on the same period with the same costs.** Trend
  following is compared with buy-and-hold, and momentum with
  equal-weight rebalancing. Beating cash is easy; beating doing nothing
  clever is the real test.

## The verdict

A strategy **survives** only if all four hold:

1. DSR > 0.95.
2. PBO < 0.5 (not applicable to single-configuration strategies).
3. OOS Sharpe beats the baseline's OOS Sharpe.
4. No sharp parameter peak.

This is deliberately strict. Most strategies, including well-known
ones, fail it on a few years of data, and the report says so plainly.
A failed verdict isn't a bug report; it's the system working.

## One thing specific to this project

The equities sleeve is *researched* on US ETFs (SPY, QQQ...), which have
long, clean histories, but *traded* as their Irish-domiciled UCITS twins
on the London Stock Exchange (EU retail investors can't buy US-domiciled
ETFs). The costs in every report are the twins' costs. The difference
between the two instrument sets (different exchange hours, FX, some
index differences, thinner trading) is measured once the bot runs, on
the live-vs-backtest tracking page, rather than assumed away. See
[ADR 0004](adr/0004-ibkr-ucits-hybrid-data.md).
