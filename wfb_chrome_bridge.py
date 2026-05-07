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


def fetch_targets(**kwargs: Any) -> list[dict[str, Any]]:
    payload = fetch_debug_json(path="/json/list", **kwargs)
    if not isinstance(payload, list):
        raise ChromeBridgeError("invalid /json/list payload")
    out: list[dict[str, Any]] = []
    for row in payload:
        if isinstance(row, dict):
            out.append(row)
    return out


def list_page_targets(**kwargs: Any) -> list[dict[str, Any]]:
    targets = fetch_targets(**kwargs)
    pages: list[dict[str, Any]] = []
    for t in targets:
        if str(t.get("type", "")) != "page":
            continue
        if not t.get("webSocketDebuggerUrl"):
            continue
        pages.append(t)
    return pages


def choose_target(targets: list[dict[str, Any]], target_id: str) -> dict[str, Any]:
    for t in targets:
        if str(t.get("id", "")) == target_id:
            return t
    raise ChromeBridgeError(f"target not found: {target_id}")


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
            ver["profile_mode"] = profile_mode
            return ver
        except ChromeBridgeError as exc:
            last_error = exc
            time.sleep(0.2)
    raise ChromeBridgeError(f"Chrome debug endpoint not reachable on port {port}: {last_error}")


def verify_debug_endpoint(
    *, port: int = DEFAULT_DEBUG_PORT, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> dict[str, Any]:
    ver = fetch_version(port=port, timeout_seconds=timeout_seconds)
    ver["debug_port"] = port
    return ver


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


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ChromeBridgeError("unexpected EOF while reading websocket frame")
        out.extend(chunk)
    return bytes(out)


def _decode_ws_frame(sock: socket.socket) -> tuple[int, bytes]:
    hdr = _recv_exact(sock, 2)
    b0, b1 = hdr[0], hdr[1]
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    plen = b1 & 0x7F
    if plen == 126:
        plen = int.from_bytes(_recv_exact(sock, 2), "big")
    elif plen == 127:
        plen = int.from_bytes(_recv_exact(sock, 8), "big")
    mask_key = _recv_exact(sock, 4) if masked else b""
    payload = bytearray(_recv_exact(sock, plen))
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
        raw_resp = _recv_until(sock, b"\r\n\r\n")
        header_blob = raw_resp.split(b"\r\n\r\n", 1)[0].decode("iso-8859-1")
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
            opcode, payload = _decode_ws_frame(sock)
            if opcode == 0x8:  # close
                raise ChromeBridgeError("websocket closed by remote")
            if opcode == 0x9:  # ping
                sock.sendall(_encode_ws_frame(payload, opcode=0xA, masked=True))
                continue
            if opcode != 0x1:
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
) -> dict[str, Any]:
    expression = """
(() => {
  const selected = (window.getSelection && window.getSelection()) ? String(window.getSelection()) : "";
  const bodyText = document && document.body ? (document.body.innerText || "") : "";
  const text = selected && selected.trim() ? selected : bodyText;
  return {
    url: String(location.href || ""),
    title: String(document.title || ""),
    selected_text: selected.slice(0, 2000),
    text_snapshot: text.slice(0, 20000)
  };
})()
""".strip()

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
    return {
        "url": str(value.get("url", "")),
        "title": str(value.get("title", "")),
        "selected_text": str(value.get("selected_text", "")),
        "text_snapshot": text[:max_chars],
        "text_snapshot_chars": min(len(text), max_chars),
        "text_snapshot_truncated": truncated,
        "captured_at_unix": int(time.time()),
    }
