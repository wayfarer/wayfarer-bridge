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

    def test_inspect_target_passes_selector_into_runtime_evaluate(self):
        fake_result = {
            "result": {
                "value": {
                    "url": "https://example.test",
                    "title": "Example",
                    "selected_text": "",
                    "text_snapshot": "scoped text",
                    "selector_matched": True,
                }
            }
        }
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.return_value = fake_result
            out = bridge.inspect_target(
                ws_url="ws://127.0.0.1:9222/devtools/page/x",
                selector="main article",
            )
        kwargs = cm.call.call_args.args
        self.assertEqual(kwargs[0], "Runtime.evaluate")
        expression = kwargs[1]["expression"]
        self.assertIn('"main article"', expression)
        self.assertEqual(out["selector"], "main article")
        self.assertEqual(out["selector_matched"], True)
        self.assertEqual(out["text_snapshot"], "scoped text")

    def test_inspect_target_reports_selector_unmatched(self):
        fake_result = {
            "result": {
                "value": {
                    "url": "https://example.test",
                    "title": "Example",
                    "selected_text": "",
                    "text_snapshot": "",
                    "selector_matched": False,
                }
            }
        }
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.return_value = fake_result
            out = bridge.inspect_target(
                ws_url="ws://127.0.0.1:9222/devtools/page/x",
                selector=".missing",
            )
        self.assertEqual(out["selector_matched"], False)
        self.assertEqual(out["text_snapshot"], "")


class TestAccessibilityTree(unittest.TestCase):
    @staticmethod
    def _ax(node_id, role, name=None, child_ids=None, ignored=False, properties=None, parent_id=None):
        node = {
            "nodeId": node_id,
            "ignored": ignored,
            "role": {"type": "internalRole", "value": role},
            "childIds": child_ids or [],
        }
        if name is not None:
            node["name"] = {"type": "computedString", "value": name}
        if parent_id is not None:
            node["parentId"] = parent_id
        if properties is not None:
            node["properties"] = properties
        return node

    def test_get_accessibility_tree_calls_enable_get_disable(self):
        nodes_payload = {"nodes": [self._ax("1", "main")]}
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.side_effect = [{}, nodes_payload, {}]
            nodes = bridge.get_accessibility_tree(
                ws_url="ws://127.0.0.1:9222/devtools/page/x",
            )
        method_calls = [c.args[0] for c in cm.call.call_args_list]
        self.assertEqual(method_calls, ["Accessibility.enable", "Accessibility.getFullAXTree", "Accessibility.disable"])
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["nodeId"], "1")

    def test_get_accessibility_tree_passes_depth_param(self):
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.side_effect = [{}, {"nodes": []}, {}]
            bridge.get_accessibility_tree(ws_url="ws://x", depth=4)
        get_call = cm.call.call_args_list[1]
        self.assertEqual(get_call.args[0], "Accessibility.getFullAXTree")
        self.assertEqual(get_call.args[1], {"depth": 4})

    def test_get_accessibility_tree_disable_failure_is_swallowed(self):
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.side_effect = [
                {},
                {"nodes": [self._ax("1", "main")]},
                bridge.ChromeBridgeError("disable failed"),
            ]
            nodes = bridge.get_accessibility_tree(ws_url="ws://x")
        self.assertEqual(len(nodes), 1)

    def test_get_accessibility_tree_invalid_payload_raises(self):
        with mock.patch.object(bridge, "CDPConnection") as conn_cls:
            cm = conn_cls.return_value.__enter__.return_value
            cm.call.side_effect = [{}, {"unexpected": True}, {}]
            with self.assertRaises(bridge.ChromeBridgeError):
                bridge.get_accessibility_tree(ws_url="ws://x")

    def test_normalize_ax_node_extracts_role_name_value_props(self):
        raw = {
            "nodeId": "1",
            "ignored": False,
            "role": {"type": "internalRole", "value": "button"},
            "name": {"type": "computedString", "value": "Send"},
            "value": {"type": "string", "value": "send-id"},
            "childIds": ["2", "3"],
            "backendDOMNodeId": 42,
            "properties": [
                {"name": "focused", "value": {"type": "boolean", "value": True}},
                {"name": "level", "value": {"type": "integer", "value": 2}},
            ],
        }
        norm = bridge.normalize_ax_node(raw)
        self.assertEqual(norm["node_id"], "1")
        self.assertEqual(norm["role"], "button")
        self.assertEqual(norm["name"], "Send")
        self.assertEqual(norm["value"], "send-id")
        self.assertEqual(norm["child_ids"], ["2", "3"])
        self.assertEqual(norm["backend_dom_node_id"], 42)
        self.assertFalse(norm["ignored"])
        prop_names = [p["name"] for p in norm["properties"]]
        self.assertIn("focused", prop_names)
        self.assertIn("level", prop_names)

    def test_filter_ax_nodes_role_match_and_name_substring(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "main"),
                self._ax("2", "button", name="Send message"),
                self._ax("3", "button", name="Cancel"),
                self._ax("4", "textbox", name="Compose", ignored=True),
            ]
        )
        by_role = bridge.filter_ax_nodes(nodes, role="button")
        self.assertEqual({n["node_id"] for n in by_role}, {"2", "3"})
        by_name = bridge.filter_ax_nodes(nodes, name="message")
        self.assertEqual({n["node_id"] for n in by_name}, {"2"})
        ignored_default = bridge.filter_ax_nodes(nodes, role="textbox")
        self.assertEqual(ignored_default, [])
        with_ignored = bridge.filter_ax_nodes(nodes, role="textbox", include_ignored=True)
        self.assertEqual({n["node_id"] for n in with_ignored}, {"4"})

    def test_select_ax_subtrees_returns_matched_plus_descendants(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "WebArea", child_ids=["2", "3"]),
                self._ax("2", "main", name="Conversation", child_ids=["4"], parent_id="1"),
                self._ax("3", "navigation", child_ids=["5"], parent_id="1"),
                self._ax("4", "paragraph", name="Hello", parent_id="2"),
                self._ax("5", "link", name="Home", parent_id="3"),
            ]
        )
        subtrees = bridge.select_ax_subtrees(nodes, role="main")
        ids = sorted(n["node_id"] for n in subtrees)
        self.assertEqual(ids, ["2", "4"])
        roots = [n for n in subtrees if n.get("parent_id") is None]
        self.assertEqual([n["node_id"] for n in roots], ["2"])

    def test_select_ax_subtrees_no_match_returns_empty(self):
        nodes = bridge.normalize_ax_tree([self._ax("1", "main")])
        self.assertEqual(bridge.select_ax_subtrees(nodes, role="missing"), [])

    def test_render_ax_outline_indents_and_skips_ignored(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "WebArea", child_ids=["2"]),
                self._ax(
                    "2",
                    "main",
                    name="Conversation",
                    child_ids=["3", "4"],
                    parent_id="1",
                ),
                self._ax("3", "generic", child_ids=["5"], parent_id="2", ignored=True),
                self._ax(
                    "4",
                    "button",
                    name="Send",
                    parent_id="2",
                    properties=[
                        {"name": "focused", "value": {"type": "boolean", "value": True}},
                        {"name": "level", "value": {"type": "integer", "value": 2}},
                    ],
                ),
                self._ax("5", "paragraph", name="Hi", parent_id="3"),
            ]
        )
        out = bridge.render_ax_outline(nodes)
        lines = out["text"].splitlines()
        self.assertEqual(lines[0], "WebArea")
        self.assertEqual(lines[1], '  main "Conversation"')
        self.assertEqual(lines[2], '    paragraph "Hi"')
        self.assertEqual(lines[3], '    button "Send" focused level=2')
        self.assertEqual(out["rendered_count"], 4)
        self.assertEqual(out["total_count"], 5)
        self.assertFalse(out["truncated"])

    def test_render_ax_outline_max_nodes_truncates(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "main", child_ids=["2", "3", "4"]),
                self._ax("2", "paragraph", name="A", parent_id="1"),
                self._ax("3", "paragraph", name="B", parent_id="1"),
                self._ax("4", "paragraph", name="C", parent_id="1"),
            ]
        )
        out = bridge.render_ax_outline(nodes, max_nodes=2)
        self.assertEqual(out["rendered_count"], 2)
        self.assertTrue(out["truncated"])
        self.assertEqual(len(out["text"].splitlines()), 2)

    def test_render_ax_outline_truncates_long_names(self):
        long = "x" * 500
        nodes = bridge.normalize_ax_tree([self._ax("1", "paragraph", name=long)])
        out = bridge.render_ax_outline(nodes, name_max_chars=40)
        self.assertIn("(+460 chars)", out["text"])

    def test_render_ax_outline_handles_empty_input(self):
        out = bridge.render_ax_outline([])
        self.assertEqual(out["text"], "")
        self.assertEqual(out["rendered_count"], 0)
        self.assertEqual(out["total_count"], 0)

    def test_ax_quality_stats_distinguishes_meaningful_and_generic(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "main"),
                self._ax("2", "button"),
                self._ax("3", "generic"),
                self._ax("4", "section"),
                self._ax("5", "ignored", ignored=True),
            ]
        )
        stats = bridge.ax_quality_stats(nodes)
        self.assertEqual(stats["total"], 5)
        self.assertEqual(stats["non_ignored"], 4)
        self.assertEqual(stats["meaningful_roles"], 2)
        self.assertEqual(stats["generic_roles"], 2)
        self.assertAlmostEqual(stats["meaningful_ratio"], 0.5, places=2)

    def test_ax_quality_stats_empty_input(self):
        stats = bridge.ax_quality_stats([])
        self.assertEqual(stats["total"], 0)
        self.assertEqual(stats["meaningful_ratio"], 0.0)

    def test_find_in_ax_tree_returns_breadcrumb_and_match_metadata(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "main", name="Page", child_ids=["2"]),
                self._ax("2", "log", name="Conversation", child_ids=["3"], parent_id="1"),
                self._ax("3", "paragraph", name="Find this exact phrase here", parent_id="2"),
            ]
        )
        matches = bridge.find_in_ax_tree(nodes, query="exact phrase")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["node_id"], "3")
        self.assertEqual(matches[0]["role"], "paragraph")
        self.assertEqual([p["role"] for p in matches[0]["path"]], ["main", "log", "paragraph"])

    def test_find_in_ax_tree_role_filter_and_max_results(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "button", name="match button"),
                self._ax("2", "button", name="other match"),
                self._ax("3", "link", name="match link"),
            ]
        )
        only_buttons = bridge.find_in_ax_tree(nodes, query="match", role="button")
        self.assertEqual({m["node_id"] for m in only_buttons}, {"1", "2"})
        capped = bridge.find_in_ax_tree(nodes, query="match", max_results=1)
        self.assertEqual(len(capped), 1)

    def test_find_in_ax_tree_skips_ignored_by_default(self):
        nodes = bridge.normalize_ax_tree(
            [
                self._ax("1", "button", name="match here", ignored=True),
                self._ax("2", "button", name="match here"),
            ]
        )
        results = bridge.find_in_ax_tree(nodes, query="match")
        self.assertEqual({m["node_id"] for m in results}, {"2"})

    def test_find_text_matches_returns_context_windows(self):
        text = "before NEEDLE inside text and another NEEDLE later"
        matches = bridge.find_text_matches(text=text, query="needle", context_chars=7)
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0]["match"], "NEEDLE")
        self.assertEqual(matches[0]["before"], "before ")
        self.assertEqual(matches[0]["after"], " inside")
        self.assertEqual(matches[1]["offset"], text.index("NEEDLE", 10))

    def test_find_text_matches_max_results_caps_output(self):
        text = "ababab"
        out = bridge.find_text_matches(text=text, query="a", max_results=2)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["offset"], 0)
        self.assertEqual(out[1]["offset"], 2)

    def test_find_text_matches_empty_inputs(self):
        self.assertEqual(bridge.find_text_matches(text="", query="x"), [])
        self.assertEqual(bridge.find_text_matches(text="x", query=""), [])


if __name__ == "__main__":
    unittest.main()
