import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Join class names, letting a later Tailwind class override an earlier one. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });

/** "₹1,090.50" for INR, "$123.40" otherwise; an index has no currency sign. */
export function formatPrice(value: number, symbol = "₹", isIndex = false): string {
  return `${isIndex ? "" : symbol}${inr.format(value)}`;
}

export function formatPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  const sign = value > 0 ? "+" : value < 0 ? "−" : "";
  return `${sign}${Math.abs(value).toFixed(digits)}%`;
}
