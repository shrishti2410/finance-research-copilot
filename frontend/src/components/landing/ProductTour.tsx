"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

import { BrowserFrame, type ShotName } from "./BrowserFrame";

const STOPS: { value: string; label: string; shot: ShotName; text: string }[] = [
  {
    value: "chart",
    label: "Chart",
    shot: "tour-chart",
    text: "Daily candles with moving averages, volume, RSI and MACD (Bollinger bands one click away), and the days the signal went in or out of the stock.",
  },
  {
    value: "signal",
    label: "Signal",
    shot: "tour-signal",
    text: "Textbook rules vote. You see each rule's position and weight, the market regime, and the strategy card that explains the rule.",
  },
  {
    value: "backtest",
    label: "Backtest",
    shot: "tour-backtest",
    text: "Walk-forward results after trading costs, against buy-and-hold and random timing, with the verdict stated plainly.",
  },
  {
    value: "options",
    label: "Options",
    shot: "tour-options",
    text: "Structures for study: estimated premiums from historical volatility, payoff at expiry, breakevens and Greeks.",
  },
  {
    value: "research",
    label: "Research",
    shot: "tour-research",
    text: "For the US companies, the 10-K filings are indexed. Suggested questions open the research chat, which answers with its sources and traces every figure to what a tool returned.",
  },
];

export function ProductTour() {
  return (
    <Tabs defaultValue="chart">
      <TabsList aria-label="Product tour">
        {STOPS.map((s) => (
          <TabsTrigger key={s.value} value={s.value}>
            {s.label}
          </TabsTrigger>
        ))}
      </TabsList>
      {STOPS.map((s) => (
        <TabsContent key={s.value} value={s.value} className="fade-in">
          <p className="mb-4 max-w-2xl text-sm text-fg-muted">{s.text}</p>
          <BrowserFrame shot={s.shot} sizes="(min-width: 1152px) 1104px, 100vw" />
        </TabsContent>
      ))}
    </Tabs>
  );
}
