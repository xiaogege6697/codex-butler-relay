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

    def test_save_session_preserves_goal(self):
        relay.update_state(self.project, goal="完成项目", goal_status="running")
        relay.save_session(self.project, "session-one", mode="interactive")
        state = relay.load_state(self.project)
        self.assertEqual(state["goal"], "完成项目")
        self.assertEqual(state["session_id"], "session-one")

    def test_goal_signals_use_first_nonblank_line(self):
        self.assertEqual(relay.goal_signal("\nGOAL_DONE\n完成"), "GOAL_DONE")
        self.assertEqual(relay.goal_signal("NEED_DECISION\n选项"), "NEED_DECISION")
        self.assertEqual(relay.goal_signal("普通进度"), "RUNNING")

    def test_goal_prompt_encodes_economic_and_event_contract(self):
        prompt = relay.goal_prompt("完成项目")
        for value in ("GOAL_DONE", "NEED_DECISION", "NEW_WINDOW", "完成项目"):
            self.assertIn(value, prompt)
        self.assertIn("普通进度只在当前 Claude TUI 展示", prompt)
        self.assertIn("约 60%", prompt)
        self.assertIn("超过 70%", prompt)
        self.assertIn("配置可用时不要询问", prompt)
        self.assertIn("临时工执行 + Claude 审核 + 预期返工", prompt)

    def test_turn_prompt_loads_butler_only_when_requested(self):
        initial = relay.turn_prompt("任务", "marker", load_butler=True)
        continued = relay.turn_prompt("继续", "marker", load_butler=False)
        self.assertTrue(initial.startswith("/butler 任务"))
        self.assertTrue(continued.startswith("继续"))
        self.assertNotIn("/butler", continued)

    @patch("relay.time.sleep")
    @patch("relay.subprocess.Popen")
    def test_screen_launcher_does_not_wait_for_interactive_process(self, popen, sleep):
        relay._start_screen(self.project, "screen-name", "/bin/zsh", "-l")
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    @patch("relay.send_native_turn", return_value="完成")
    @patch("relay._screen_command")
    @patch("relay.wait_for_native_ready")
    @patch("relay.ensure_screen_attached")
    @patch("relay._start_screen")
    def test_native_starts_like_manual_claude_and_loads_butler_once(
        self, start_screen, ensure_attached, wait_ready, screen_command, send_turn
    ):
        result, session_id, screen_name = relay.start_native(self.project, "执行任务")
        self.assertEqual(result, "完成")
        self.assertIn("-l", start_screen.call_args.args)
        launch_calls = [str(call.args) for call in screen_command.call_args_list]
        self.assertTrue(any("--session-id" in call for call in launch_calls))
        self.assertFalse(any("--permission-mode" in call for call in launch_calls))
        send_turn.assert_called_once_with(
            screen_name, session_id, "执行任务", load_butler=True
        )

    @patch("relay.send_native_turn", return_value="继续完成")
    @patch("relay.screen_is_alive", return_value=True)
    def test_native_continuation_does_not_reload_butler(self, alive, send_turn):
        result = relay.continue_native("butler-native-test", "session", "继续")
        self.assertEqual(result, "继续完成")
        send_turn.assert_called_once_with(
            "butler-native-test", "session", "继续", load_butler=False
        )

    @patch("relay.continue_native", return_value="继续完成")
    @patch("relay.screen_status", return_value="attached")
    @patch("relay.screen_is_alive", return_value=True)
    def test_native_resumes_live_screen(self, alive, status, continue_native):
        relay.save_session(
            self.project,
            "session-native",
            screen_name="butler-native-test",
            mode="interactive",
        )
        self.assertEqual(relay.relay_native(self.project, "继续"), "继续完成")
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
            mode="interactive",
        )
        relay.relay_native(self.project, "继续")
        open_terminal.assert_called_once_with("butler-native-test")

    @patch("relay.close_screen")
    @patch(
        "relay.start_native",
        return_value=("首次完成", "session-new", "butler-native-new"),
    )
    def test_force_new_replaces_managed_screen(self, start_native, close_screen):
        relay.save_session(
            self.project,
            "session-old",
            screen_name="butler-native-old",
            mode="interactive",
        )
        result = relay.relay_native(self.project, "开始", force_new=True)
        self.assertEqual(result, "首次完成")
        close_screen.assert_called_once_with("butler-native-old")
        self.assertEqual(relay.load_session(self.project), "session-new")

    @patch("relay._screen_command")
    @patch("relay.screen_is_alive", return_value=True)
    def test_close_screen_only_accepts_managed_name(self, alive, screen_command):
        self.assertFalse(relay.close_screen("user-session"))
        screen_command.assert_not_called()
        self.assertTrue(relay.close_screen("butler-native-safe"))
        screen_command.assert_called_once_with(
            "-S", "butler-native-safe", "-X", "quit", check=False
        )

    @patch("relay.close_screen")
    @patch(
        "relay.start_native",
        side_effect=[
            ("NEW_WINDOW\n交接内容", "session-old", "butler-native-old"),
            ("GOAL_DONE\n新窗口完成", "session-new", "butler-native-new"),
        ],
    )
    def test_new_window_handoff_closes_replaced_screen(self, start_native, close_screen):
        self.assertEqual(
            relay.relay_native(self.project, "长任务", force_new=True),
            "GOAL_DONE\n新窗口完成",
        )
        self.assertEqual(start_native.call_count, 2)
        close_screen.assert_any_call("butler-native-old")
        self.assertEqual(relay.load_session(self.project), "session-new")

    @patch("relay.time.sleep")
    @patch("relay.native_screen_snapshot", return_value="Welcome back!")
    @patch("relay.screen_is_alive", return_value=True)
    def test_native_ready_detects_real_tui(self, alive, snapshot, sleep):
        relay.wait_for_native_ready("screen-one")
        sleep.assert_not_called()

    @patch("relay.time.sleep")
    @patch("relay.screen_status", side_effect=["detached", "attached"])
    @patch("relay.open_native_terminal")
    def test_terminal_attach_retries_after_race(self, open_terminal, status, sleep):
        relay.ensure_screen_attached("screen-one")
        self.assertEqual(open_terminal.call_count, 2)

    def test_transcript_reply_matches_marker(self):
        transcript = self.project / "session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": "任务完成\n[relay-marker:test]"}
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
            mode="interactive",
        )
        value = relay.relay_status(self.project)
        self.assertEqual(value["screen_status"], "attached")
        self.assertEqual(value["mode"], "interactive")

    @patch("relay.subprocess.run")
    def test_headless_loads_butler_only_for_new_session(self, run):
        run.side_effect = [
            self.response("首次完成", "session-one"),
            self.response("继续完成", "session-one"),
        ]
        self.assertEqual(relay.relay_headless(self.project, "执行"), "首次完成")
        first = run.call_args_list[0].args[0]
        self.assertTrue(first[-1].startswith("/butler "))
        self.assertEqual(relay.relay_headless(self.project, "继续"), "继续完成")
        second = run.call_args_list[1].args[0]
        self.assertEqual(second[-1], "继续")
        self.assertNotIn("/butler", second[-1])

    @patch(
        "relay.start_native",
        return_value=("GOAL_DONE\n成果", "session-new", "butler-native-new"),
    )
    def test_goal_result_waits_for_codex_acceptance(self, start_native):
        relay.update_state(self.project, goal="完成项目", goal_status="running")
        relay.relay_native(self.project, "开始", force_new=True)
        state = relay.load_state(self.project)
        self.assertEqual(state["goal_status"], "awaiting_acceptance")
        self.assertEqual(state["last_signal"], "GOAL_DONE")

    def test_accept_marks_goal_complete(self):
        relay.update_state(self.project, goal="完成项目", goal_status="awaiting_acceptance")
        with patch(
            "relay.sys.argv", ["relay.py", "--accept", "--project", str(self.project)]
        ):
            self.assertEqual(relay.main(), 0)
        self.assertEqual(relay.load_state(self.project)["goal_status"], "accepted")

    @patch("relay.relay_native", return_value="GOAL_DONE\n完成")
    def test_goal_cli_starts_fresh_session_and_sends_contract(self, relay_native):
        with patch(
            "relay.sys.argv",
            ["relay.py", "--goal", "--project", str(self.project), "完成项目"],
        ):
            self.assertEqual(relay.main(), 0)
        sent_text = relay_native.call_args.args[1]
        self.assertIn("GOAL_DONE", sent_text)
        self.assertTrue(relay_native.call_args.kwargs["force_new"])


if __name__ == "__main__":
    unittest.main()
