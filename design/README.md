# design/

What a design pass hands over, kept as it was handed over. **Nothing here is built or
served**: the console reads `shared/design/tokens.css`, not this folder.

| File | What it is |
|---|---|
| `brief.md` | The brief the design was made against: the product, the direction, the screens, and the constraints a designer cannot guess (strict CSP, keyboard, contrast, 320 px). |
| `tokens.css` | **The hand-off.** `web/test/tokens.test.ts` compares `shared/design/tokens.css` and `shared/design/tokens.json` against this file and fails when they drift, so a token cannot quietly change in the console and lose its design intent. Do not delete it as a duplicate — it is the reference the duplicate is checked against. |
| `Tindarr Refonte.dc.html`, `support.js` | The canvas: artboards and component specs. Open the HTML in a browser to read them. |
| `github.md` | The screen map the canvas wrote: which artboard corresponds to which repo file. |
| `screenshots/before/`, `screenshots/after/` | The same pages, same data, before and after the redesign, at desktop and phone width, in both themes. |

## Changing a token

Change it in **all three** places — `design/tokens.css`, `shared/design/tokens.css`,
`shared/design/tokens.json` — or the test fails. That is deliberate: it keeps the shipped
values, the data the Android app will read, and the design's own record from telling three
different stories.

One value deliberately differs from the hand-off: `--control-border` was added because the
design's `--border-strong` measures 2.3:1 on `--surface`, below the 3:1 a control's outline
needs. The reasoning is in the token file itself.
