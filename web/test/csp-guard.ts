/**
 * Approximates the console's CSP inside jsdom (docs/adr/0009):
 * `require-trusted-types-for 'script'` and `style-src 'self'` forbid injecting
 * markup or inline styles. Anything the components do that a browser would
 * report as a violation is recorded here and fails the test.
 *
 * The real check, on a real browser with the real header, is the end-to-end
 * lot (see `e2e/csp.ts`).
 */
const violations: string[] = [];

type Restore = () => void;

const restores: Restore[] = [];

function guardProperty(target: object, name: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(target, name);
  if (descriptor?.set === undefined) return;
  const original = descriptor.set;
  Object.defineProperty(target, name, {
    ...descriptor,
    set(this: unknown, value: unknown) {
      violations.push(`${name} assigned`);
      original.call(this, value);
    },
  });
  restores.push(() => {
    Object.defineProperty(target, name, descriptor);
  });
}

function guardMethod(target: object, name: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(target, name);
  if (descriptor === undefined || typeof descriptor.value !== "function") return;
  const original = descriptor.value as (...args: unknown[]) => unknown;
  Object.defineProperty(target, name, {
    ...descriptor,
    value(this: unknown, ...args: unknown[]) {
      violations.push(`${name} called`);
      return original.apply(this, args);
    },
  });
  restores.push(() => {
    Object.defineProperty(target, name, descriptor);
  });
}

export function installCspGuard(): void {
  guardProperty(Element.prototype, "innerHTML");
  guardProperty(Element.prototype, "outerHTML");
  guardMethod(Element.prototype, "insertAdjacentHTML");
  guardMethod(document, "write");

  const setAttribute = Element.prototype.setAttribute;
  Element.prototype.setAttribute = function (this: Element, name: string, value: string) {
    if (name.toLowerCase() === "style") violations.push("style attribute set");
    setAttribute.call(this, name, value);
  };
  restores.push(() => {
    Element.prototype.setAttribute = setAttribute;
  });

}

export function uninstallCspGuard(): void {
  while (restores.length > 0) restores.pop()?.();
}

export function cspViolations(): readonly string[] {
  return violations;
}

export function clearCspViolations(): void {
  violations.length = 0;
}
