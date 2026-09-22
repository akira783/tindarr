/**
 * Fails when the committed API client is not what `api/openapi.yaml` generates.
 * The contract is the source of truth and the client is committed
 * (docs/architecture.md, "Web console"), so CI regenerates it and compares.
 *
 * Usage: node scripts/check-api-client.mjs
 */
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import process from "node:process";

const committed = "src/api/schema.d.ts";
const contract = "../api/openapi.yaml";

const directory = mkdtempSync(join(tmpdir(), "tindarr-api-"));
const generated = join(directory, "schema.d.ts");

try {
  execFileSync(
    process.execPath,
    ["node_modules/openapi-typescript/bin/cli.js", contract, "--output", generated],
    { stdio: ["ignore", "ignore", "inherit"] },
  );

  const expected = readFileSync(generated, "utf8");
  const actual = readFileSync(committed, "utf8");

  if (expected !== actual) {
    console.error(
      `${committed} is out of date with ${contract}. Run \`npm run gen:api\` and commit the result.`,
    );
    process.exit(1);
  }
  console.log(`${committed} matches ${contract}`);
} finally {
  rmSync(directory, { recursive: true, force: true });
}
