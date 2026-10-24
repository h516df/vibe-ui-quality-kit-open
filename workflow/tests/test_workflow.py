import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/workflow.py'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'project'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Workflow fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        (self.root / 'AGENTS.md').write_text('# Keep existing rules\n')
        (self.root / 'HANDOFF.md').write_text('# Keep history\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'fixture')

    def git(self, *args):
        return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                              capture_output=True, text=True).stdout.strip()

    def run_tool(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args, '--target', str(self.root)],
                              capture_output=True, text=True)

    def adopt(self):
        self.assertEqual(self.run_tool('adopt', '--apply').returncode, 0)

    def test_dry_run_then_idempotent_preserves_project(self):
        self.assertEqual(self.run_tool('adopt').returncode, 0)
        self.assertFalse((self.root / '.ai-workflow.json').exists())
        self.adopt()
        self.assertTrue((self.root / 'cloud/coordinate.py').is_file())
        self.assertTrue((self.root / 'cloud/verify_release.py').is_file())
        self.assertTrue((self.root / 'cloud/resume_status.py').is_file())
        self.assertTrue((self.root / 'docs/DELIVERY.md').is_file())
        self.assertIn('Keep existing rules', (self.root / 'AGENTS.md').read_text())
        self.assertEqual((self.root / 'HANDOFF.md').read_text(), '# Keep history\n')
        result = self.run_tool('adopt', '--apply')
        self.assertEqual(result.returncode, 0)
        self.assertIn('0 file(s)', result.stdout)

    def test_conflict_preflight_and_symlink(self):
        (self.root / 'docs').mkdir()
        (self.root / 'docs/AI_WORKFLOW.md').write_text('custom')
        self.assertNotEqual(self.run_tool('adopt', '--apply').returncode, 0)
        self.assertFalse((self.root / 'cloud').exists())
        (self.root / 'docs/AI_WORKFLOW.md').unlink()
        (self.root / 'docs').rmdir()
        (self.root / 'docs').symlink_to(Path(self.tmp.name), target_is_directory=True)
        self.assertNotEqual(self.run_tool('adopt', '--apply').returncode, 0)
        self.assertFalse((Path(self.tmp.name) / 'AI_WORKFLOW.md').exists())

    def test_core_not_configured_and_failure_propagation(self):
        self.adopt()
        self.assertEqual(self.run_tool('check', '--level', 'core').returncode, 2)
        config = self.root / '.ai-workflow.json'
        data = json.loads(config.read_text())
        data['checks'] = [{'name': 'meaningful fixture', 'level': 'core',
                          'command': [sys.executable, '-c', 'raise SystemExit(7)']}]
        config.write_text(json.dumps(data))
        self.assertEqual(self.run_tool('check', '--level', 'core').returncode, 1)
        data['checks'][0]['command'][-1] = 'assert 2 + 2 == 4'
        config.write_text(json.dumps(data))
        self.assertEqual(self.run_tool('check', '--level', 'core').returncode, 0)
        self.assertEqual(self.run_tool('adopt', '--apply').returncode, 0)
        self.assertEqual(json.loads(config.read_text()), data)

    def test_zero_checks_and_empty_selected_level_are_not_passed(self):
        self.adopt()
        self.assertEqual(self.run_tool('check').returncode, 2)
        config = self.root / '.ai-workflow.json'
        data = json.loads(config.read_text())
        data['checks'] = [{'name': 'core only', 'level': 'core',
                           'command': [sys.executable, '-c', 'assert True']}]
        config.write_text(json.dumps(data))
        self.assertEqual(self.run_tool('check', '--level', 'quick').returncode, 2)
        self.assertEqual(self.run_tool('check', '--level', 'core').returncode, 0)

    def test_receipt_rejects_dirty_and_unpushed_head(self):
        remote = Path(self.tmp.name) / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '-q', str(remote)], check=True)
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.run_tool('receipt').returncode, 0)

        (self.root / 'new.txt').write_text('new')
        self.assertEqual(self.run_tool('receipt').returncode, 1)
        self.git('add', '.')
        self.git('commit', '-qm', 'not pushed')
        self.assertEqual(self.run_tool('receipt').returncode, 1)
        self.git('push', '-q', 'origin', 'main')
        self.assertEqual(self.run_tool('receipt').returncode, 0)

    def test_coordinator_conflict_does_not_partially_adopt(self):
        (self.root / 'cloud').mkdir()
        (self.root / 'cloud/coordinate.py').write_text('# project-specific coordinator\n')
        self.assertNotEqual(self.run_tool('adopt', '--apply').returncode, 0)
        self.assertFalse((self.root / 'docs/AI_WORKFLOW.md').exists())
        self.assertFalse((self.root / '.ai-workflow.json').exists())


if __name__ == '__main__':
    unittest.main()
