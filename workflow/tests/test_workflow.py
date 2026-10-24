import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_python_checks_do_not_leave_bytecode_in_a_clean_source_tree(self):
        (self.root/'fixture_module.py').write_text('VALUE = 42\n')
        config={'version':1,'checks':[{'name':'Python fixture','level':'core',
            'command':[sys.executable,'-c','import fixture_module; assert fixture_module.VALUE == 42']}]}
        (self.root/'.ai-workflow.json').write_text(json.dumps(config))
        self.git('add','.');self.git('commit','-qm','configured Python check')
        environment={k:v for k,v in os.environ.items() if k not in ('PYTHONDONTWRITEBYTECODE','PYTHONPYCACHEPREFIX')}
        with patch.dict(os.environ,environment,clear=True):
            result=self.run_tool('check','--level','core')
        self.assertEqual(0,result.returncode,result.stderr)
        self.assertFalse((self.root/'__pycache__').exists())
        self.assertEqual('',self.git('status','--porcelain'))

    def test_python_check_cache_choices_from_the_caller_are_preserved(self):
        for choice in ('write','prefix'):
            with self.subTest(choice=choice):
                environment={k:v for k,v in os.environ.items() if k not in ('PYTHONDONTWRITEBYTECODE','PYTHONPYCACHEPREFIX')}
                if choice=='write':
                    environment['PYTHONDONTWRITEBYTECODE']='0'
                    code='import os; assert os.environ["PYTHONDONTWRITEBYTECODE"] == "0"'
                else:
                    environment['PYTHONPYCACHEPREFIX']=str(Path(self.tmp.name)/'caller-cache')
                    code='import os; assert "PYTHONDONTWRITEBYTECODE" not in os.environ; assert os.environ.get("PYTHONPYCACHEPREFIX")'
                config={'version':1,'checks':[{'name':'Caller cache choice','level':'core','command':[sys.executable,'-c',code]}]}
                (self.root/'.ai-workflow.json').write_text(json.dumps(config))
                with patch.dict(os.environ,environment,clear=True):
                    result=self.run_tool('check','--level','core')
                self.assertEqual(0,result.returncode,result.stderr)

    def test_dry_run_then_idempotent_preserves_project(self):
        self.assertEqual(self.run_tool('adopt').returncode, 0)
        self.assertFalse((self.root / '.ai-workflow.json').exists())
        self.adopt()
        self.assertTrue((self.root / 'cloud/coordinate.py').is_file())
        self.assertTrue((self.root / 'cloud/verify_release.py').is_file())
        self.assertTrue((self.root / 'cloud/resume_status.py').is_file())
        self.assertTrue((self.root / 'cloud/git_recovery.py').is_file())
        self.assertTrue((self.root / 'cloud/privacy_scan.py').is_file())
        self.assertTrue((self.root / 'docs/DELIVERY.md').is_file())
        self.assertIn('Keep existing rules', (self.root / 'AGENTS.md').read_text())
        package = SCRIPT.parents[1]
        begin, end = '<!-- shared-cloud-workflow:start -->', '<!-- shared-cloud-workflow:end -->'
        canonical = (package / 'CORE.md').read_text().split(begin, 1)[1].split(end, 1)[0].strip()
        fragment = (package / 'templates/AGENTS.fragment.md').read_text().strip()
        source_entry = (package.parent / 'AGENTS.md').read_text().split(begin, 1)[1].split(end, 1)[0].strip()
        self.assertEqual(fragment, canonical, 'New-project policy must match the maintained cloud rules')
        self.assertEqual(source_entry, canonical, 'This project entry must not drift from its maintained rules')
        self.assertIn(canonical, (self.root / 'AGENTS.md').read_text())
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
