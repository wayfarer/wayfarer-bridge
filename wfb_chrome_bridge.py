"""Stdlib-only Chrome DevTools Protocol bridge helpers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import subprocess
import time
from typing import Any
from urllib import error, parse, request


DEFAULT_DEBUG_HOST = "127.0.0.1"
DEFAULT_DEBUG_PORT = 9222
DEFAULT_TIMEOUT_SECONDS = 5.0
MAX_TEXT_SNAPSHOT_CHARS = 4000
DEFAULT_TARGET_TYPES = ("page",)
SUPPORTED_TARGET_TYPES = frozenset({"page", "webview"})
DEFAULT_PORT_CANDIDATES = (9222, 9223, 9224, 9333)

DEFAULT_AX_MAX_NODES = 600
DEFAULT_AX_NAME_MAX_CHARS = 120
GENERIC_AX_ROLES = frozenset(
    {
        "generic",
        "group",
        "section",
        "none",
        "presentation",
        "GenericContainer",
        "InlineTextBox",
        "LineBreak",
        "RootWebArea",
    }
)
MEANINGFUL_AX_PROPERTY_NAMES = (
    "focused",
    "selected",
    "expanded",
    "disabled",
    "checked",
    "pressed",
    "level",
    "required",
    "invalid",
    "modal",
    "readonly",
    "multiline",
    "autocomplete",
)


class ChromeBridgeError(Exception):
    """Chrome bridge operation failed."""


def chrome_debug_http_url(host: str, port: int, path: str) -> str:
    path_clean = path if path.startswith("/") else f"/{path}"
    return f"http://{host}:{port}{path_clean}"


def _default_mac_chrome_paths() -> list[str]:
    return [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
    ]


def find_chrome_executable(explicit_path: str | None = None) -> str:
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        raise ChromeBridgeError(f"chrome executable not found: {explicit_path}")
    for candidate in _default_mac_chrome_paths():
        if os.path.isfile(candidate):
            return candidate
    raise ChromeBridgeError(
        "Google Chrome executable not found on macOS. "
        "Install Chrome or pass --chrome-path."
    )


def fetch_debug_json(
    *,
    host: str = DEFAULT_DEBUG_HOST,
    port: int = DEFAULT_DEBUG_PORT,
    path: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> Any:
    url = chrome_debug_http_url(host, port, path)
    try:
        with request.urlopen(url, timeout=timeout_seconds) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, error.URLError, json.JSONDecodeError) as exc:
        raise ChromeBridgeError(f"failed to fetch {url}: {exc}") from exc


def fetch_version(**kwargs: Any) -> dict[str, Any]:
    payload = fetch_debug_json(path="/json/version", **kwargs)
    if not isinstance(payload, dict):
        raise ChromeBridgeError("invalid /json/version payload")
    return payload


def detect_debug_ports(
    candidates: tuple[int, ...] = DEFAULT_PORT_CANDIDATES,
    *,
    timeout_seconds: float = 1.0,
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for port in candidates:
        try:
            ver = fetch_version(port=port, timeout_seconds=timeout_seconds)
        except ChromeBridgeError:
            continue
        found.append({"port": int(port), "version": ver})
    return found


def fetch_targets(**kwargs: Any) -> list[dict[str, Any]]:
    payload = fetch_debug_json(path="/json/list", **kwargs)
    if not isinstance(payload, list):
        raise ChromeBridgeError("invalid /json/list payload")
    out: list[dict[str, Any]] = []
    for row in payload:
        if isinstance(row, dict):
            out.append(row)
    return out


def parse_target_types(include_types: str | None) -> tuple[str, ...]:
    if include_types is None or not include_types.strip():
        return DEFAULT_TARGET_TYPES
    types: list[str] = []
    for raw in include_types.split(","):
        item = raw.strip().lower()
        if not item:
            continue
        if item not in SUPPORTED_TARGET_TYPES:
            supported = ", ".join(sorted(SUPPORTED_TARGET_TYPES))
            raise ChromeBridgeError(f"unsupported target type: {item} (supported: {supported})")
        if item not in types:
            types.append(item)
    if not types:
        raise ChromeBridgeError("include-types resolved to empty set")
    return tuple(types)


def list_targets(
    *,
    include_types: tuple[str, ...] | None = None,
    gemini_only: bool = False,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    targets = fetch_targets(**kwargs)
    selected_types = include_types or DEFAULT_TARGET_TYPES
    out: list[dict[str, Any]] = []
    for t in targets:
        ttype = str(t.get("type", "")).lower()
        if ttype not in selected_types:
            continue
        if not t.get("webSocketDebuggerUrl"):
            continue
        if gemini_only:
            url = str(t.get("url", "")).lower()
            title = str(t.get("title", "")).lower()
            if "gemini.google.com/glic" not in url and "gemini" not in title:
                continue
        out.append(t)
    return out


def list_page_targets(**kwargs: Any) -> list[dict[str, Any]]:
    return list_targets(include_types=("page",), gemini_only=False, **kwargs)


def choose_target(targets: list[dict[str, Any]], target_id: str) -> dict[str, Any]:
    for t in targets:
        if str(t.get("id", "")) == target_id:
            return t
    raise ChromeBridgeError(f"target not found: {target_id}")


def select_capture_target(
    targets: list[dict[str, Any]],
    *,
    target_id: str | None = None,
) -> tuple[dict[str, Any], str, str]:
    if not targets:
        raise ChromeBridgeError("no capture candidates found")
    if target_id:
        chosen = choose_target(targets, target_id)
        return chosen, "explicit_id", "selected by --target-id"

    for t in targets:
        if bool(t.get("active")) or bool(t.get("attached")):
            return t, "focused", "selected focused/active target"

    def _score(t: dict[str, Any]) -> int:
        score = 0
        url = str(t.get("url", "")).lower()
        title = str(t.get("title", "")).lower()
        if url.startswith("chrome://omnibox-popup"):
            pass
        elif url.startswith("chrome://"):
            score += 1
        else:
            score += 4
        if "gemini.google.com/glic" in url or "gemini" in title:
            score += 3
        if str(t.get("type", "")).lower() == "webview":
            score += 1
        return score

    scored = sorted(
        ((idx, _score(t), t) for idx, t in enumerate(targets)),
        key=lambda row: (row[1], -row[0]),
        reverse=True,
    )
    _, best_score, chosen = scored[0]
    if best_score > 0:
        return chosen, "heuristic", "selected by heuristic ranking"
    return targets[0], "fallback_first", "selected first candidate fallback"


def _chrome_launch_args(
    *,
    chrome_path: str,
    port: int,
    profile_mode: str,
    profile_dir: str | None,
) -> list[str]:
    args = [
        chrome_path,
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if profile_mode == "isolated":
        if not profile_dir:
            raise ChromeBridgeError("isolated profile mode requires profile_dir")
        args.append(f"--user-data-dir={profile_dir}")
    elif profile_mode != "user":
        raise ChromeBridgeError(f"invalid profile_mode: {profile_mode}")
    args.append("about:blank")
    return args


def launch_chrome_debug(
    *,
    port: int = DEFAULT_DEBUG_PORT,
    profile_mode: str = "isolated",
    profile_dir: str | None = None,
    chrome_path: str | None = None,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    try:
        existing = fetch_version(port=port)
        existing["debug_port"] = port
        existing["requested_port"] = port
        existing["resolved_port"] = port
        existing["fallback_used"] = False
        existing["profile_mode"] = profile_mode
        existing["already_running"] = True
        return existing
    except ChromeBridgeError:
        detected = detect_debug_ports(timeout_seconds=1.0)
        if detected:
            first = detected[0]
            ver = dict(first["version"])
            resolved_port = int(first["port"])
            ver["debug_port"] = resolved_port
            ver["requested_port"] = port
            ver["resolved_port"] = resolved_port
            ver["fallback_used"] = resolved_port != port
            ver["profile_mode"] = profile_mode
            ver["already_running"] = True
            ver["detected_ports"] = [int(d["port"]) for d in detected]
            return ver

    exe = find_chrome_executable(chrome_path)
    args = _chrome_launch_args(
        chrome_path=exe,
        port=port,
        profile_mode=profile_mode,
        profile_dir=profile_dir,
    )
    try:
        subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise ChromeBridgeError(f"failed to launch Chrome: {exc}") from exc

    deadline = time.time() + max(timeout_seconds, 1.0)
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            ver = fetch_version(port=port)
            ver["debug_port"] = port
            ver["requested_port"] = port
            ver["resolved_port"] = port
            ver["fallback_used"] = False
            ver["profile_mode"] = profile_mode
            ver["already_running"] = False
            return ver
        except ChromeBridgeError as exc:
            last_error = exc
            time.sleep(0.2)
    raise ChromeBridgeError(f"Chrome debug endpoint not reachable on port {port}: {last_error}")


def _ws_accept_value(key: str) -> str:
    magic = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
    digest = hashlib.sha1((key + magic).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _recv_until(sock: socket.socket, marker: bytes, *, max_bytes: int = 32768) -> bytes:
    buf = bytearray()
    while marker not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ChromeBridgeError("unexpected EOF while reading websocket handshake")
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ChromeBridgeError("websocket handshake too large")
    return bytes(buf)


def _recv_headers_and_remainder(sock: socket.socket) -> tuple[str, bytearray]:
    raw = _recv_until(sock, b"\r\n\r\n")
    head, tail = raw.split(b"\r\n\r\n", 1)
    return head.decode("iso-8859-1"), bytearray(tail)


def _encode_ws_frame(payload: bytes, *, opcode: int = 0x1, masked: bool = True) -> bytes:
    first = 0x80 | (opcode & 0x0F)  # FIN=1
    out = bytearray([first])
    plen = len(payload)
    mask_bit = 0x80 if masked else 0
    if plen < 126:
        out.append(mask_bit | plen)
    elif plen <= 0xFFFF:
        out.append(mask_bit | 126)
        out.extend(plen.to_bytes(2, "big"))
    else:
        out.append(mask_bit | 127)
        out.extend(plen.to_bytes(8, "big"))

    if masked:
        key = os.urandom(4)
        out.extend(key)
        masked_payload = bytearray(len(payload))
        for i, b in enumerate(payload):
            masked_payload[i] = b ^ key[i % 4]
        out.extend(masked_payload)
    else:
        out.extend(payload)
    return bytes(out)


def _recv_exact(sock: socket.socket, n: int, *, recv_buffer: bytearray | None = None) -> bytes:
    if recv_buffer is None:
        recv_buffer = bytearray()
    out = bytearray()
    if recv_buffer:
        take = min(len(recv_buffer), n)
        out.extend(recv_buffer[:take])
        del recv_buffer[:take]
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ChromeBridgeError("unexpected EOF while reading websocket frame")
        out.extend(chunk)
    return bytes(out)


def _decode_ws_frame(sock: socket.socket, *, recv_buffer: bytearray | None = None) -> tuple[int, bytes]:
    hdr = _recv_exact(sock, 2, recv_buffer=recv_buffer)
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    plen = b1 & 0x7F
    if plen == 126:
        plen = int.from_bytes(_recv_exact(sock, 2, recv_buffer=recv_buffer), "big")
    elif plen == 127:
        plen = int.from_bytes(_recv_exact(sock, 8, recv_buffer=recv_buffer), "big")
    mask_key = _recv_exact(sock, 4, recv_buffer=recv_buffer) if masked else b""
    payload = bytearray(_recv_exact(sock, plen, recv_buffer=recv_buffer))
    if masked:
        for i in range(len(payload)):
            payload[i] ^= mask_key[i % 4]
    return opcode, bytes(payload)


class CDPConnection:
    """Very small CDP websocket client using stdlib sockets."""

    def __init__(self, ws_url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self._url = ws_url
        self._timeout = timeout_seconds
        self._sock: socket.socket | None = None
        self._next_id = 1
        self._recv_buffer = bytearray()

    def __enter__(self) -> "CDPConnection":
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def connect(self) -> None:
        parsed = parse.urlparse(self._url)
        if parsed.scheme != "ws":
            raise ChromeBridgeError(f"unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname or DEFAULT_DEBUG_HOST
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        sock = socket.create_connection((host, port), timeout=self._timeout)
        sock.settimeout(self._timeout)
        sock.sendall(req)
        header_blob, remainder = _recv_headers_and_remainder(sock)
        lines = header_blob.split("\r\n")
        status = lines[0] if lines else ""
        if "101" not in status:
            sock.close()
            raise ChromeBridgeError(f"websocket upgrade failed: {status}")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        expected = _ws_accept_value(key)
        if headers.get("sec-websocket-accept", "") != expected:
            sock.close()
            raise ChromeBridgeError("invalid websocket accept header")
        self._sock = sock
        self._recv_buffer = remainder

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ChromeBridgeError("websocket not connected")
        return self._sock

    def _send_json(self, payload: dict[str, Any]) -> None:
        sock = self._require_socket()
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        sock.sendall(_encode_ws_frame(body, opcode=0x1, masked=True))

    def _recv_json(self) -> dict[str, Any]:
        sock = self._require_socket()
        while True:
            opcode, payload = _decode_ws_frame(sock, recv_buffer=self._recv_buffer)
            if opcode == 0x8:  # close
                raise ChromeBridgeError("websocket closed by remote")
            if opcode == 0x9:  # ping
                sock.sendall(_encode_ws_frame(payload, opcode=0xA, masked=True))
                continue
            if opcode == 0xA:  # pong
                continue
            if opcode != 0x1:
                raise ChromeBridgeError(f"unexpected websocket opcode: {opcode}")
            if not payload:
                continue
            try:
                decoded = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(decoded, dict):
                return decoded

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        msg_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        self._send_json(payload)
        while True:
            msg = self._recv_json()
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise ChromeBridgeError(f"cdp {method} failed: {msg['error']}")
                result = msg.get("result")
                if isinstance(result, dict):
                    return result
                return {}


def inspect_target(
    *,
    ws_url: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_chars: int = MAX_TEXT_SNAPSHOT_CHARS,
    selector: str | None = None,
) -> dict[str, Any]:
    selector_js = json.dumps(selector) if isinstance(selector, str) else "null"
    expression = (
        "(() => {"
        f"const selectorRaw = {selector_js};"
        "const selected = (window.getSelection && window.getSelection()) ? String(window.getSelection()) : '';"
        "let root = null;"
        "let selectorMatched = false;"
        "if (selectorRaw === null) {"
        "  root = document && document.body ? document.body : null;"
        "  selectorMatched = !!root;"
        "} else {"
        "  try {"
        "    root = document.querySelector(selectorRaw);"
        "    selectorMatched = !!root;"
        "  } catch (e) {"
        "    root = null;"
        "    selectorMatched = false;"
        "  }"
        "}"
        "const rootText = root ? (root.innerText || '') : '';"
        "const useSelected = (selectorRaw === null) && selected && selected.trim();"
        "const text = useSelected ? selected : rootText;"
        "return {"
        "  url: String(location.href || ''),"
        "  title: String(document.title || ''),"
        "  selected_text: selected,"
        "  text_snapshot: text,"
        "  selector_matched: selectorMatched"
        "};"
        "})()"
    )

    with CDPConnection(ws_url, timeout_seconds=timeout_seconds) as cdp:
        result = cdp.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
            },
        )
    value = result.get("result", {}).get("value", {})
    if not isinstance(value, dict):
        raise ChromeBridgeError("unexpected Runtime.evaluate response value")
    text = str(value.get("text_snapshot", ""))
    truncated = len(text) > max_chars
    raw_selector_matched = value.get("selector_matched")
    selector_matched: bool | None
    if selector is None:
        selector_matched = None
    elif isinstance(raw_selector_matched, bool):
        selector_matched = raw_selector_matched
    else:
        selector_matched = False
    return {
        "url": str(value.get("url", "")),
        "title": str(value.get("title", "")),
        "selected_text": str(value.get("selected_text", "")),
        "text_snapshot": text[:max_chars],
        "text_snapshot_chars": min(len(text), max_chars),
        "text_snapshot_truncated": truncated,
        "captured_at_unix": int(time.time()),
        "selector": selector,
        "selector_matched": selector_matched,
    }


def get_accessibility_tree(
    *,
    ws_url: str,
    depth: int | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[dict[str, Any]]:
    """Fetch the full accessibility tree for the page and return raw AX nodes.

    Calls Accessibility.enable, getFullAXTree, then best-effort disable.
    """
    params: dict[str, Any] | None = None
    if depth is not None:
        params = {"depth": int(depth)}
    with CDPConnection(ws_url, timeout_seconds=timeout_seconds) as cdp:
        cdp.call("Accessibility.enable")
        try:
            result = cdp.call("Accessibility.getFullAXTree", params)
        finally:
            try:
                cdp.call("Accessibility.disable")
            except ChromeBridgeError:
                pass
    nodes = result.get("nodes")
    if not isinstance(nodes, list):
        raise ChromeBridgeError("Accessibility.getFullAXTree returned no nodes")
    return [n for n in nodes if isinstance(n, dict)]


def _ax_value(field: Any) -> Any:
    if isinstance(field, dict):
        return field.get("value")
    return None


def normalize_ax_node(raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one CDP AXNode into a compact normalized dict."""
    role_value = _ax_value(raw.get("role"))
    name_value = _ax_value(raw.get("name"))
    value_value = _ax_value(raw.get("value"))
    description_value = _ax_value(raw.get("description"))

    properties: list[dict[str, Any]] = []
    raw_props = raw.get("properties")
    if isinstance(raw_props, list):
        for prop in raw_props:
            if not isinstance(prop, dict):
                continue
            p_name = prop.get("name")
            p_value = _ax_value(prop.get("value"))
            if isinstance(p_name, str):
                properties.append({"name": p_name, "value": p_value})

    child_ids: list[str] = []
    raw_children = raw.get("childIds")
    if isinstance(raw_children, list):
        for cid in raw_children:
            if isinstance(cid, str):
                child_ids.append(cid)

    parent_raw = raw.get("parentId")
    parent_id = parent_raw if isinstance(parent_raw, str) and parent_raw else None
    backend_raw = raw.get("backendDOMNodeId")
    backend_dom_node_id = backend_raw if isinstance(backend_raw, int) else None

    return {
        "node_id": str(raw.get("nodeId", "")),
        "parent_id": parent_id,
        "backend_dom_node_id": backend_dom_node_id,
        "role": str(role_value) if isinstance(role_value, str) and role_value else None,
        "name": str(name_value) if isinstance(name_value, str) and name_value else None,
        "value": str(value_value) if isinstance(value_value, str) and value_value else None,
        "description": (
            str(description_value)
            if isinstance(description_value, str) and description_value
            else None
        ),
        "ignored": bool(raw.get("ignored", False)),
        "properties": properties,
        "child_ids": child_ids,
    }


def normalize_ax_tree(raw_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [normalize_ax_node(n) for n in raw_nodes if isinstance(n, dict)]


def filter_ax_nodes(
    nodes: list[dict[str, Any]],
    *,
    role: str | None = None,
    name: str | None = None,
    include_ignored: bool = False,
) -> list[dict[str, Any]]:
    """Return nodes that match role (exact, case-insensitive) and name (substring)."""
    if not nodes:
        return []
    role_match = role.strip().lower() if isinstance(role, str) and role.strip() else None
    name_pattern = name.strip().lower() if isinstance(name, str) and name.strip() else None
    out: list[dict[str, Any]] = []
    for n in nodes:
        if not include_ignored and n.get("ignored"):
            continue
        if role_match is not None:
            r = (n.get("role") or "").lower()
            if r != role_match:
                continue
        if name_pattern is not None:
            nm = (n.get("name") or "").lower()
            if name_pattern not in nm:
                continue
        out.append(n)
    return out


def select_ax_subtrees(
    nodes: list[dict[str, Any]],
    *,
    role: str | None = None,
    name: str | None = None,
    include_ignored: bool = False,
) -> list[dict[str, Any]]:
    """Return nodes for subtrees rooted at nodes matching role/name filters.

    Matched nodes are re-parented to be roots so the renderer treats them as the
    new top-level. Their descendants are kept verbatim.
    """
    role_match = role.strip().lower() if isinstance(role, str) and role.strip() else None
    name_pattern = name.strip().lower() if isinstance(name, str) and name.strip() else None
    if role_match is None and name_pattern is None:
        return nodes
    matched = filter_ax_nodes(
        nodes,
        role=role,
        name=name,
        include_ignored=include_ignored,
    )
    if not matched:
        return []

    by_id = {n["node_id"]: n for n in nodes if n.get("node_id")}
    keep_ids: set[str] = set()

    def collect(node_id: str) -> None:
        if not node_id or node_id in keep_ids:
            return
        keep_ids.add(node_id)
        node = by_id.get(node_id)
        if node is None:
            return
        for cid in node.get("child_ids") or []:
            collect(cid)

    matched_ids: set[str] = set()
    for m in matched:
        nid = m.get("node_id")
        if isinstance(nid, str) and nid:
            matched_ids.add(nid)
            collect(nid)

    out_nodes: list[dict[str, Any]] = []
    for n in nodes:
        nid = n.get("node_id")
        if not isinstance(nid, str) or nid not in keep_ids:
            continue
        if nid in matched_ids:
            new_node = dict(n)
            new_node["parent_id"] = None
            out_nodes.append(new_node)
        else:
            out_nodes.append(n)
    return out_nodes


def _ax_outline_name_token(name: str | None, *, max_chars: int) -> str:
    if not isinstance(name, str) or not name:
        return ""
    flattened = name.replace("\n", " ").replace("\r", " ").strip()
    if not flattened:
        return ""
    if len(flattened) > max_chars:
        truncated = flattened[:max_chars]
        more = len(flattened) - max_chars
        return f'"{truncated}\u2026 (+{more} chars)"'
    return f'"{flattened}"'


def _ax_outline_state_suffix(properties: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for prop in properties or []:
        if not isinstance(prop, dict):
            continue
        p_name = prop.get("name")
        if p_name not in MEANINGFUL_AX_PROPERTY_NAMES:
            continue
        value = prop.get("value")
        if isinstance(value, bool):
            if value:
                parts.append(str(p_name))
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{p_name}={value}")
        elif isinstance(value, str) and value:
            parts.append(f"{p_name}={value}")
    if not parts:
        return ""
    return " " + " ".join(parts)


def render_ax_outline(
    nodes: list[dict[str, Any]],
    *,
    max_nodes: int | None = None,
    name_max_chars: int = DEFAULT_AX_NAME_MAX_CHARS,
    indent_step: str = "  ",
    include_ignored: bool = False,
) -> dict[str, Any]:
    """Render normalized AX nodes into an indented, screen-reader-style outline.

    Ignored nodes are skipped (their children are emitted at the parent depth)
    unless `include_ignored=True`. Returns dict with `text`, `rendered_count`,
    `total_count`, `truncated`.
    """
    if not nodes:
        return {"text": "", "rendered_count": 0, "total_count": 0, "truncated": False}

    by_id: dict[str, dict[str, Any]] = {}
    child_set: set[str] = set()
    for n in nodes:
        nid = n.get("node_id")
        if isinstance(nid, str) and nid:
            by_id[nid] = n
        for cid in n.get("child_ids") or []:
            if isinstance(cid, str):
                child_set.add(cid)

    roots: list[dict[str, Any]] = []
    for n in nodes:
        nid = n.get("node_id")
        if not isinstance(nid, str) or not nid:
            continue
        explicit_parent = n.get("parent_id")
        if explicit_parent in by_id:
            continue
        if not explicit_parent and nid in child_set:
            continue
        roots.append(n)

    lines: list[str] = []
    state = {"rendered": 0, "truncated": False}

    def emit(node: dict[str, Any], depth: int) -> bool:
        if max_nodes is not None and state["rendered"] >= max_nodes:
            state["truncated"] = True
            return False
        ignored = bool(node.get("ignored", False))
        if ignored and not include_ignored:
            for cid in node.get("child_ids") or []:
                child = by_id.get(cid)
                if child is None:
                    continue
                if not emit(child, depth):
                    return False
            return True
        role = node.get("role") or "(no-role)"
        name_token = _ax_outline_name_token(node.get("name"), max_chars=name_max_chars)
        state_suffix = _ax_outline_state_suffix(node.get("properties") or [])
        line = f"{indent_step * depth}{role}"
        if name_token:
            line += f" {name_token}"
        if state_suffix:
            line += state_suffix
        lines.append(line)
        state["rendered"] += 1
        for cid in node.get("child_ids") or []:
            child = by_id.get(cid)
            if child is None:
                continue
            if not emit(child, depth + 1):
                return False
        return True

    for r in roots:
        if not emit(r, 0):
            break

    return {
        "text": "\n".join(lines),
        "rendered_count": state["rendered"],
        "total_count": len(nodes),
        "truncated": state["truncated"],
    }


def ax_quality_stats(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute role-quality stats used by bridge auto-mode."""
    total = len(nodes)
    if total == 0:
        return {
            "total": 0,
            "non_ignored": 0,
            "meaningful_roles": 0,
            "generic_roles": 0,
            "meaningful_ratio": 0.0,
        }
    non_ignored = 0
    meaningful = 0
    generic = 0
    for n in nodes:
        if n.get("ignored"):
            continue
        non_ignored += 1
        role = (n.get("role") or "").strip()
        if not role:
            continue
        if role in GENERIC_AX_ROLES:
            generic += 1
        else:
            meaningful += 1
    ratio = (meaningful / non_ignored) if non_ignored else 0.0
    return {
        "total": total,
        "non_ignored": non_ignored,
        "meaningful_roles": meaningful,
        "generic_roles": generic,
        "meaningful_ratio": ratio,
    }


def find_in_ax_tree(
    nodes: list[dict[str, Any]],
    *,
    query: str,
    role: str | None = None,
    max_results: int | None = None,
    include_ignored: bool = False,
) -> list[dict[str, Any]]:
    """Find AX nodes whose name/value/description contain the query (case-insensitive).

    Returns each match with a role/name breadcrumb path from the AX root.
    """
    if not isinstance(query, str) or not query.strip():
        return []
    q = query.strip().lower()
    role_match = role.strip().lower() if isinstance(role, str) and role.strip() else None

    by_id = {n["node_id"]: n for n in nodes if n.get("node_id")}
    parent_of: dict[str, str] = {}
    for n in nodes:
        nid = n.get("node_id")
        if not isinstance(nid, str) or not nid:
            continue
        for cid in n.get("child_ids") or []:
            if isinstance(cid, str):
                parent_of[cid] = nid

    def path_for(node_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        cur: str | None = node_id
        while cur and cur not in seen:
            seen.add(cur)
            n = by_id.get(cur)
            if n is None:
                break
            out.append({"role": n.get("role"), "name": n.get("name")})
            cur = parent_of.get(cur)
        return list(reversed(out))

    matches: list[dict[str, Any]] = []
    for n in nodes:
        if not include_ignored and n.get("ignored"):
            continue
        if role_match is not None and (n.get("role") or "").lower() != role_match:
            continue
        haystack_parts: list[str] = []
        for key in ("name", "value", "description"):
            val = n.get(key)
            if isinstance(val, str) and val:
                haystack_parts.append(val)
        haystack = " ".join(haystack_parts).lower()
        if not haystack or q not in haystack:
            continue
        nid_raw = n.get("node_id", "")
        nid = nid_raw if isinstance(nid_raw, str) else ""
        matches.append(
            {
                "node_id": nid,
                "role": n.get("role"),
                "name": n.get("name"),
                "value": n.get("value"),
                "backend_dom_node_id": n.get("backend_dom_node_id"),
                "path": path_for(nid),
            }
        )
        if max_results is not None and len(matches) >= max_results:
            break
    return matches


def find_text_matches(
    *,
    text: str,
    query: str,
    max_results: int | None = None,
    context_chars: int = 200,
) -> list[dict[str, Any]]:
    """Find all (case-insensitive) occurrences of `query` in `text` with context windows."""
    if not isinstance(text, str) or not text:
        return []
    if not isinstance(query, str) or not query:
        return []
    if context_chars < 0:
        context_chars = 0
    q_lower = query.lower()
    text_lower = text.lower()
    matches: list[dict[str, Any]] = []
    cursor = 0
    while True:
        idx = text_lower.find(q_lower, cursor)
        if idx == -1:
            break
        end = idx + len(query)
        before_start = max(0, idx - context_chars)
        after_end = min(len(text), end + context_chars)
        matches.append(
            {
                "offset": idx,
                "before": text[before_start:idx],
                "match": text[idx:end],
                "after": text[end:after_end],
            }
        )
        if max_results is not None and len(matches) >= max_results:
            break
        cursor = end if end > cursor else cursor + 1
    return matches
