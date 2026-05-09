"""Unit tests for stdlib Chrome bridge primitives."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import wfb_chrome_bridge as bridge


class _FakeSocket:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks[:]
        self.sent: list[bytes] = []

    def recv(self, n: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= n:
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[n:]
        return chunk[:n]

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        return None


class TestWfbChromeBridge(unittest.TestCase):
    def test_detect_debug_ports_returns_healthy_entries(self):
        with mock.patch.object(
            bridge,
            "fetch_version",
            side_effect=[bridge.ChromeBridgeError("down"), {"Browser": "Chrome/1"}],
        ):
            out = bridge.detect_debug_ports(candidates=(9222, 9333))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["port"], 9333)

    def test_parse_target_types_default(self):
        self.assertEqual(bridge.parse_target_types(None), ("page",))

    def test_parse_target_types_invalid_raises(self):
        with self.assertRaises(bridge.ChromeBridgeError):
            bridge.parse_target_types("page,iframe")

    def test_list_targets_filters_types_and_gemini(self):
        rows = [
            {"id": "p1", "type": "page", "url": "https://example.test", "title": "Example", "webSocketDebuggerUrl": "ws://x"},
            {
                "id": "w1",
                "type": "webview",
                "url": "https://gemini.google.com/glic?hl=en",
                "title": "Gemini Chrome :: New Conversation",
                "webSocketDebuggerUrl": "ws://y",
            },
        ]
        with mock.patch.object(bridge, "fetch_targets", return_value=rows):
            gemini = bridge.list_targets(include_types=("webview",), gemini_only=True)
            pages = bridge.list_targets(include_types=("page",), gemini_only=False)
        self.assertEqual(len(gemini), 1)
        self.assertEqual(gemini[0]["id"], "w1")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["id"], "p1")

    def test_select_capture_target_prefers_active(self):
        targets = [
            {"id": "a", "title": "A", "url": "https://example.test/a", "type": "page"},
            {"id": "b", "title": "B", "url": "https://example.test/b", "type": "page", "active": True},
        ]
        chosen, method, _ = bridge.select_capture_target(targets)
        self.assertEqual(chosen["id"], "b")
        self.assertEqual(method, "focused")

    def test_select_capture_target_explicit_id(self):
        targets = [
            {"id": "a", "title": "A", "url": "https://example.test/a", "type": "page"},
            {"id": "b", "title": "Gemini", "url": "https://gemini.google.com/glic", "type": "webview"},
        ]
        chosen, method, _ = bridge.select_capture_target(targets, target_id="a")
        self.assertEqual(chosen["id"], "a")
        self.assertEqual(method, "explicit_id")

    def test_select_capture_target_deprioritizes_chrome_internal_urls(self):
        targets = [
            {
                "id": "internal",
                "title": "Internal UI",
                "url": "chrome://contextual-tasks/?q=test",
                "type": "page",
            },
            {
                "id": "serp",
                "title": "Search",
                "url": "https://www.google.com/search?q=test",
                "type": "page",
            },
        ]
        chosen, method, _ = bridge.select_capture_target(targets)
        self.assertEqual(chosen["id"], "serp")
        self.assertEqual(method, "heuristic")

    def test_select_capture_target_only_chrome_internal_still_selects(self):
        targets = [
            {"id": "a", "title": "A", "url": "chrome://contextual-tasks/?q=1", "type": "page"},
            {"id": "b", "title": "B", "url": "chrome://version/", "type": "page"},
        ]
        chosen, method, _ = bridge.select_capture_target(targets)
        self.assertEqual(method, "heuristic")
        self.assertEqual(chosen["id"], "a")

    def test_launch_short_circuits_when_endpoint_exists(self):
        with (
            mock.patch.object(bridge, "fetch_version", return_value={"Browser": "Chrome/1"}) as fetch_version,
            mock.patch.object(bridge, "find_chrome_executable") as find_chrome,
            mock.patch.object(bridge.subprocess, "Popen") as popen,
        ):
            out = bridge.launch_chrome_debug(port=9222)
        self.assertEqual(out["Browser"], "Chrome/1")
        self.assertEqual(out["already_running"], True)
        find_chrome.assert_not_called()
        popen.assert_not_called()
        fetch_version.assert_called_once()

    def test_launch_spawns_when_endpoint_missing(self):
        with (
            mock.patch.object(
                bridge,
                "fetch_version",
                side_effect=[bridge.ChromeBridgeError("no endpoint"), {"Browser": "Chrome/2"}],
            ) as fetch_version,
            mock.patch.object(bridge, "detect_debug_ports", return_value=[]),
            mock.patch.object(bridge, "find_chrome_executable", return_value="/Applications/Chrome") as find_chrome,
            mock.patch.object(bridge.subprocess, "Popen") as popen,
        ):
            out = bridge.launch_chrome_debug(port=9333, profile_mode="user")
        self.assertEqual(out["already_running"], False)
        self.assertEqual(out["debug_port"], 9333)
        find_chrome.assert_called_once()
        popen.assert_called_once()
        self.assertEqual(fetch_version.call_count, 2)

    def test_launch_falls_back_to_detected_port(self):
        with (
            mock.patch.object(bridge, "fetch_version", side_effect=bridge.ChromeBridgeError("down")),
            mock.patch.object(
                bridge,
                "detect_debug_ports",
                return_value=[{"port": 9333, "version": {"Browser": "Chrome/Detected"}}],
            ),
            mock.patch.object(bridge, "find_chrome_executable") as find_chrome,
            mock.patch.object(bridge.subprocess, "Popen") as popen,
        ):
            out = bridge.launch_chrome_debug(port=9222)
        self.assertEqual(out["requested_port"], 9222)
        self.assertEqual(out["resolved_port"], 9333)
        self.assertEqual(out["fallback_used"], True)
        self.assertTrue(out["already_running"])
        find_chrome.assert_not_called()
        popen.assert_not_called()

    def test_encode_frame_masks_payload(self):
        frame = bridge._encode_ws_frame(b"hello", opcode=0x1, masked=True)
        self.assertEqual(frame[0] & 0x0F, 0x1)
        self.assertTrue(frame[1] & 0x80)

    def test_decode_unmasked_text_frame(self):
        raw = bridge._encode_ws_frame(b'{"ok":true}', opcode=0x1, masked=False)
        sock = _FakeSocket([raw])
        opcode, payload = bridge._decode_ws_frame(sock)
        self.assertEqual(opcode, 0x1)
        self.assertEqual(json.loads(payload.decode("utf-8"))["ok"], True)

    def test_choose_target_missing_raises(self):
        with self.assertRaises(bridge.ChromeBridgeError):
            bridge.choose_target([{"id": "a"}], "b")

    def test_cdp_call_skips_events_until_matching_id(self):
        conn = bridge.CDPConnection("ws://127.0.0.1:9222/devtools/page/abc")
        conn._sock = _FakeSocket([])
        conn._send_json = mock.Mock()  # type: ignore[method-assign]
        conn._recv_json = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                {"method": "Runtime.consoleAPICalled"},
                {"id": 1, "result": {"value": 42}},
            ]
        )
        out = conn.call("Runtime.evaluate", {"expression": "1+1"})
        self.assertEqual(out["value"], 42)

    def test_recv_headers_and_remainder_keeps_buffered_bytes(self):
        frame = bridge._encode_ws_frame(b'{"id":1,"result":{"ok":true}}', opcode=0x1, masked=False)
        raw = b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n" + frame
        sock = _FakeSocket([raw])
        header_blob, remainder = bridge._recv_headers_and_remainder(sock)
        self.assertIn("101", header_blob)
        self.assertGreater(len(remainder), 0)
        opcode, payload = bridge._decode_ws_frame(sock, recv_buffer=remainder)
        self.assertEqual(opcode, 0x1)
        self.assertTrue(json.loads(payload.decode("utf-8"))["result"]["ok"])

    def test_recv_json_unexpected_opcode_raises(self):
        binary_frame = bridge._encode_ws_frame(b"\x01\x02", opcode=0x2, masked=False)
        conn = bridge.CDPConnection("ws://127.0.0.1:9222/devtools/page/abc")
        conn._sock = _FakeSocket([binary_frame])
        with self.assertRaises(bridge.ChromeBridgeError):
            conn._recv_json()

    def test_inspect_target_extracts_value(self):
        fake_result = {
            "result": {
                "value": {
                    "url": "https://example.test",
                    "title": "Example",
                    "selected_text": "",
                    "text_snapshot": "body text",
                }
            }
        }
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.return_value = fake_result
            out = bridge.inspect_target(ws_url="ws://127.0.0.1:9222/devtools/page/x")
        self.assertEqual(out["url"], "https://example.test")
        self.assertEqual(out["title"], "Example")
        self.assertEqual(out["text_snapshot"], "body text")

    def test_inspect_target_applies_max_chars_exactly(self):
        fake_result = {
            "result": {
                "value": {
                    "url": "https://example.test",
                    "title": "Example",
                    "selected_text": "selection",
                    "text_snapshot": "abcdefghij",
                }
            }
        }
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.return_value = fake_result
            out = bridge.inspect_target(
                ws_url="ws://127.0.0.1:9222/devtools/page/x",
                max_chars=4,
            )
        self.assertEqual(out["text_snapshot"], "abcd")
        self.assertEqual(out["text_snapshot_chars"], 4)
        self.assertEqual(out["text_snapshot_truncated"], True)


if __name__ == "__main__":
    unittest.main()
