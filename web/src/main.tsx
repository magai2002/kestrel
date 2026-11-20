import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter, NavLink, Route, Routes } from "react-router-dom";
import { StatusStrip } from "./components/StatusStrip";
import { HowItWorks } from "./pages/HowItWorks";
import { Overview } from "./pages/Overview";
import { Strategies } from "./pages/Strategies";
import { Tracking } from "./pages/Tracking";
import { Trades } from "./pages/Trades";
import "./styles.css";

const queryClient = new QueryClient({ defaultOptions: { queries: { staleTime: 30_000, retry: 1 } } });

function App() {
  return (
    <BrowserRouter>
      <header>
        <div className="brand">
          <span className="mark">◆</span> kestrel
        </div>
        <nav>
          <NavLink to="/" end>overview</NavLink>
          <NavLink to="/trades">trades</NavLink>
          <NavLink to="/strategies">strategies</NavLink>
          <NavLink to="/tracking">live vs backtest</NavLink>
          <NavLink to="/how">how it works</NavLink>
        </nav>
      </header>
      <main>
        <StatusStrip />
        <Routes>
          <Route path="/" element={<Overview />} />
          <Route path="/trades" element={<Trades />} />
          <Route path="/strategies" element={<Strategies />} />
          <Route path="/tracking" element={<Tracking />} />
          <Route path="/how" element={<HowItWorks />} />
        </Routes>
      </main>
      <footer>
        <b>Paper trading. Not financial advice.</b> Educational project. Charts by{" "}
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
