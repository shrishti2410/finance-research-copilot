"use client";

// In the shadcn/ui pattern (MIT), on Radix Tabs: keyboard navigation and ARIA
// roles come from Radix.
import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as React from "react";

import { cn } from "@/lib/utils";

export const Tabs = TabsPrimitive.Root;

export function TabsList({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.List>) {
  return (
    <TabsPrimitive.List
      className={cn(
        // Fits five tabs at 390px; on narrower screens it scrolls, and the
        // right edge fades so the cut-off tab reads as "more this way".
        "flex w-full gap-0 overflow-x-auto border-b border-border [scrollbar-width:none] sm:gap-1",
        "max-[379px]:[mask-image:linear-gradient(to_right,black_80%,transparent)]",
        className,
      )}
      {...props}
    />
  );
}

export function TabsTrigger({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  return (
    <TabsPrimitive.Trigger
      className={cn(
        "-mb-px shrink-0 border-b-2 border-transparent px-2 py-2.5 text-sm font-medium text-fg-muted transition-colors hover:text-fg data-[state=active]:border-accent data-[state=active]:text-fg sm:px-3",
        className,
      )}
      {...props}
    />
  );
}

export function TabsContent({ className, ...props }: React.ComponentProps<typeof TabsPrimitive.Content>) {
  return <TabsPrimitive.Content className={cn("pt-6 outline-none", className)} {...props} />;
}
