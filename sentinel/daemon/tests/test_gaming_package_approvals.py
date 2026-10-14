"""Regression checks for the native gaming/launcher APT allowlist."""

import unittest

from sen.install.packages import APPROVED_PACKAGES


class GamingPackageApprovalsTests(unittest.TestCase):
    def test_launcher_and_overlay_tools_are_exact_apt_approvals(self):
        names = (
            "rofi",
            "mangohud",  # Existing approval must remain intact.
            "goverlay",
            "vkbasalt",
            "gamemode",
            "mesa-utils",
            "vulkan-tools",
            "mangohudctl",
            "mangoplot",
        )
        for name in names:
            with self.subTest(name=name):
                self.assertEqual(
                    APPROVED_PACKAGES[name],
                    {"type": "apt", "packages": [name]},
                )


if __name__ == "__main__":
    unittest.main()
