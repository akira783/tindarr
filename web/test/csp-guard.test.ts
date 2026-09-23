/**
 * The guard has to catch an inline style whatever set it.
 *
 * React never calls `setAttribute("style", …)`: it writes through the `style` object.
 * A guard that only traps `setAttribute` therefore reports nothing at all, and the
 * suite's "no inline styles" assertion passes without ever having looked.
 */
import { expect, test } from "vitest";

import { clearCspViolations, cspViolations, scanForInlineStyles } from "./csp-guard";

test("an inline style written the way React writes it is reported", () => {
  const element = document.createElement("div");
  document.body.append(element);
  try {
    element.style.color = "red";
    scanForInlineStyles(document);
    expect(cspViolations()).toContain("inline style on <div>");
  } finally {
    element.remove();
    clearCspViolations();
  }
});

test("a tree without inline styles reports nothing", () => {
  const element = document.createElement("p");
  element.className = "hint";
  document.body.append(element);
  try {
    scanForInlineStyles(document);
    expect(cspViolations()).toEqual([]);
  } finally {
    element.remove();
  }
});
