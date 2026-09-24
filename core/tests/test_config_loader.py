# File: core/tests/test_config_loader.py

import unittest
from pathlib import Path

from core.config.loader import ConfigLoader


class ConfigLoaderTests(unittest.TestCase):
    def test_the_trading_section_survives_loading(self) -> None:
        config = ConfigLoader(Path(__file__).resolve().parents[1] / "config.json").load()

        trading = config.get("trading")
        self.assertIsInstance(trading, dict)
        self.assertIn("decide", trading)
        self.assertIn("appraisal", trading)
        self.assertIn("price", trading["appraisal"]["ignore_features"])
        self.assertGreater(trading["decide"]["approve_lift"], trading["decide"]["veto_lift"])


if __name__ == "__main__":
    unittest.main()
