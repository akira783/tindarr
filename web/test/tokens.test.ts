/**
 * The design tokens, held to what the design actually hands over.
 *
 * Three things can drift and none of them would show up in a render test:
 *
 * 1. `shared/design/tokens.css` against the hand-off in `design/tokens.css`;
 * 2. `shared/design/tokens.json` — what the Android theme will read (roadmap
 *    step 7) — against that same CSS;
 * 3. the light theme's two entry points, `[data-theme="light"]` and
 *    `prefers-color-scheme: light`, against each other.
 *
 * And the contrast ratios the tokens claim in their comments are measured here
 * rather than believed: WCAG AA is a promise the console makes in both themes
 * (design/brief.md, "Constraints that are not negotiable").
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import process from "node:process";

import { describe, expect, it } from "vitest";

// Vitest runs from `web/`; the repository root is one above it. Built with `resolve`
// rather than `new URL(…, import.meta.url)`, which Vite would turn into an asset.
function read(relative: string): string {
  return readFileSync(resolve(process.cwd(), "..", relative), "utf8");
}

const handoff = read("design/tokens.css");
const shared = read("shared/design/tokens.css");
const asData = JSON.parse(read("shared/design/tokens.json")) as {
  color: Record<"dark" | "light", Record<string, string>> & { scrim: string };
  shadow: Record<"dark" | "light", Record<string, string>>;
  font: Record<string, string>;
  text: Record<string, string>;
  space: Record<string, string>;
  radius: Record<string, string>;
  motion: Record<string, string>;
};

/** Every custom property declared in the first block matching `header`. */
function declarations(css: string, header: RegExp): Map<string, string> {
  const start = css.search(header);
  if (start < 0) throw new Error(`no block for ${String(header)}`);
  let depth = 0;
  let end = start;
  for (let index = css.indexOf("{", start); index < css.length; index += 1) {
    const char = css[index];
    if (char === "{") depth += 1;
    else if (char === "}") {
      depth -= 1;
      if (depth === 0) {
        end = index;
        break;
      }
    }
  }
  const body = css.slice(css.indexOf("{", start) + 1, end);
  const found = new Map<string, string>();
  for (const match of body.matchAll(/(--[a-z0-9-]+)\s*:\s*([^;]+);/g)) {
    found.set(match[1] ?? "", (match[2] ?? "").split(/\s+/).join(" ").trim());
  }
  return found;
}

const sharedRoot = declarations(shared, /^:root \{/m);
const sharedLight = declarations(shared, /^:root\[data-theme="light"\] \{/m);
const sharedQueried = declarations(shared, /^\s*:root:not\(\[data-theme="dark"\]\) \{/m);

describe("the tokens the two clients share", () => {
  it("keeps every value the design handed over", () => {
    for (const [header, live] of [
      [/^:root \{/m, sharedRoot],
      [/^:root\[data-theme="light"\] \{/m, sharedLight],
    ] as const) {
      for (const [name, value] of declarations(handoff, header)) {
        expect(`${name}: ${live.get(name) ?? "missing"}`).toBe(`${name}: ${value}`);
      }
    }
  });

  it("says the same thing to a client that reads JSON rather than CSS", () => {
    const scalars: Record<string, string> = {
      ...asData.font,
      ...asData.text,
      ...asData.space,
      ...asData.radius,
      ...asData.motion,
    };
    for (const [name, value] of Object.entries(scalars)) {
      expect(`${name}=${sharedRoot.get(`--${name}`) ?? "missing"}`).toBe(`${name}=${value}`);
    }
    for (const [name, value] of Object.entries(asData.color.dark)) {
      expect(`${name}=${sharedRoot.get(`--${name}`) ?? "missing"}`).toBe(`${name}=${value}`);
    }
    for (const [name, value] of Object.entries(asData.color.light)) {
      const live = sharedLight.get(`--${name}`) ?? sharedRoot.get(`--${name}`);
      expect(`${name}=${live ?? "missing"}`).toBe(`${name}=${value}`);
    }
    for (const [name, value] of Object.entries(asData.shadow.dark)) {
      expect(sharedRoot.get(`--${name}`)).toBe(value);
    }
    expect(sharedRoot.get("--scrim")).toBe(asData.color.scrim);
  });

  it("gives the light theme the same values by preference as by choice", () => {
    expect(Object.fromEntries(sharedQueried)).toEqual(Object.fromEntries(sharedLight));
  });
});

// --- contrast, measured -------------------------------------------------------------

function channel(value: number): number {
  const srgb = value / 255;
  return srgb <= 0.040_45 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (match === null) throw new Error(`not a hex colour: ${hex}`);
  const digits = match[1] ?? "";
  const [red, green, blue] = [0, 2, 4].map((offset) =>
    Number.parseInt(digits.slice(offset, offset + 2), 16),
  ) as [number, number, number];
  return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue);
}

export function contrast(foreground: string, background: string): number {
  const [light, dark] = [luminance(foreground), luminance(background)].sort((a, b) => b - a) as [
    number,
    number,
  ];
  return (light + 0.05) / (dark + 0.05);
}

/** `[ink, paper, floor]`, the floor being 4.5 for text and 3 for a shape or a border. */
const PAIRS: readonly (readonly [string, string, number])[] = [
  ["text", "bg", 7],
  ["text", "surface", 7],
  ["text", "surface-2", 7],
  ["muted", "bg", 4.5],
  ["muted", "surface", 4.5],
  ["muted", "surface-2", 4.5],
  ["accent-ink", "bg", 4.5],
  ["accent-ink", "surface", 4.5],
  ["bet-ink", "bg", 4.5],
  ["bet-ink", "surface", 4.5],
  ["success", "bg", 4.5],
  ["success", "surface", 4.5],
  ["warning", "bg", 4.5],
  ["warning", "surface", 4.5],
  ["danger", "bg", 4.5],
  ["danger", "surface", 4.5],
  ["on-accent", "accent", 4.5],
  // Not text: the focus ring and the outline that identifies a control (WCAG
  // 1.4.11). Every button and field carries --control-border, including the
  // filled one — in the light theme --accent reads at 2,1:1 against --surface,
  // so the fill alone could not be what says "this is a button".
  ["focus", "bg", 3],
  ["focus", "surface", 3],
  ["control-border", "bg", 3],
  ["control-border", "surface", 3],
  ["control-border", "surface-2", 3],
  ["bet", "surface", 3],
];

/** `over` seen through `alpha` of `ink`: what the scrim actually leaves. */
function composite(ink: string, alpha: number, over: string): string {
  const parse = (hex: string): [number, number, number] =>
    [0, 2, 4].map((offset) => Number.parseInt(hex.slice(1 + offset, 3 + offset), 16)) as [
      number,
      number,
      number,
    ];
  const [a, b] = [parse(ink), parse(over)];
  const mix = a.map((value, index) => Math.round(value * alpha + (b[index] ?? 0) * (1 - alpha)));
  return `#${mix.map((value) => value.toString(16).padStart(2, "0")).join("")}`;
}

describe.each(["dark", "light"] as const)("contrast in the %s theme", (theme) => {
  const palette = asData.color[theme];

  it.each(PAIRS)("%s on %s reaches %d:1", (ink, paper, floor) => {
    const ratio = contrast(palette[ink] ?? "", palette[paper] ?? "");
    // The ratio is in the failure message, so a regression says by how much.
    expect([`${ink} on ${paper}`, Math.round(ratio * 10) / 10 >= floor]).toEqual([
      `${ink} on ${paper}`,
      true,
    ]);
  });

  it("keeps the text over a poster readable whatever the poster is", () => {
    // The worst poster is a white one. The card's scrim is at least 86 % opaque
    // where the text sits, so that is what the reading has to survive.
    const worst = composite("#0a0806", 0.86, "#ffffff");
    expect(contrast(palette["on-scrim"] ?? "", worst)).toBeGreaterThanOrEqual(4.5);
  });
});
