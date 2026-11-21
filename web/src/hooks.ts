import { useEffect, useState } from "react";

/** The current time, re-rendered every `ms`: for countdowns and "x min old". */
export function useNow(ms = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), ms);
    return () => window.clearInterval(id);
  }, [ms]);
  return now;
}

export function usePageTitle(title: string): void {
  useEffect(() => {
    document.title = title ? `${title} · kestrel` : "kestrel · paper trading";
  }, [title]);
}
