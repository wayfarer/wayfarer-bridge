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
wfb chrome capture --include-types page,webview --format json
wfb chrome current
wfb chrome detach
```

Defaults remain backward-compatible: without `--include-types`, `targets`/`attach` resolve page targets only.
`inspect` is attachment-aware: when inspecting a persisted attachment, it auto-includes
the saved attachment type (such as `webview`) unless `--include-types` is explicitly provided.

## Data flow

1. `launch` first probes for an existing debug endpoint on the requested port; if missing, it probes common fallback ports, then starts Chrome with `--remote-debugging-port` if needed.
2. `targets` reads `/json/list`.
3. `capture` can run full discover -> attach -> inspect with deterministic selection provenance.
4. `attach` stores selected target metadata at `~/.wfb/chrome_attachment.json`.
5. `inspect` opens the target websocket and calls CDP `Runtime.evaluate`.
6. `inspect` returns bounded JSON context (`url`, `title`, `selected_text`, `text_snapshot`) where `text_snapshot` is truncated strictly by `--max-chars`.
7. `current` reports persisted attachment state and endpoint health for recovery/debugging.

## Capture selection policy

`wfb chrome capture` resolves a target in this order:

1. Explicit `--target-id` when provided.
2. Focused/active target when present in discovered metadata.
3. Heuristic ranking (prefer non-omnibox targets, then Gemini-signaled targets).
4. First candidate fallback.

## Bridge workflow

`wfb bridge ask` orchestrates the full pipeline:

```
capture (discover -> attach -> inspect) -> prompt envelope -> gemini ask -> combined provenance
```

Each stage reports failures independently (`capture stage failed`, `ask stage failed`) with actionable recovery hints.

`wfb bridge loop` extends this into a bounded iterative pipeline:

```
for each iteration (up to --max-iterations):
    capture -> prompt envelope -> gemini ask -> record provenance
    if --stability-check on and snapshot unchanged: stop(no_change)
    if error at any stage: stop(error)
```

Stop reasons are explicit: `max_iterations`, `no_change`, or `error`.

## Troubleshooting

- If launch fails, pass `--chrome-path` explicitly.
- If target list is empty, verify Chrome is running with remote debugging enabled on the selected port.
- If `inspect` reports missing attachment, run `wfb chrome attach --target-id ...` first.
- If `inspect` still cannot resolve a target, pass explicit `--include-types` to override auto behavior.
