"""Synthetic configuration-only checks; no real services or credentials accessed."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('service_config', Path(__file__).with_name('peer-service-config.py'))
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hapi-config-unit-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original = b'[Unit]\nDescription=Synthetic\nRequires=hapi-hub.service\nAfter=hapi-hub.service\n[Service]\nExecStart=/synthetic/hapi\n'
        self.replacement = config.transform_runner_unit(self.original, config.HUB)
        self.target = self.root / 'runner.service'
        self.target.write_bytes(self.original)
        (self.root / 'original.unit').write_bytes(self.original)
        (self.root / 'replacement.unit').write_bytes(self.replacement)
        self.plan = {'unitPath': str(self.target), 'unitMode': 0o644, 'before': {'processes': []},
            'originalSha256': config.sha(self.original), 'replacementSha256': config.sha(self.replacement)}

    def test_transform_preserves_every_other_byte_and_line_ending(self):
        contents = b'# Requires=hapi-hub.service\r\n[Unit]\r\n  Requires=hapi-hub.service\r\nAfter=hapi-hub.service\r\n[Service]\r\nRequires=hapi-hub.service\r\n'
        expected = contents.replace(b'  Requires=', b'  Wants=', 1)
        self.assertEqual(config.transform_runner_unit(contents, config.HUB), expected)
        for invalid in [b'[Unit]\nWants=hapi-hub.service\n', self.original + b'[Unit]\nRequires=hapi-hub.service\n', b'[Service]\nRequires=hapi-hub.service\n']:
            with self.assertRaises(ValueError):
                config.transform_runner_unit(invalid, config.HUB)

    def test_target_service_mutation_commands_are_impossible(self):
        with patch.object(config.subprocess, 'run') as run:
            for action in ['stop', 'start', 'restart', 'try-restart', 'kill', 'revert', 'disable', 'set-property']:
                with self.assertRaises(RuntimeError):
                    config.systemctl(action, config.HUB)
            run.assert_not_called()

    def test_supervision_failure_happens_before_unit_write(self):
        with patch.object(config, 'verify_supervisor', side_effect=RuntimeError('not independent')), \
             patch.object(config, 'atomic_file') as write, patch.object(config, 'systemctl') as manager:
            with self.assertRaises(RuntimeError):
                config.apply(self.plan, self.root)
            write.assert_not_called()
            manager.assert_not_called()
        self.assertEqual(self.target.read_bytes(), self.original)

    def test_external_edit_is_never_overwritten(self):
        external = self.original + b'# external edit\n'
        self.target.write_bytes(external)
        with patch.object(config, 'verify_supervisor'), patch.object(config, 'validate_graph'), patch.object(config, 'verify_survival'):
            with self.assertRaisesRegex(RuntimeError, 'changed since preparation'):
                config.apply(self.plan, self.root)
            with self.assertRaisesRegex(RuntimeError, 'External unit edit'):
                config.recover(self.plan, self.root)
        self.assertEqual(self.target.read_bytes(), external)

    def test_failed_reload_restores_file_without_service_restart(self):
        with patch.object(config, 'verify_supervisor'), patch.object(config, 'validate_graph'), patch.object(config, 'verify_survival'), \
             patch.object(config, 'systemctl', side_effect=[RuntimeError('synthetic reload failure'), '']) as manager:
            with self.assertRaises(RuntimeError):
                config.apply(self.plan, self.root)
            self.assertEqual(self.target.read_bytes(), self.replacement)
            self.assertFalse((self.root / 'committed.json').exists())
            config.recover(self.plan, self.root)
            self.assertEqual(self.target.read_bytes(), self.original)
            self.assertEqual([c.args for c in manager.call_args_list], [('daemon-reload',), ('daemon-reload',)])
        self.assertEqual(json.loads((self.root / 'result.json').read_text())['status'], 'restored')

    def test_verified_commit_survives_supervisor_exit(self):
        with patch.object(config, 'verify_supervisor'), patch.object(config, 'validate_graph'), patch.object(config, 'verify_survival'), \
             patch.object(config, 'systemctl') as manager:
            config.apply(self.plan, self.root)
            config.recover(self.plan, self.root)
            manager.assert_called_once_with('daemon-reload')
        self.assertEqual(self.target.read_bytes(), self.replacement)
        self.assertEqual(json.loads((self.root / 'result.json').read_text())['status'], 'committed')


if __name__ == '__main__':
    unittest.main()
