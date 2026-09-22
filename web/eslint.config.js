import js from "@eslint/js";
import react from "@eslint-react/eslint-plugin";
import { defineConfig, globalIgnores } from "eslint/config";
import jsxA11y from "eslint-plugin-jsx-a11y-x";
import reactHooks from "eslint-plugin-react-hooks";
import globals from "globals";
import tseslint from "typescript-eslint";

export default defineConfig([
  globalIgnores(["dist", "coverage", "src/api/schema.d.ts"]),
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  tseslint.configs.stylisticTypeChecked,
  react.configs["recommended-typescript"],
  reactHooks.configs.flat["recommended-latest"],
  jsxA11y.configs.strict,
  {
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: {
        projectService: {
          allowDefaultProject: ["eslint.config.js", "scripts/*.mjs"],
        },
        tsconfigRootDir: import.meta.dirname,
      },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      // The console renders server data as text only (docs/security.md, section 4).
      "no-restricted-properties": [
        "error",
        { property: "innerHTML", message: "Forbidden by the console's CSP (Trusted Types)." },
        { property: "outerHTML", message: "Forbidden by the console's CSP (Trusted Types)." },
        {
          property: "insertAdjacentHTML",
          message: "Forbidden by the console's CSP (Trusted Types).",
        },
        { object: "document", property: "write", message: "Forbidden by the console's CSP." },
      ],
      "no-eval": "error",
      "no-implied-eval": "off",
      "@typescript-eslint/no-implied-eval": "error",
      "no-new-func": "error",
      // No credential ever reaches storage a script can read (docs/adr/0009).
      "no-restricted-globals": [
        "error",
        {
          name: "localStorage",
          message: "Only src/lib/prefs.ts may use it, and only for display preferences.",
        },
        { name: "sessionStorage", message: "Never used: no token is kept in the browser." },
        { name: "indexedDB", message: "Never used: no token is kept in the browser." },
        {
          name: "fetch",
          message: "Call the API through src/api/client.ts, which adds the CSRF token.",
        },
      ],
      "@typescript-eslint/consistent-type-imports": ["error", { fixStyle: "inline-type-imports" }],
      "@typescript-eslint/restrict-template-expressions": [
        "error",
        { allowNumber: true, allowBoolean: false, allowNullish: false },
      ],
    },
  },
  {
    // Generic helpers whose type parameter exists to type-check the call site.
    files: ["test/mock-api.ts"],
    rules: { "@typescript-eslint/no-unnecessary-type-parameters": "off" },
  },
  {
    // Monkey-patches DOM prototypes on purpose, to catch what the CSP forbids.
    files: ["test/csp-guard.ts"],
    rules: { "@typescript-eslint/unbound-method": "off" },
  },
  {
    files: ["src/lib/prefs.ts"],
    rules: { "no-restricted-globals": "off" },
  },
  {
    files: ["src/api/client.ts", "test/**", "src/**/*.test.ts", "src/**/*.test.tsx"],
    rules: { "no-restricted-globals": "off" },
  },
  {
    files: ["**/*.test.ts", "**/*.test.tsx", "test/**", "e2e/**"],
    rules: {
      "@typescript-eslint/no-non-null-assertion": "off",
      "@typescript-eslint/no-unsafe-assignment": "off",
    },
  },
  {
    files: ["**/*.js", "**/*.mjs"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: { ...globals.node } },
  },
  {
    files: ["vite.config.ts", "scripts/**"],
    languageOptions: { globals: { ...globals.node } },
    rules: { "no-restricted-globals": "off" },
  },
]);
