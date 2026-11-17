// In the shadcn/ui pattern (MIT). "buy"/"sell" are the only green/red badges.
import { cva, type VariantProps } from "class-variance-authority";
import * as React from "react";

import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-semibold tracking-wide",
  {
    variants: {
      variant: {
        neutral: "border-border bg-surface-2 text-fg-muted",
        buy: "border-up/40 bg-up-soft text-up",
        sell: "border-down/40 bg-down-soft text-down",
        warn: "border-warn/40 bg-surface-2 text-warn",
        accent: "border-accent/40 bg-surface-2 text-accent",
      },
    },
    defaultVariants: { variant: "neutral" },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
