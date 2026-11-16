// In the shadcn/ui pattern (MIT). Hidden from screen readers: the region that
// holds skeletons announces "Loading" itself (see aria-busy at the call site).
import { cn } from "@/lib/utils";

export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-surface-2", className)}
      {...props}
    />
  );
}
