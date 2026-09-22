import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, expect } from "vitest";

import { resetClientHooks, resetFetchImpl, setFetchImpl } from "../src/api/client";
import { setCsrfToken } from "../src/api/csrf";
import { clearCspViolations, cspViolations, installCspGuard } from "./csp-guard";

beforeAll(() => {
  // jsdom has no modal dialog: enough of it for the components under test.
  if (typeof HTMLDialogElement !== "undefined") {
    const proto = HTMLDialogElement.prototype as unknown as {
      showModal?: () => void;
      close?: () => void;
    };
    proto.showModal ??= function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    };
    proto.close ??= function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    };
  }
  installCspGuard();
});

beforeEach(() => {
  clearCspViolations();
  setCsrfToken(null);
  resetClientHooks();
  // No test ever reaches the network: every call goes through a MockApi.
  setFetchImpl(() => {
    throw new Error("unexpected network call: install a MockApi with setFetchImpl()");
  });
});

afterEach(() => {
  cleanup();
  resetFetchImpl();
  resetClientHooks();
  expect(cspViolations(), "the console must not inject markup or inline styles").toEqual([]);
});
