from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import relay


class RelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def response(result: str, session_id: str):
        class Completed:
            returncode = 0
            stderr = ""
            stdout = json.dumps({"result": result, "session_id": session_id})

        return Completed()

    @patch("relay.subprocess.run")
    def test_first_message_creates_session(self, run):
        run.return_value = self.response("完成", "session-one")
        self.assertEqual(relay.relay(self.project, "执行任务"), "完成")
        command = run.call_args.args[0]
        self.assertIn("--session-id", command)
        self.assertIn("/butler 执行任务", command)
        self.assertEqual(relay.load_session(self.project), "session-one")

    @patch("relay.subprocess.run")
    def test_next_message_resumes_session(self, run):
        relay.save_session(self.project, "session-old")
        run.return_value = self.response("继续完成", "session-old")
        self.assertEqual(relay.relay(self.project, "继续"), "继续完成")
        self.assertIn("--resume", run.call_args.args[0])

    @patch("relay.subprocess.run")
    def test_adopt_uses_continue_and_saves_session(self, run):
        run.return_value = self.response("已接管", "session-adopted")
        self.assertEqual(relay.relay(self.project, "继续", adopt=True), "已接管")
        command = run.call_args.args[0]
        self.assertIn("--continue", command)
        self.assertNotIn("--resume", command)
        self.assertEqual(relay.load_session(self.project), "session-adopted")

    @patch("relay.subprocess.run")
    def test_new_window_marker_is_forwarded(self, run):
        run.side_effect = [
            self.response("NEW_WINDOW\n已完成：A\n下一步：B", "session-old"),
            self.response("新窗口已继续", "session-new"),
        ]
        self.assertEqual(relay.relay(self.project, "长任务"), "新窗口已继续")
        self.assertEqual(run.call_count, 2)
        second_command = run.call_args_list[1].args[0]
        self.assertIn("--session-id", second_command)
        self.assertTrue(any("NEW_WINDOW" in part for part in second_command))
        self.assertEqual(relay.load_session(self.project), "session-new")

    @patch("relay.continue_native", return_value="继续完成")
    @patch("relay.screen_status", return_value="attached")
    @patch("relay.screen_is_alive", return_value=True)
    def test_native_resumes_live_screen(self, alive, status, continue_native):
        relay.save_session(
            self.project,
            "session-native",
            screen_name="butler-native-test",
            mode="native",
        )
        result = relay.relay_native(self.project, "继续")
        self.assertEqual(result, "继续完成")
        continue_native.assert_called_once_with(
            "butler-native-test", "session-native", "继续"
        )

    @patch("relay.open_native_terminal")
    @patch("relay.continue_native", return_value="继续完成")
    @patch("relay.screen_status", return_value="detached")
    @patch("relay.screen_is_alive", return_value=True)
    def test_native_reopens_detached_screen(
        self, alive, status, continue_native, open_terminal
    ):
        relay.save_session(
            self.project,
            "session-native",
            screen_name="butler-native-test",
            mode="native",
        )
        relay.relay_native(self.project, "继续")
        open_terminal.assert_called_once_with("butler-native-test")

    @patch(
        "relay.start_native",
        return_value=("首次完成", "session-new", "butler-native-new"),
    )
    def test_native_starts_and_saves_screen(self, start_native):
        result = relay.relay_native(self.project, "开始", force_new=True)
        self.assertEqual(result, "首次完成")
        start_native.assert_called_once_with(self.project, "开始")
        self.assertEqual(
            relay.load_state(self.project)["screen_name"], "butler-native-new"
        )

    def test_transcript_reply_matches_marker(self):
        transcript = self.project / "session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "text",
                                "text": "任务完成\n[relay-marker:test]",
                            }
                        ]
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )
        self.assertEqual(
            relay.transcript_reply(transcript, "[relay-marker:test]"), "任务完成"
        )

    def test_transcript_detects_finished_reply_without_marker(self):
        marker = "[relay-marker:test]"
        transcript = self.project / "session.jsonl"
        events = [
            {"type": "user", "message": {"content": f"任务 {marker}"}},
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "完成但漏了标记"}],
                    "stop_reason": "end_turn",
                },
            },
        ]
        transcript.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )
        self.assertTrue(relay.transcript_turn_ended_without_marker(transcript, marker))

    @patch("relay.screen_is_alive", return_value=False)
    @patch("relay.find_transcript", return_value=None)
    def test_wait_stops_when_screen_exits(self, transcript, alive):
        with self.assertRaisesRegex(RuntimeError, "已退出"):
            relay.wait_for_native_reply("session", "marker", "screen")

    @patch("relay.screen_status", return_value="attached")
    def test_status_reports_native_screen(self, status):
        relay.save_session(
            self.project,
            "session-native",
            screen_name="butler-native-test",
            mode="native",
        )
        value = relay.relay_status(self.project)
        self.assertEqual(value["screen_status"], "attached")
        self.assertEqual(value["session_id"], "session-native")

    @patch(
        "relay.start_native",
        side_effect=[
            ("NEW_WINDOW\n交接内容", "session-old", "screen-old"),
            ("新窗口完成", "session-new", "screen-new"),
        ],
    )
    def test_native_new_window_marker_starts_fresh_session(self, start_native):
        self.assertEqual(
            relay.relay_native(self.project, "长任务", force_new=True), "新窗口完成"
        )
        self.assertEqual(start_native.call_count, 2)
        self.assertEqual(relay.load_session(self.project), "session-new")


if __name__ == "__main__":
    unittest.main()
