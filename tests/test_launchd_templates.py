from pathlib import Path
import plistlib
import unittest


DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


class LaunchdTemplateTests(unittest.TestCase):
    def test_templates_are_valid_plists_after_substitution(self):
        templates = sorted(DEPLOY.glob("*.plist.template"))
        self.assertEqual(len(templates), 2)
        for template in templates:
            with self.subTest(template=template.name):
                # Same substitution install-macos.sh does with sed.
                text = template.read_text(encoding="utf-8")
                text = text.replace("__PROJECT_ROOT__", "/Users/me/helios")
                text = text.replace("__PYTHON_BIN__", "/usr/bin/python3")
                plist = plistlib.loads(text.encode("utf-8"))
                self.assertEqual(plist["Label"], template.name.removesuffix(".plist.template"))
                self.assertNotIn("__", repr(plist))


if __name__ == "__main__":
    unittest.main()
