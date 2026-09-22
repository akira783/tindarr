import { describe, expect, it } from "vitest";

const module = (await import("../scripts/check-csp.mjs")) as {
  inspectHtml: (html: string) => string[];
  inspectCss: (css: string, name: string) => string[];
  inspectJs: (code: string, name: string) => string[];
};

const { inspectCss, inspectHtml, inspectJs } = module;

const builtPage = `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8" />
<link rel="icon" href="/favicon.svg" type="image/svg+xml" />
<title>Tindeerr console</title>
<script type="module" crossorigin src="/assets/index-abc123.js"></script>
<link rel="stylesheet" crossorigin href="/assets/index-def456.css">
</head><body><div id="root"></div></body></html>`;

describe("the CSP check of the built console", () => {
  it("accepts a page whose scripts and styles are hashed files under /assets/", () => {
    expect(inspectHtml(builtPage)).toEqual([]);
  });

  it("catches an inline script, which script-src 'self' refuses", () => {
    expect(inspectHtml(`${builtPage}<script>window.x = 1;</script>`)).toContain(
      "inline <script> in index.html",
    );
  });

  it("catches inline styles and event handlers", () => {
    expect(inspectHtml('<style>body{color:red}</style>')).toContain("inline <style> in index.html");
    expect(inspectHtml('<div style="color:red"></div>')).toContain(
      "style attribute in index.html",
    );
    expect(inspectHtml('<button onclick="go()"></button>')).toContain(
      "inline event handler in index.html",
    );
  });

  it("catches a third-party script or stylesheet and a <base>", () => {
    expect(inspectHtml('<script src="https://cdn.example/app.js"></script>')).toContain(
      "<script src> outside /assets/: https://cdn.example/app.js",
    );
    expect(inspectHtml('<link rel="stylesheet" href="https://fonts.example/x.css">')).toEqual(
      expect.arrayContaining([expect.stringContaining("third-party <link stylesheet>")]),
    );
    expect(inspectHtml("<base href='/'>")).toContain("<base> is refused by base-uri 'none'");
  });

  it("catches a data: URI and a remote import in the CSS", () => {
    expect(inspectCss("a{background:url(data:image/png;base64,AAA)}", "app.css")).toContain(
      "data: URI in app.css",
    );
    expect(inspectCss("@import url(https://fonts.example/x.css);", "app.css")).toEqual(
      expect.arrayContaining([expect.stringContaining("third-party")]),
    );
    expect(inspectCss("a{background:url('/assets/x.svg')}", "app.css")).toEqual([]);
  });

  it("catches eval, new Function and document.write in the bundle", () => {
    expect(inspectJs("const f = eval('1');", "app.js")).toContain("eval() in app.js");
    expect(inspectJs("const f = new Function('return 1');", "app.js")).toContain(
      "new Function() in app.js",
    );
    expect(inspectJs("document.write('x');", "app.js")).toContain("document.write() in app.js");
    expect(inspectJs("const value = obj.evaluate(1);", "app.js")).toEqual([]);
  });
});
