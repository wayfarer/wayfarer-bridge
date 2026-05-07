# Gemini API-Managed State Check

## Decision gate result

- **API-managed reusable conversation/session handles: unsupported (current client surface).**

## Evidence

Checked the REST endpoints already used by `wfb`:

- `GET /v1/models`
- `POST /v1beta/models/{model}:generateContent`

Observed behavior:

- Requests accept content turns and optional system instruction.
- Responses return generated candidates/content.
- No stable conversation/session id/handle is returned for reuse in later calls.
- No documented companion endpoint in this surface exposes create/list/use session handles.

## Outcome for bridge design

`wfb` uses local session memory under `~/.wfb/`:

- `gemini_sessions/<session_id>.json` stores turn history and metadata.
- `gemini_active_session.json` stores active session pointer.

This yields deterministic, agent-usable continuity while remaining stdlib-only.
# Gemini Session Discovery Report

## Scope

Validate whether official Gemini REST APIs expose browser-session attachment primitives needed for the bridge:

- `list_sessions`
- `get_session_metadata`
- `session_tab_context`
- `attach_to_existing_browser_session`

## Probing method

- Authenticated with existing `wfb` OAuth token.
- Probed likely REST paths under `https://generativelanguage.googleapis.com`.
- Captured HTTP status and response detail.

## Capability matrix

| Capability | Endpoint Probe | Result |
|---|---|---|
| `list_sessions` | `/v1/sessions/probe` | Unsupported (HTTP 404) |
| `get_session_metadata` | `/v1/sessions/probe` | Unsupported (HTTP 404) |
| `session_tab_context` | `/v1/sessions/probe:tabContext` | Unsupported (HTTP 404) |
| `attach_to_existing_browser_session` | `/v1/sessions/probe:attach` | Unsupported (HTTP 404) |

Additional probes (`/v1/chats`, `/v1beta/chats`, `/v1/conversations`, `/v1beta/conversations`) also returned HTTP 404 during discovery.

## Decision gate outcome

- **Direct official API attach is currently not supported** by the endpoints we can validate.
- Bridge should proceed with a fallback design for browser-session mapping (extension bridge, browser automation, or explicit/manual mapping).

## Current bridge fallback implemented

- `wfb gemini sessions list` prints discovery matrix.
- `wfb gemini sessions attach --id ...` stores selected session reference locally at `~/.wfb/session_attachment.json`.
- `wfb gemini sessions inspect` shows stored attachment reference.

## Manual verification checklist

1. Run `wfb gemini sessions list` and confirm matrix output appears.
2. Confirm unsupported capabilities currently report non-success status.
3. Run `wfb gemini sessions attach --id my-session-id`.
4. Run `wfb gemini sessions inspect` and confirm stored `session_id`.
5. Confirm file exists at `~/.wfb/session_attachment.json`.
