# Known Issues

Limitations and gotchas in `wfb` that are intentional, not yet fixed, or planned for a near-term fix. This file is the canonical reference; the issues here are not repeated in `README.md`.

## Index

- [Virtualized scroll containers are mostly invisible to `wfb chrome` capture](#virtualized-scroll-containers-are-mostly-invisible-to-wfb-chrome-capture)

## Virtualized scroll containers are mostly invisible to `wfb chrome` capture

**Summary.** Pages that embed a virtualized scroll container — Monaco editors, CodeMirror 6 views, react-window / react-virtualized lists, and similar — only keep the currently visible viewport in the DOM. Anything off-screen exists only in the editor's or list's internal model and is not reachable through the page's DOM, accessibility tree, or `body.innerText`.

**Affected commands.** `wfb chrome inspect`, `wfb chrome capture`, `wfb chrome ax`, `wfb chrome find`, and any `wfb bridge ask` / `wfb bridge loop` invocation that captures from such a page. The snapshot looks complete but silently omits most of the editor's content.

**Why each path fails today.**

- `inspect` / `capture` read `document.body.innerText` (see the `inspect_target` JS expression in `wfb_chrome_bridge.py`). Monaco virtualizes to roughly the editor's viewport — typically about 8 rendered `.view-line` elements per editor at rest — so a several-hundred-line document still produces only a few hundred characters of `innerText` for that region.
- `chrome ax` reads `Accessibility.getFullAXTree`, which mirrors the DOM. Monaco regions typically appear as a `code`-role node with `value: null`, `name: null`, and only a handful of descendants for the visible lines. Any `textbox`-role node on the page is usually a plain HTML `<textarea>` elsewhere on the page, not the editor model.
- `wfb chrome find` and raising `--max-chars` cannot recover the missing text because the missing text is not in the DOM at all. `text_snapshot_truncated` in the JSON payload reports whether `wfb` truncated the returned string; it does **not** report whether the DOM was missing content to begin with. A `text_snapshot_truncated: false` result on a Monaco/CodeMirror page is *not* a completeness signal.

**Why a quick `window.monaco` workaround is not reliable.** Many host apps bundle Monaco as an ES module rather than attaching it to the page's `window`. In those cases `typeof window.monaco === "undefined"` even when several Monaco editors are clearly rendered, so a CDP `Runtime.evaluate` of `monaco.editor.getModels()` returns nothing. Agents should not write custom CDP scripts that depend on `window.monaco`; that path is not portable across hosts.

**Other characteristics agents commonly misinterpret.** Each Monaco scroll container reports a `scrollHeight` near 16,777,000 px — this is Monaco's fake virtual scroller, not the real content height. Its native `scrollTop` does **not** reflect Monaco's internal scroll position: `scrollTop` can read `0` while the editor visibly renders a mid-document range, and writes to `scrollTop` are ignored by Monaco's `ScrollableElement`. CodeMirror 6 exhibits the same DOM-only-renders-the-viewport pattern via `.cm-content` / `.cm-line` and the underlying `EditorView` state.

**Planned fix.** A first-class `wfb` command (working name `wfb chrome editors`) that extracts the full text of every virtualized editor on the attached tab, plus a `virtualized_editors_detected` field on `chrome inspect` / `chrome capture` payloads so the limitation is announced in-band rather than discovered by inference. Until that ships, treat any tab containing a Monaco or CodeMirror surface as one whose code/text-editor content is **not** captured by `wfb`.
