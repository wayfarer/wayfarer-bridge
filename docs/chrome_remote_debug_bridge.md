# Chrome Remote Debug Bridge (v1)

`wfb` now includes a stdlib-only Chrome DevTools Protocol bridge for local tab context extraction.

## Why this exists

The official Gemini API surface does not currently expose a direct API to enumerate/attach active Gemini side-panel browser sessions. The Chrome bridge provides a practical local fallback: attach to a browser tab and extract bounded text context for agent workflows.

## Security boundaries

- Default launch mode is isolated Chrome profile: `~/.wfb/chrome_debug_profile/`.
- Existing user profile is opt-in via `--profile-mode user`.
- v1 is read-only extraction (no click/type/navigation automation).
- v1 does not read cookie stores or local storage directly.

## Commands

```sh
wfb chrome launch --profile-mode isolated
wfb chrome targets --include-types page,webview --format text
wfb chrome attach --target-id <id> --include-types page,webview
wfb chrome inspect --include-types page,webview --format json
wfb chrome inspect --selector "main article" --format json
wfb chrome capture --include-types page,webview --format json
wfb chrome ax --format outline
wfb chrome ax --format json --role textbox
wfb chrome find --query "send" --mode both --format json
wfb chrome current
wfb chrome detach
wfb bridge doctor --include-types page,webview
```

Defaults remain backward-compatible: without `--include-types`, `targets`/`attach` resolve page targets only.
`inspect` is attachment-aware: when inspecting a persisted attachment, it auto-includes
the saved attachment type (such as `webview`) unless `--include-types` is explicitly provided.

Ordered debug-port probing also applies to `inspect`, `capture`, `ax`, `find`, `bridge ask`, and `bridge loop`:
the requested `--port` is attempted first (default `9222`), then locally detected fallback ports until `/json/list` succeeds. Persisted attachments may pin a websocket URL from a higher port — `inspect` can still remap the target onto the reachable debug endpoint when probing finds it.
Machine-readable payloads surface `requested_port` vs `resolved_port` (`chrome capture`/`bridge`/`chrome ax`/`chrome find`; `inspect --format json` includes a `debug` object).

## Accessibility Tree (AOM) capture

`wfb chrome ax` reads the page Accessibility Tree using CDP `Accessibility.getFullAXTree`
and renders a screen-reader-style outline that is dramatically more token-efficient than
the flat `body.innerText` snapshot used by `chrome inspect`. It exists to break the
"raise `--max-chars` and re-run" loop that fast agents fell into when a page contained
large amounts of structured but mostly noise-free DOM (long conversation logs, repeated
component shells, etc.).

```
WebArea
  main "Conversation"
    log "Messages"
      paragraph "Hi there"
      paragraph "How can I help?"
    textbox "Compose" focused level=1
    button "Send" disabled
```

Outline rules:

- Ignored AX nodes are skipped by default; their children are emitted at the parent depth
  (set `--ignored on` to include them with explicit ignored markers).
- Long accessible names are truncated to `--name-max-chars` (default `120`) with a
  conservative `"…(+N chars)"` indicator.
- Meaningful AX state properties (`focused`, `selected`, `expanded`, `disabled`,
  `checked`, `pressed`, `level`, `required`, `invalid`, `modal`, `readonly`,
  `multiline`, `autocomplete`) are appended as compact suffixes.
- The total tree is bounded by `--max-nodes` (default `600`); the JSON `outline_meta`
  surfaces `rendered_count`, `total_count`, and `outline_truncated`.
- `--role ROLE` and `--name SUBSTRING` narrow the rendered outline to **subtrees rooted
  at matching nodes**, so an agent can ask only for the `main` landmark or only for the
  `log` region without scanning the rest of the page.
- `--depth N` is forwarded to CDP as `Accessibility.getFullAXTree({depth})` for an
  additional source-side bound on tree size.

JSON output also includes `ax_quality` stats (`meaningful_roles`, `generic_roles`,
`meaningful_ratio`) used by `bridge ... --capture-mode auto` to decide between AOM and
text capture.

## Targeted text scope and search

- `wfb chrome inspect --selector "CSS"` and `wfb chrome capture --selector "CSS"`
  evaluate `document.querySelector(selector)` and only return `innerText` of that
  subtree. When the selector matches nothing, JSON output reports
  `selector_matched: false`, the snapshot is empty, and a recovery hint is written to
  stderr instead of silently falling back to `body.innerText`.
- `wfb chrome find --query STRING [--mode text|aom|both] [--selector CSS] [--role ROLE]
  [--max-results N] [--context-chars N]` searches the page once and returns matches with
  context. Text-mode matches include surrounding context windows, AOM matches include a
  role/name breadcrumb path. The default mode is `both`, which lets agents pose targeted
  questions ("where does the page mention `X`?") instead of re-fetching ever-larger
  snapshots.

## Data flow

1. `launch` first probes for an existing debug endpoint on the requested port; if missing, it probes common fallback ports, then starts Chrome with `--remote-debugging-port` if needed.
2. `targets` reads `/json/list`.
3. `capture` can run full discover -> attach -> inspect with deterministic selection provenance.
4. `attach` stores selected target metadata at `~/.wfb/chrome_attachment.json`.
5. `inspect` probes the debugging endpoint (requested port plus detected ports), opens the resolved websocket when needed, and calls CDP `Runtime.evaluate`.
6. `inspect` returns bounded JSON context (`url`, `title`, `selected_text`, `text_snapshot`) where `text_snapshot` is truncated strictly by `--max-chars`; JSON inspect output includes optional `debug` metadata with `fallback_used`.
7. `current` reports persisted attachment state and endpoint health for recovery/debugging.
8. `bridge doctor` summarizes endpoint health, target inventory, persisted attachment/session signals, and ordered recovery recommendations for blind agents without mutating attachment state.

## Capture selection policy

`wfb chrome capture` resolves a target in this order:

1. Explicit `--target-id` when provided.
2. Focused/active target when present in discovered metadata.
3. Heuristic ranking (prefer non-omnibox targets, then Gemini-signaled targets).
4. First candidate fallback.

## Bridge workflow

`wfb bridge ask` orchestrates the full pipeline:

```
capture (discover -> attach -> inspect/ax) -> prompt envelope -> gemini ask -> combined provenance
```

Each stage reports failures independently (`capture stage failed`, `ask stage failed`) with actionable recovery hints. Use `wfb bridge doctor --format json` when you want a consolidated snapshot plus suggested next CLI commands before running ask/loop.

`wfb bridge loop` extends this into a bounded iterative pipeline:

```
for each iteration (up to --max-iterations):
    capture -> prompt envelope -> gemini ask -> record provenance
    if --stability-check on and snapshot unchanged: stop(no_change)
    if error at any stage: stop(error)
```

Stop reasons are explicit: `max_iterations`, `no_change`, or `error`. Stability comparison uses
the active capture mode's content (text snapshot, AX outline, or both).

### Capture modes and budget metadata

Both `bridge ask` and `bridge loop` accept `--capture-mode text|aom|both|auto` (default
`auto`) plus `--selector CSS`, `--ax-max-nodes N`, and `--ax-name-max-chars N`. The
prompt envelope (`prompt_envelope.template_version: "2"`) carries a `budget` block:

```json
"budget": {
  "capture_mode": "aom",
  "text_snapshot_chars": 0,
  "text_snapshot_truncated": false,
  "ax_total_nodes": 412,
  "ax_rendered_nodes": 412,
  "ax_outline_truncated": false
}
```

The `capture` block also surfaces `mode_requested`, `mode_chosen`, `mode_reason`, the
selector and whether it matched, and `ax_quality` so agents can introspect how their
context was assembled. `auto` mode picks AOM when the page has at least
`AOM_AUTO_MIN_MEANINGFUL_NODES` (5) meaningful AX roles and a
`meaningful_ratio >= 0.3`; otherwise it falls back to the text snapshot.

## Troubleshooting

- If launch fails, pass `--chrome-path` explicitly.
- If target list is empty, verify Chrome is running with remote debugging enabled on the selected port.
- If `inspect` reports missing attachment, run `wfb chrome attach --target-id ...` first.
- If `inspect` still cannot resolve a target, pass explicit `--include-types` to override auto behavior.
- If `chrome inspect`/`capture`/`bridge` reports `selector did not match`, drop the
  `--selector` or relax it; do not raise `--max-chars` to compensate.
- Run `wfb bridge doctor` (JSON by default, or `--format text`) to see reachable ports vs requested port, Gemini-like target counts, and recommended next commands.

## Future scope

The current AOM bridge is intentionally read-only and single-frame. The following
capabilities are documented here so future maintenance can plan against a known roadmap
without rediscovering the same trade-offs.

### Iframe and OOPIF traversal

Out-of-process iframes (OOPIFs) require additional CDP plumbing — typically
`Page.getFrameTree`, per-frame target attachment, and websocket session multiplexing in
`CDPConnection` so a single command can scrape main + child frames. Frame-heavy pages
were judged a low-volume case for the agent workflows that motivated this maintenance
pass: the cost of correctly traversing OOPIFs (per-frame sessions, OOPIF target lifecycle
edge cases, error reporting per frame) is high relative to expected use. When this
becomes important, the likely shape is `--frame main|all|FRAME_ID` on `chrome ax`,
`chrome find`, `chrome inspect`, plus an OOPIF-aware target/session router in
`wfb_chrome_bridge.py`.

### Snapshot cache and pagination

A persisted snapshot cache (`~/.wfb/chrome_last_snapshot.json`) plus
`--offset`/`--limit` or AX node-range pagination would let agents repeatedly query the
same large page without re-paying the CDP round trip. AOM caching is especially cheap
because the normalized representation is small and easy to slice.

### Network/XHR capture

For SPA-heavy pages, the most compact and precise context is often the underlying API
response, not the rendered DOM/AOM. A future `wfb chrome network` could enable
`Network.enable`/`Network.requestWillBeSent`/`Network.responseReceived` for a bounded
window and surface JSON bodies that match a URL substring, with strict size and PII
controls.

### Screenshot / vision mode

`Page.captureScreenshot` would unblock the cases where layout, charts, icons, or
canvas-rendered content matter and AOM/text are insufficient. Likely shape: `wfb chrome
screenshot --format png|base64`, opt-in only, with explicit size limits.

### AOM-backed write-path actions

Persistent element handles (`backendDOMNodeId` plus AX node id) are already returned in
`chrome ax --format json`/`chrome find --format json` output so future commands can
re-target the same element. A natural next step is read-only-by-default browser actions
keyed by accessible role/name (`focus`, `type`, `click`), guarded by a write-path opt-in
and a trust profile (read-only mode, warning gates, domain allowlists). This is
explicitly outside the current maintenance pass.

### AOM quality telemetry

`bridge ask`/`loop` already log `ax_quality.meaningful_ratio` and `mode_reason`. A
future debugging command could surface a richer telemetry view (per-role counts,
ignored-node count, generic-role ratio, and the auto-mode decision boundary) to make
auto-mode choices auditable when they look surprising.

### Fixture capture tooling

Tests currently use synthetic AX nodes. A `wfb chrome ax --capture-fixture PATH` mode
that records sanitized AX/text fixtures from real pages would let regression tests run
against realistic browser payloads without ad-hoc dumps.

### Structured page summaries

A higher-level primitive could group AOM output into landmarks, headings, forms,
messages, and actions so agents get a single page summary rather than walking the
outline.

### Model-aware prompt budgeting

Snapshot size and outline verbosity are currently fixed by `--max-chars`/`--max-nodes`.
A future budget controller could adapt those caps to the selected model's known context
window so agents don't have to guess per model.

### Content redaction

Browser context can leak emails, tokens, long IDs, or sensitive URLs into prompt
envelopes. A future `--redact` profile (regex-driven) would let agents send AOM/text to
Gemini without manually scrubbing first.

### Differential bridge loop

`bridge loop` currently re-sends the full snapshot each iteration. A normalized AX path
hash (role + name + value + state) would let later iterations send only the diff plus a
tiny outline header, dramatically lowering token cost on long-running watches.
