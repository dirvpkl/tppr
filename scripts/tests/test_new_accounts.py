import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import new_accounts


class AccountNameTests(unittest.TestCase):
    def test_uses_base_then_numeric_suffix(self) -> None:
        self.assertEqual(
            new_accounts._account_names("acc", 3, set()), ["acc", "acc1", "acc2"]
        )

    def test_skips_names_already_taken(self) -> None:
        self.assertEqual(
            new_accounts._account_names("acc", 2, {"acc", "acc1"}), ["acc2", "acc3"]
        )

    def test_skips_only_the_collisions(self) -> None:
        self.assertEqual(
            new_accounts._account_names("user", 3, {"user1"}),
            ["user", "user2", "user3"],
        )


class DispatcherPortTests(unittest.TestCase):
    def test_missing_block(self) -> None:
        self.assertIsNone(new_accounts._dispatcher_port('[health]\nurl = "x"\n'))

    def test_reads_port(self) -> None:
        text = '[health]\nurl = "x"\n\n[dispatcher]\nport = 17893\n'
        self.assertEqual(new_accounts._dispatcher_port(text), 17893)

    def test_block_without_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "no port"):
            new_accounts._dispatcher_port("[dispatcher]\n")


if __name__ == "__main__":
    unittest.main()
