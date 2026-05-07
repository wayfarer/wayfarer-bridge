# Gemini Session Discovery

This note records what `wfb` has validated about official Gemini API session support.

## Decision Gate Result

- **API-managed reusable conversation/session handles:** unsupported in the REST surface currently used by `wfb`.
- **Direct browser side-panel session attach:** unsupported by the endpoint probes validated so far.

## API-Managed Conversation State

Checked the REST endpoints already used by `wfb`:

- `GET /v1/models`
- `POST /v1beta/models/{model}:generateContent`

Observed behavior:

- Requests accept content turns and optional system instruction.
- Responses return generated candidates/content.
- No stable conversation/session id or handle is returned for later reuse.
- No companion endpoint in this validated surface exposes create/list/use chat handles.

## Browser Session Attachment Probes

Validated whether official Gemini REST APIs expose browser-session attachment primitives:

| Capability | Endpoint Probe | Result |
|---|---|---|
| `list_sessions` | `/v1/sessions/probe` | Unsupported (HTTP 404) |
| `get_session_metadata` | `/v1/sessions/probe` | Unsupported (HTTP 404) |
| `session_tab_context` | `/v1/sessions/probe:tabContext` | Unsupported (HTTP 404) |
| `attach_to_existing_browser_session` | `/v1/sessions/probe:attach` | Unsupported (HTTP 404) |

Additional probes also returned HTTP 404 during discovery:

- `/v1/chats`
- `/v1beta/chats`
- `/v1/conversations`
- `/v1beta/conversations`

## Current Fallback

`wfb` uses local session memory under `~/.wfb/`:

- `gemini_sessions/<session_id>.json` stores turn history and metadata.
- `gemini_active_session.json` stores the active local session pointer.

Current commands:

- `wfb gemini session current`
- `wfb gemini session list`
- `wfb gemini session new [--name ...] [--model ...] [--system ...]`
- `wfb gemini session use --id ...`
- `wfb gemini session reset [--id ...]`
- `wfb gemini session inspect [--id ...] [--format text|json]`

This fallback gives terminal agents deterministic continuity while preserving the project constraint of no external Python dependencies.
