import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("helios_version_server", ROOT / "agent" / "server.py")
server = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(server)


class VersionTests(unittest.TestCase):
    def test_gateway_and_package_report_the_same_version(self):
        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(server.VERSION, package["version"])

    def test_no_other_version_literal_is_left_in_the_gateway(self):
        source = (ROOT / "agent" / "server.py").read_text(encoding="utf-8")
        literals = re.findall(r'"\d+\.\d+\.\d+"', source)
        self.assertEqual(literals, ['"' + server.VERSION + '"'])


if __name__ == "__main__":
    unittest.main()
