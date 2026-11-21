import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode, useEffect } from "react";
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
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/decisions" element={<Decisions />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/tracking" element={<Tracking />} />
          <Route path="/how" element={<HowItWorks />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
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
