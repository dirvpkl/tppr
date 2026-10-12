import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot


def make_config() -> bot.BotConfig:
    config = object.__new__(bot.BotConfig)
    config.token = "test-token"
    config.api = "http://mihomo:9090"
    config.group = "SVC_opencode-acc"
    return config


class PickNextTests(unittest.TestCase):
    def test_picks_middle(self) -> None:
        self.assertEqual(bot.pick_next(["a", "b", "c"], "a"), "b")

    def test_wraps_around_last(self) -> None:
        self.assertEqual(bot.pick_next(["a", "b", "c"], "c"), "a")

    def test_unknown_current_starts_first(self) -> None:
        self.assertEqual(bot.pick_next(["a", "b"], "zzz"), "a")

    def test_empty_members_fails_loudly(self) -> None:
        with self.assertRaisesRegex(ValueError, "no members"):
            bot.pick_next([], "a")


class HandleUpdateTests(unittest.TestCase):
    def test_start_rotates_and_replies(self) -> None:
        config = make_config()
        update = {"update_id": 7, "message": {"chat": {"id": 42}, "text": "/start"}}
        with patch.object(bot, "rotate_group", return_value=("old-node", "new-node")):
            self.assertEqual(
                bot.handle_update(config, update),
                (42, "rotated SVC_opencode-acc: old-node -> new-node"),
            )

    def test_start_with_bot_mention_rotates(self) -> None:
        config = make_config()
        update = {
            "update_id": 8,
            "message": {"chat": {"id": 1}, "text": "/start@mybot"},
        }
        with patch.object(bot, "rotate_group", return_value=("old-node", "new-node")):
            reply = bot.handle_update(config, update)
        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply[0], 1)

    def test_rotation_failure_replies_error(self) -> None:
        config = make_config()
        update = {"update_id": 9, "message": {"chat": {"id": 5}, "text": "/start"}}
        with patch.object(
            bot, "rotate_group", side_effect=RuntimeError("controller down")
        ):
            self.assertEqual(
                bot.handle_update(config, update),
                (5, "rotation of SVC_opencode-acc failed: controller down"),
            )

    def test_non_command_is_ignored(self) -> None:
        config = make_config()
        update = {"update_id": 10, "message": {"chat": {"id": 5}, "text": "hello"}}
        self.assertIsNone(bot.handle_update(config, update))

    def test_malformed_update_is_ignored(self) -> None:
        config = make_config()
        self.assertIsNone(bot.handle_update(config, {"update_id": 11}))
        self.assertIsNone(bot.handle_update(config, "junk"))


class RotateGroupTests(unittest.TestCase):
    def test_selects_next_and_verifies(self) -> None:
        config = make_config()
        with (
            patch.object(
                bot,
                "group_state",
                side_effect=[(["a", "b", "c"], "b"), (["a", "b", "c"], "c")],
            ),
            patch.object(bot, "select_node") as select,
        ):
            self.assertEqual(bot.rotate_group(config), ("b", "c"))
        select.assert_called_once_with(config.api, config.group, "c")

    def test_verify_mismatch_fails_loudly(self) -> None:
        config = make_config()
        with (
            patch.object(
                bot,
                "group_state",
                side_effect=[(["a", "b"], "a"), (["a", "b"], "a")],
            ),
            patch.object(bot, "select_node"),
        ):
            with self.assertRaisesRegex(RuntimeError, "did not select"):
                bot.rotate_group(config)


class ConfigTests(unittest.TestCase):
    def test_missing_token_fails_loudly(self) -> None:
        with (
            patch.dict("os.environ", {}, clear=True),
            self.assertRaisesRegex(ValueError, "TELEGRAM_BOT_TOKEN"),
        ):
            bot.BotConfig()

    def test_missing_proxy_fails_loudly(self) -> None:
        environment = {
            "TELEGRAM_BOT_TOKEN": "t",
            "MIHOMO_API": "http://m:9090",
            "TARGET_GROUP": "g",
        }
        with (
            patch.dict("os.environ", environment, clear=True),
            self.assertRaisesRegex(ValueError, "HTTPS_PROXY"),
        ):
            bot.BotConfig()


if __name__ == "__main__":
    unittest.main()
