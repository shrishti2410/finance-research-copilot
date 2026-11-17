import Image from "next/image";

import shots from "@/content/screenshots.json";

export type ShotName = keyof typeof shots.images;

/** A real screenshot (see scripts/capture-screenshots.mjs) in a plain browser
 * frame: three dots and the page's address. */
export function BrowserFrame({
  shot,
  priority = false,
  sizes,
  className = "",
}: {
  shot: ShotName;
  priority?: boolean;
  sizes: string;
  className?: string;
}) {
  const img = shots.images[shot];
  return (
    <figure
      className={`overflow-hidden rounded-[var(--radius-card)] border border-border bg-surface shadow-2xl shadow-black/40 ${className}`}
    >
      <div className="flex items-center gap-3 border-b border-border bg-surface-2 px-3 py-2">
        <span aria-hidden="true" className="flex gap-1.5">
          <span className="size-2.5 rounded-full bg-border" />
          <span className="size-2.5 rounded-full bg-border" />
          <span className="size-2.5 rounded-full bg-border" />
        </span>
        <span className="truncate rounded-md bg-bg px-2 py-0.5 text-[11px] text-fg-subtle">{img.path}</span>
      </div>
      <Image src={img.src} width={img.width} height={img.height} alt={img.alt} sizes={sizes} priority={priority} className="h-auto w-full" />
    </figure>
  );
}

/** A screenshot without the frame, for small feature cards. */
export function Shot({ shot, sizes, className = "" }: { shot: ShotName; sizes: string; className?: string }) {
  const img = shots.images[shot];
  return (
    <Image
      src={img.src}
      width={img.width}
      height={img.height}
      alt={img.alt}
      sizes={sizes}
      className={`h-auto w-full rounded-md border border-border ${className}`}
    />
  );
}
