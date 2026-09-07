"use client";

import { useState } from "react";
import type { TraceStep } from "@/lib/api";

/**
 * The "Sources" disclosure under an assistant message.
 *
 * An answer that states 71.07% without saying where it came from is a black
 * box, and the trace needed to open it is already stored on the message. This
 * renders it: which tool ran, with what arguments, whether it succeeded, and
 * what it returned.
 *
 * Steps that are not tool calls are shown too, and deliberately so. A
 * `tool_call_budget` or `ungrounded_answer` row is the explanation for an answer
 * that stopped early or took an extra turn -- hiding them would leave exactly
 * the confusing cases unexplained.
 */

/** Steps that describe the loop's own decisions rather than a tool call. */
const CONTROL_STEPS: Record<string, { label: string; blurb: string }> = {
  ungrounded_answer: {
    label: "Discarded a draft",
    blurb:
      "Stated a figure without calling a tool, so it was rejected and re-asked.",
  },
  tool_call_budget: {
    label: "Stopped at the tool-call budget",
    blurb: "Asked for more tool calls in one step than the budget allows.",
  },
  final_answer: { label: "Answered", blurb: "" },
};

function summarise(step: TraceStep): string {
  const args = Object.entries(step.arguments)
    .map(([key, value]) =>
      key === "query" || key === "ticker" || key === "ratio_name"
        ? String(value)
        : `${key}=${String(value)}`,
    )
    .join(", ");
  return `${step.tool}(${args})`;
}

function resultText(result: unknown): string {
  if (typeof result === "string") return result;
  return JSON.stringify(result, null, 2);
}

export function Sources({ steps }: { steps: TraceStep[] }) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);

  const toolCalls = steps.filter((s) => !(s.tool in CONTROL_STEPS));
  const control = steps.filter(
    (s) => s.tool in CONTROL_STEPS && s.tool !== "final_answer",
  );
  if (toolCalls.length === 0 && control.length === 0) return null;

  const label =
    toolCalls.length === 1 ? "1 source" : `${toolCalls.length} sources`;

  return (
    <div className="mt-3 border-t border-neutral-800 pt-2">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-xs text-neutral-400 hover:text-neutral-200"
      >
        <span
          className={`transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden
        >
          ▸
        </span>
        Sources · {label}
        {control.length > 0 && (
          <span className="text-amber-500/80">
            · {control.length} note{control.length > 1 ? "s" : ""}
          </span>
        )}
      </button>

      {open && (
        <ul className="mt-2 space-y-1.5">
          {steps
            .filter((s) => s.tool !== "final_answer")
            .map((step, index) => {
              const isControl = step.tool in CONTROL_STEPS;
              const isOpen = expanded === index;
              return (
                <li
                  key={index}
                  className="rounded border border-neutral-800 bg-neutral-900/60"
                >
                  <button
                    onClick={() => setExpanded(isOpen ? null : index)}
                    aria-expanded={isOpen}
                    className="flex w-full items-start gap-2 px-2.5 py-1.5 text-left"
                  >
                    <span
                      className={`mt-0.5 text-[10px] ${
                        step.ok ? "text-emerald-500" : "text-amber-500"
                      }`}
                      aria-hidden
                    >
                      {step.ok ? "●" : "▲"}
                    </span>
                    <span className="min-w-0 flex-1">
                      <code className="block break-all font-mono text-xs text-neutral-200">
                        {isControl
                          ? CONTROL_STEPS[step.tool].label
                          : summarise(step)}
                      </code>
                      {isControl && (
                        <span className="mt-0.5 block text-[11px] text-neutral-500">
                          {CONTROL_STEPS[step.tool].blurb}
                        </span>
                      )}
                    </span>
                    <span className="shrink-0 font-mono text-[10px] text-neutral-600">
                      {step.latency_ms >= 1
                        ? `${(step.latency_ms / 1000).toFixed(1)}s`
                        : ""}
                    </span>
                  </button>

                  {isOpen && (
                    <pre className="max-h-64 overflow-auto border-t border-neutral-800 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-neutral-400">
                      {resultText(step.result)}
                    </pre>
                  )}
                </li>
              );
            })}
        </ul>
      )}
    </div>
  );
}
