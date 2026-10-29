"""Check overlap must be opt-in, bounded, resource-aware and fail closed."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
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
        events = []

        def launch(command, **kwargs):
            name = command[-1]
            events.append(('start', name))
            time.sleep(0.01)
            events.append(('end', name))
            return subprocess.CompletedProcess(command, 0)

        self.assertEqual(self.execute(checks, launch), 0)
        for before, after in [('a', 'b'), ('b', 'c'), ('c', 'd')]:
            self.assertLess(events.index(('end', before)), events.index(('start', after)))
        events.clear()
        self.assertEqual(self.execute(checks, launch, jobs=1), 0)
        for before, after in zip(checks, checks[1:]):
            self.assertLess(events.index(('end', before['name'])), events.index(('start', after['name'])))

    def test_idle_worker_starts_next_check_before_slow_peer_finishes(self):
        third_started = threading.Event()

        def launch(command, **kwargs):
            if command[-1] == 'slow':
                # A fixed batch cannot reach the third check before this timeout.
                self.assertTrue(third_started.wait(timeout=2), 'idle worker did not refill')
            elif command[-1] == 'third':
                third_started.set()
            return subprocess.CompletedProcess(command, 0)

        checks = [item(name, parallel_safe=True) for name in ('slow', 'fast', 'third')]
        self.assertEqual(self.execute(checks, launch), 0)

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
