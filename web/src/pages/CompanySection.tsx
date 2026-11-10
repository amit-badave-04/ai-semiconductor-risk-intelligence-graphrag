import type { ReactNode } from "react";

/** A section of the Company page (docs/specs/m5b-redesign.md RD21): its heading carries the section's fixed id and takes focus
 *  when the section navigation or a hash names it. */
export function Block({ id, title, children }: { id: string; title: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="mb-8">
      <h2 id={id} tabIndex={-1} className="mb-2 scroll-mt-4 text-lg font-semibold">
        {title}
      </h2>
      {children}
    </section>
  );
}

/** A table that may be wider than the screen scrolls inside this named region, so the page never scrolls sideways (WCAG 1.4.10);
 *  a region that scrolls must be reachable by keyboard (WCAG 2.1.1; axe scrollable-region-focusable), so it takes focus. */
export function ScrollRegion({ label, children }: { label: string; children: ReactNode }) {
  return (
    // oxlint-disable-next-line jsx-a11y/no-noninteractive-tabindex
    <section aria-label={label} tabIndex={0} className="max-w-full overflow-x-auto">
      {children}
    </section>
  );
}
