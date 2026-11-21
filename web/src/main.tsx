import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Component, type ReactNode, StrictMode, useEffect } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, Link, NavLink, Route, Routes, useLocation } from "react-router-dom";
import { StatusStrip } from "./components/StatusStrip";
import { usePageTitle } from "./hooks";
import { Decisions } from "./pages/Decisions";
import { HowItWorks } from "./pages/HowItWorks";
import { Overview } from "./pages/Overview";
import { Strategies } from "./pages/Strategies";
import { Tracking } from "./pages/Tracking";
import { Trades } from "./pages/Trades";
import "./styles.css";

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 1 } } });

const PAGES: [string, string, string][] = [
  ["/", "overview", "Overview"],
  ["/decisions", "decisions", "Decisions"],
  ["/trades", "trades", "Trades"],
  ["/strategies", "strategies", "Strategies"],
  ["/tracking", "live vs backtest", "Live vs backtest"],
  ["/how", "how it works", "How it works"],
];

function Chrome() {
  const { pathname } = useLocation();
  const page = PAGES.find(([p]) => p === pathname);
  usePageTitle(page ? (page[0] === "/" ? "" : page[2]) : "Not found");
  useEffect(() => window.scrollTo(0, 0), [pathname]);
  return null;
}

function NotFound() {
  return (
    <section className="panel">
      <h2>Nothing here</h2>
      <p className="mute">
        That page doesn't exist. Try the <Link to="/">overview</Link> or <Link to="/how">how it works</Link>.
      </p>
    </section>
  );
}

/** One page failing to render must not blank the whole app (React unmounts
 * everything on an uncaught render error). Keyed by path, so navigating away
 * clears it. */
class PageBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error) {
    console.error("page failed to render", error);
  }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <section className="panel" role="alert">
        <h2>This page failed to load</h2>
        <p className="mute">
          Usually a newer version of the site was deployed while this tab was open.{" "}
          <button onClick={() => window.location.reload()}>reload</button>
        </p>
        <p className="mute small">{String(this.state.error.message || this.state.error)}</p>
      </section>
    );
  }
}

function Pages() {
  const { pathname } = useLocation();
  return (
    <PageBoundary key={pathname}>
      <Routes>
        <Route path="/" element={<Overview />} />
        <Route path="/decisions" element={<Decisions />} />
        <Route path="/trades" element={<Trades />} />
        <Route path="/strategies" element={<Strategies />} />
        <Route path="/tracking" element={<Tracking />} />
        <Route path="/how" element={<HowItWorks />} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </PageBoundary>
  );
}

function App() {
  return (
    <BrowserRouter>
      <Chrome />
      <a className="skip" href="#main">
        Skip to content
      </a>
      <header>
        <div className="header-inner">
          <Link to="/" className="brand" aria-label="kestrel home">
            <span className="mark" aria-hidden="true">◆</span> kestrel <span className="tag">paper trading</span>
          </Link>
          <nav aria-label="pages">
            {PAGES.map(([to, name]) => (
              <NavLink key={to} to={to} end={to === "/"}>
                {name}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main id="main">
        <StatusStrip />
        <Pages />
      </main>
      <footer>
        <b>Paper trading. Not financial advice.</b> Educational project ·{" "}
        <a href="https://github.com/magai2002/kestrel">source on GitHub</a> · charts by{" "}
        <a href="https://www.tradingview.com/">TradingView</a> Lightweight Charts.
      </footer>
    </BrowserRouter>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
