# Vendored assets

Both files are embedded into the `asrsub` binary at compile time
(`include_str!` in `src/web.rs`) and served from `/assets/*`. There is no
runtime asset directory in the image.

| file | source | sha256 |
| --- | --- | --- |
| `htmx.min.js` | https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |
| `app.css` | written for this repo | — |

`htmx` is MIT-licensed, © 2020 htmx contributors (https://htmx.org/). It is
vendored — not fetched at runtime — so the dashboard works without network
access. To bump it: download the new version, update the version and sha256 in
this table, re-run `cargo test`, and click through `/` once (the shell relies on
`htmx:configRequest` and `htmx:beforeSwap`).
