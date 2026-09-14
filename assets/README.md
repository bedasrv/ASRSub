# Embedded dashboard assets

Both files are embedded into the `asrsub` binary at compile time
(`include_str!` in `src/web/mod.rs`) and served from `/assets/*`. There is no
runtime asset directory in the image.

| file | purpose |
| --- | --- |
| `app.js` | session-only control-key display and URL-encoded authenticated form submission |
| `app.css` | dashboard layout and light/dark styling |

The dashboard uses ordinary browser navigation and complete server-rendered
HTML pages. `app.js` is intentionally small: it keeps the control key in
`sessionStorage`, sends it only as `X-API-Key`, disables a submitted form while
the request is in flight, follows successful `303` responses, and replaces the
document with complete HTML for error responses. It has no framework/runtime
dependency and is not fetched from the network.
