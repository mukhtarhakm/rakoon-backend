"""_is_demo_mode_allowed: production di RAKOON_ENV atau ENVIRONMENT mematikan mode demo."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.routers.ads import _is_demo_mode_allowed


class TestDemoModeGuard(unittest.TestCase):
    def allowed(self, **env):
        base = {"RAKOON_ENV": "", "ENVIRONMENT": "", "ALLOW_DEMO_ADS": "true"}
        base.update(env)
        with patch.dict(os.environ, base):
            return _is_demo_mode_allowed()

    def test_production_in_either_variable_disables_demo(self):
        cases = [
            {"RAKOON_ENV": "production", "ENVIRONMENT": "development"},  # kasus Render sekarang
            {"RAKOON_ENV": "production"},
            {"ENVIRONMENT": "production"},
            {"RAKOON_ENV": "PRODUCTION"},
            {"ENVIRONMENT": " Production "},
            {"RAKOON_ENV": "development", "ENVIRONMENT": "production"},
        ]
        for env in cases:
            self.assertFalse(self.allowed(**env), env)

    def test_demo_needs_the_explicit_flag_outside_production(self):
        self.assertTrue(self.allowed(RAKOON_ENV="development"))
        self.assertTrue(self.allowed(ENVIRONMENT="development"))
        self.assertTrue(self.allowed())
        self.assertFalse(self.allowed(ALLOW_DEMO_ADS="false"))
        self.assertFalse(self.allowed(ALLOW_DEMO_ADS=""))


if __name__ == "__main__":
    unittest.main()
