"""Temporary V016-0 gate probe; never merge this acceptance branch."""

import unittest


class RemoteGateProbe(unittest.TestCase):
    def test_required_ci_blocks_an_intentional_failure(self):
        self.assertTrue(False, "Intentional V016-0 negative acceptance probe")


if __name__ == "__main__":
    unittest.main()
