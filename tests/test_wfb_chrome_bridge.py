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


if __name__ == "__main__":
    unittest.main()
