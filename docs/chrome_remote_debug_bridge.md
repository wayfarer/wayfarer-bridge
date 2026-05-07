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
wfb chrome targets --format text
wfb chrome attach --target-id <id>
wfb chrome inspect --format json
wfb chrome detach
```

## Data flow

1. `launch` starts Chrome with `--remote-debugging-port`.
2. `targets` reads `/json/list`.
3. `attach` stores selected target metadata at `~/.wfb/chrome_attachment.json`.
4. `inspect` opens the target websocket and calls CDP `Runtime.evaluate`.
5. `inspect` returns bounded JSON context (`url`, `title`, `selected_text`, `text_snapshot`).

## Troubleshooting

- If launch fails, pass `--chrome-path` explicitly.
- If target list is empty, verify Chrome is running with remote debugging enabled on the selected port.
- If `inspect` reports missing attachment, run `wfb chrome attach --target-id ...` first.
