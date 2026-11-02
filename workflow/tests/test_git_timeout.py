"""Git network stalls must exit without leaking output or replaying writes."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(
        'bounded_' + name, Path(__file__).parents[1] / 'scripts' / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


workflow = load('workflow')
coordinate = load('coordinate')


class GitTimeoutTest(unittest.TestCase):
    def test_workflow_stall_is_bounded_sanitized_and_not_retried(self):
        failure = subprocess.TimeoutExpired('git credential-secret', 40,
                                            output='private-output')
        with patch.object(workflow.subprocess, 'run', side_effect=failure) as run:
            with self.assertRaisesRegex(ValueError, 'timed out after 40 seconds') as error:
                workflow.git(Path('.'), 'ls-remote', 'origin')
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.kwargs['timeout'], 40)
        self.assertNotIn('credential-secret', str(error.exception))
        self.assertNotIn('private-output', str(error.exception))

    def test_coordination_write_timeout_never_replays_even_with_check_false(self):
        store = coordinate.GitStore.__new__(coordinate.GitStore)
        store.root = '.'
        failure = subprocess.TimeoutExpired('git credential-secret', 40,
                                            output='private-output')
        for checked in (True, False):
            with self.subTest(check=checked), \
                    patch.object(coordinate.subprocess, 'run', side_effect=failure) as run:
                with self.assertRaisesRegex(coordinate.Conflict,
                                            'inspect remote state before retrying') as error:
                    store.run('push', 'origin', 'HEAD', check=checked)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.kwargs['timeout'], 40)
                self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')
                self.assertNotIn('credential-secret', str(error.exception))
                self.assertNotIn('private-output', str(error.exception))


if __name__ == '__main__':
    unittest.main()
