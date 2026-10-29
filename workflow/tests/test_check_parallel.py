"""Check overlap must be opt-in, bounded, resource-aware and fail closed."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('parallel_workflow', Path(__file__).parents[1] / 'scripts/workflow.py')
workflow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(workflow)


def item(name, **extra):
    return dict(name=name, level='core', command=['synthetic', name], **extra)


class ParallelCheckTest(unittest.TestCase):
    def execute(self, items, launch, jobs=2):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.ai-workflow.json').write_text(json.dumps(dict(version=1, checks=items)))
            with patch.object(workflow, 'repository', return_value=root), patch.object(workflow, 'git'), \
                    patch.object(workflow.subprocess, 'run', side_effect=launch), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return workflow.check(SimpleNamespace(target=tmp, level='core', jobs=jobs))

    def test_independent_checks_actually_overlap(self):
        rendezvous = threading.Barrier(2)

        def launch(command, **kwargs):
            rendezvous.wait(timeout=2)
            return subprocess.CompletedProcess(command, 0)

        self.assertEqual(self.execute([item('a', parallel_safe=True), item('b', parallel_safe=True)], launch), 0)

    def test_unknown_checks_and_shared_resources_are_serial_barriers(self):
        checks = [item('a', parallel_safe=True), item('b'), item('c', parallel_safe=True, resource_group='gradle'),
                  item('d', parallel_safe=True, resource_group='gradle'), item('e', parallel_safe=True)]
        self.assertEqual([[c['name'] for c in batch] for batch in workflow.check_batches(checks, 2)],
                         [['a'], ['b'], ['c'], ['d', 'e']])
        self.assertTrue(all(len(batch) == 1 for batch in workflow.check_batches(checks, 1)))

    def test_failure_waits_for_started_peers_and_does_not_start_later_work(self):
        called = []

        def launch(command, **kwargs):
            called.append(command[-1])
            return subprocess.CompletedProcess(command, 9 if command[-1] == 'a' else 0)

        result = self.execute([item('a', parallel_safe=True), item('b', parallel_safe=True), item('later')], launch)
        self.assertEqual(result, 1)
        self.assertCountEqual(called, ['a', 'b'])

    def test_timeout_and_bad_flags_do_not_pass(self):
        with self.subTest('timeout'):
            self.assertEqual(self.execute([item('a')], lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('synthetic', 1))), 1)
        for extra in ({'parallel_safe': 'true'}, {'resource_group': ''}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                workflow.validate_config(dict(version=1, checks=[item('a', **extra)]))


if __name__ == '__main__':
    unittest.main()
