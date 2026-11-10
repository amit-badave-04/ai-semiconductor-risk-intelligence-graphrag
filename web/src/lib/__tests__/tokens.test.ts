import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

// docs/specs/m5b-redesign.md RD1 and A8: the colour tokens of index.css, in both schemes, meet WCAG contrast for every pair a
// component draws, including tokens no component uses yet (axe can only vouch for colours on the page). The government-rule hue
// is never yellow or gold, so a rule is never mistaken for a highlighted quote.

const CSS = readFileSync(path.resolve(import.meta.dirname, "../../index.css"), "utf-8");

function tokens(block: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [, name, value] of block.matchAll(/--([a-z0-9-]+)\s*:\s*(#[0-9a-f]{6})\s*;/gi)) out[name] = value.toLowerCase();
  return out;
}

const LIGHT = tokens(/:root\s*\{([^}]*)\}/.exec(CSS)?.[1] ?? "");
const DARK = tokens(/@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}/.exec(CSS)?.[1] ?? "");

function channel(hex: string, at: number): number {
  const c = Number.parseInt(hex.slice(at, at + 2), 16) / 255;
  return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  return 0.2126 * channel(hex, 1) + 0.7152 * channel(hex, 3) + 0.0722 * channel(hex, 5);
}

function contrast(a: string, b: string): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function hue(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => Number.parseInt(hex.slice(i, i + 2), 16) / 255);
  const max = Math.max(r, g, b);
  const d = max - Math.min(r, g, b);
  if (d === 0) return 0;
  const h = max === r ? ((g - b) / d) % 6 : max === g ? (b - r) / d + 2 : (r - g) / d + 4;
  return (h * 60 + 360) % 360;
}

// [foreground, background] pairs drawn as text (4.5:1) and as a control edge or focus ring (3:1, WCAG 1.4.11).
const TEXT_PAIRS: [string, string][] = [
  ["foreground", "background"],
  ["foreground", "card"],
  ["muted-foreground", "background"],
  ["muted-foreground", "card"],
  ["muted-foreground", "muted"],
  ["primary-foreground", "primary"],
  ["primary", "card"],
  ["primary", "background"],
  ["mark-ink", "mark"],
  ["rule", "rule-fill"],
  ["rule", "card"],
  ["caution", "caution-fill"],
  ["caution", "card"],
  ["destructive", "card"],
];
const EDGE_PAIRS: [string, string][] = [
  ["ring", "background"],
  ["ring", "card"],
  ["input", "card"],
];

describe.each([
  ["light", LIGHT],
  ["dark", DARK],
])("the %s tokens", (_scheme, t) => {
  it("were read from index.css", () => {
    expect(Object.keys(t).length).toBeGreaterThanOrEqual(28); // the colour tokens of RD1, every one in both schemes
  });

  it.each(TEXT_PAIRS)("%s on %s is 4.5:1 or better", (fg, bg) => {
    expect(t[fg], fg).toBeDefined();
    expect(t[bg], bg).toBeDefined();
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(EDGE_PAIRS)("%s against %s is 3:1 or better", (fg, bg) => {
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(3);
  });

  it("gives the government rule a hue outside the yellow and gold band, away from the highlighter", () => {
    const rule = hue(t.rule);
    expect(rule < 20 || rule > 80, `rule hue ${rule}`).toBe(true);
    expect(Math.abs(rule - hue(t.mark))).toBeGreaterThan(60);
  });
});
