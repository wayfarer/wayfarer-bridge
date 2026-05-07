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
wfb chrome current
wfb chrome detach
```

Defaults remain backward-compatible: without `--include-types`, `targets`/`attach`/`inspect` resolve page targets only.

## Data flow

1. `launch` first probes for an existing debug endpoint on the requested port; if missing, it probes common fallback ports, then starts Chrome with `--remote-debugging-port` if needed.
2. `targets` reads `/json/list`.
3. `attach` stores selected target metadata at `~/.wfb/chrome_attachment.json`.
4. `inspect` opens the target websocket and calls CDP `Runtime.evaluate`.
5. `inspect` returns bounded JSON context (`url`, `title`, `selected_text`, `text_snapshot`) where `text_snapshot` is truncated strictly by `--max-chars`.
6. `current` reports persisted attachment state and endpoint health for recovery/debugging.

## Troubleshooting

- If launch fails, pass `--chrome-path` explicitly.
- If target list is empty, verify Chrome is running with remote debugging enabled on the selected port.
- If `inspect` reports missing attachment, run `wfb chrome attach --target-id ...` first.
