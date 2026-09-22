/**
 * Checks that the built console can run under the strict CSP of ADR 0009:
 *
 *   default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'
 *   https://image.tmdb.org; connect-src 'self'; font-src 'self';
 *   object-src 'none'; base-uri 'none'; form-action 'self';
 *   frame-ancestors 'none'; require-trusted-types-for 'script'
 *
 * No inline script or style, no `data:` URI, no third-party origin, and every
 * asset under /assets/. Run by `npm run build`; the browser-level check (zero
 * `securitypolicyviolation`) belongs to the end-to-end lot (see e2e/csp.ts).
 *
 * Usage: node scripts/check-csp.mjs <dist directory>
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";

/**
 * @param {string} html
 * @returns {string[]} one message per violation
 */
export function inspectHtml(html) {
  /** @type {string[]} */
  const problems = [];

  for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)) {
    const attributes = match[1] ?? "";
    const body = (match[2] ?? "").trim();
    const src = /\ssrc=["']([^"']+)["']/i.exec(attributes)?.[1];
    if (body !== "") problems.push("inline <script> in index.html");
    if (src === undefined) {
      if (body === "") problems.push("<script> without src");
    } else if (!src.startsWith("/assets/")) {
      problems.push(`<script src> outside /assets/: ${src}`);
    }
  }

  if (/<style\b/i.test(html)) problems.push("inline <style> in index.html");
  if (/\sstyle=["']/i.test(html)) problems.push("style attribute in index.html");
  if (/\son[a-z]+=["']/i.test(html)) problems.push("inline event handler in index.html");

  for (const match of html.matchAll(/<link\b([^>]*)>/gi)) {
    const attributes = match[1] ?? "";
    const rel = /\srel=["']([^"']+)["']/i.exec(attributes)?.[1] ?? "";
    const href = /\shref=["']([^"']+)["']/i.exec(attributes)?.[1] ?? "";
    if (href.startsWith("data:")) problems.push(`data: URI in <link ${rel}>`);
    if (/^[a-z]+:\/\//i.test(href)) problems.push(`third-party <link ${rel}>: ${href}`);
    if ((rel.includes("stylesheet") || rel.includes("modulepreload")) && !href.startsWith("/assets/")) {
      problems.push(`<link ${rel}> outside /assets/: ${href}`);
    }
  }

  if (/<base\b/i.test(html)) problems.push("<base> is refused by base-uri 'none'");

  return problems;
}

/**
 * @param {string} css
 * @param {string} name
 * @returns {string[]}
 */
export function inspectCss(css, name) {
  /** @type {string[]} */
  const problems = [];
  for (const match of css.matchAll(/url\(\s*['"]?([^'")]+)['"]?\s*\)/gi)) {
    const url = match[1] ?? "";
    if (url.startsWith("data:")) problems.push(`data: URI in ${name}`);
    else if (/^[a-z]+:\/\//i.test(url)) problems.push(`third-party url() in ${name}: ${url}`);
  }
  if (/@import\s+url\(\s*['"]?[a-z]+:\/\//i.test(css)) {
    problems.push(`third-party @import in ${name}`);
  }
  return problems;
}

/**
 * @param {string} code
 * @param {string} name
 * @returns {string[]}
 */
export function inspectJs(code, name) {
  /** @type {string[]} */
  const problems = [];
  if (/[^.\w]eval\s*\(/.test(code)) problems.push(`eval() in ${name}`);
  if (/new\s+Function\s*\(/.test(code)) problems.push(`new Function() in ${name}`);
  if (/document\.write\s*\(/.test(code)) problems.push(`document.write() in ${name}`);
  return problems;
}

/** @param {string} dir @returns {string[]} */
function walk(dir) {
  /** @type {string[]} */
  const files = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) files.push(...walk(full));
    else files.push(full);
  }
  return files;
}

/**
 * @param {string} dist
 * @returns {string[]}
 */
export function checkDist(dist) {
  /** @type {string[]} */
  const problems = [];
  const index = join(dist, "index.html");
  problems.push(...inspectHtml(readFileSync(index, "utf8")));

  for (const file of walk(dist)) {
    const name = relative(dist, file);
    if (name.endsWith(".css")) problems.push(...inspectCss(readFileSync(file, "utf8"), name));
    else if (name.endsWith(".js")) problems.push(...inspectJs(readFileSync(file, "utf8"), name));
    else if (name.endsWith(".html") && name !== "index.html") {
      problems.push(`unexpected HTML file in the build: ${name}`);
    }
  }

  return problems;
}

const entry = process.argv[1];
if (entry !== undefined && import.meta.url === pathToFileURL(entry).href) {
  const dist = process.argv[2] ?? "dist";
  const problems = checkDist(dist);
  if (problems.length > 0) {
    console.error(`CSP check failed for ${dist}:`);
    for (const problem of problems) console.error(` - ${problem}`);
    process.exit(1);
  }
  console.log(`CSP check passed for ${dist}`);
}
