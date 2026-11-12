from pathlib import Path
import shutil
import subprocess
import unittest


class DashboardBehaviorTests(unittest.TestCase):
    def test_session_switching_and_commentary_in_real_javascript(self):
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("Node is required for dashboard behavioral coverage")
        script = Path(__file__).with_name("dashboard_sessions.test.js")
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stop_continue_and_per_run_status_in_real_javascript(self):
        node = shutil.which("node") or "/opt/homebrew/bin/node"
        if not Path(node).exists():
            self.skipTest("Node is required for dashboard behavioral coverage")
        script = Path(__file__).with_name("dashboard_controls.test.js")
        result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
