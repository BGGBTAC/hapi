"""Synthetic regression tests; no existing HAPI state or services are touched."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('peer_activate', Path(__file__).with_name('peer-activate.py'))
activate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activate)
RECIPIENT = 'age1' + 'q' * 58


def envelope(recipient=RECIPIENT, **metadata):
    return {'data': 'ENC[AES256_GCM,data:synthetic-only,iv:test,tag:test,type:str]', 'sops': {
        'age': [{'recipient': recipient, 'enc': '-----BEGIN AGE ENCRYPTED FILE-----\nsynthetic-only'}], **metadata}}


class ActivationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='hapi-peer-activate-synthetic-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.hapi = self.home / '.hapi'
        self.hapi.mkdir()
        self.database = self.hapi / 'hapi.db'
        self.database.write_bytes(b'synthetic database bytes, never a real database')
        self.backup = self.root / 'synthetic.tar.gz.sops'
        self.sops = self.root / 'sops'
        self.sops.write_text('#!/usr/bin/python3\nimport sys, json\nsys.stdin.buffer.read()\njson.dump(' + repr(envelope()) + ', sys.stdout)\n')
        self.sops.chmod(0o700)

    def backup_now(self, timeout=3):
        return activate.encrypted_backup(self.home, ['.hapi'], self.database, self.backup,
            self.sops, self.root, 'synthetic', {RECIPIENT}, timeout)

    def test_envelope_exact_recipient_and_bounded_large_ciphertext(self):
        content = envelope()
        content['data'] = 'ENC[AES256_GCM,data:' + 'A' * (3 * 1024 * 1024) + ',type:str]'
        self.backup.write_text(json.dumps(content))
        activate.validate_envelope(self.backup, {RECIPIENT})
        with self.assertRaisesRegex(RuntimeError, 'exactly match'):
            activate.validate_envelope(self.backup, {'age1' + 'p' * 58})
        self.backup.write_text(json.dumps(envelope(kms=[{'arn': 'synthetic'}])))
        with self.assertRaisesRegex(RuntimeError, 'additional'):
            activate.validate_envelope(self.backup, {RECIPIENT})

    def test_tar_stream_members_prove_database_and_existing_wal_shm(self):
        Path(str(self.database) + '-wal').write_bytes(b'synthetic-wal')
        Path(str(self.database) + '-shm').write_bytes(b'synthetic-shm')
        receipt = self.backup_now()
        self.assertEqual(receipt['databaseMembers'], ['.hapi/hapi.db', '.hapi/hapi.db-shm', '.hapi/hapi.db-wal'])
        self.assertFalse(receipt['decryptRestoreTest'])
        self.assertTrue(self.backup.exists())
        self.assertEqual(len(receipt['sha256']), 64)

    def test_tar_stderr_cannot_fill_undrained_pipe(self):
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        tar = fake_bin / 'tar'
        tar.write_text('#!/usr/bin/python3\nimport sys\nsys.stderr.write("synthetic diagnostic\\n" * 50000)\nsys.stdout.buffer.write(b"synthetic")\n')
        tar.chmod(0o700)
        started = time.monotonic()
        with patch.dict(os.environ, {'PATH': str(fake_bin) + ':' + os.environ['PATH']}):
            with self.assertRaisesRegex(RuntimeError, 'reported diagnostics'):
                self.backup_now(timeout=2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(self.backup.exists())

    def test_backup_timeout_terminates_both_pipeline_processes(self):
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        tar = fake_bin / 'tar'
        pid_file = self.root / 'tar.pid'
        tar.write_text('#!/usr/bin/python3\nimport os, time\nopen(' + repr(str(pid_file)) + ', "w").write(str(os.getpid()))\ntime.sleep(30)\n')
        tar.chmod(0o700)
        started = time.monotonic()
        with patch.dict(os.environ, {'PATH': str(fake_bin) + ':' + os.environ['PATH']}):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                self.backup_now(timeout=0.3)
        self.assertLess(time.monotonic() - started, 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)
        self.assertFalse(self.backup.exists())

    def test_database_change_aborts_ciphertext_promotion(self):
        self.sops.write_text('#!/usr/bin/python3\nimport sys, json\nsys.stdin.buffer.read()\nopen(' + repr(str(self.database)) + ', "ab").write(b"changed")\njson.dump(' + repr(envelope()) + ', sys.stdout)\n')
        with self.assertRaisesRegex(RuntimeError, 'changed during backup'):
            self.backup_now()
        self.assertFalse(self.backup.exists())

    def test_missing_database_in_actual_tar_members_fails(self):
        (self.home / 'unrelated.txt').write_text('synthetic-only')
        with self.assertRaisesRegex(RuntimeError, 'membership receipt'):
            activate.encrypted_backup(self.home, ['unrelated.txt'], self.database, self.backup,
                self.sops, self.root, 'synthetic', {RECIPIENT}, 3)

    def test_free_space_checked_before_operations(self):
        self.assertGreater(activate.required_space(self.home, ['.hapi'], self.sops, self.root)['requiredBytes'], 0)
        with patch.object(activate.shutil, 'disk_usage', return_value=type('Space', (), {'free': 0})()):
            with self.assertRaisesRegex(RuntimeError, 'Insufficient'):
                activate.required_space(self.home, ['.hapi'], self.sops, self.root)

    def test_unknown_later_dropin_rejected_even_if_not_loaded(self):
        directory = self.root / 'units'
        drop_dir = directory / (activate.RUNNER + '.d')
        drop_dir.mkdir(parents=True)
        (drop_dir / '95-unrelated.conf').write_text('[Service]\n')
        with patch.object(activate, 'prop', return_value=''):
            with self.assertRaisesRegex(RuntimeError, 'foreign drop-in'):
                activate.assert_dropins(activate.RUNNER, directory)

    def test_unit_invocation_checked_without_disclosing_unexpected_values(self):
        unexpected = '{ path=/usr/bin/hapi ; argv[]=/usr/bin/hapi hub --synthetic-password pretend-value ; ignore_errors=no ; status=0/0 }'
        with patch.object(activate, 'prop', return_value=unexpected):
            with self.assertRaises(RuntimeError) as error:
                activate.assert_exec(activate.HUB, Path('/usr/bin/hapi'), 'hub')
        self.assertNotIn('pretend-value', str(error.exception))

    def test_child_exit_during_marker_probe_does_not_lose_other_adoptions(self):
        ended = {'pid': 101, 'generation': 'one', 'happySessionId': 'ended', 'processStartMarker': 'old'}
        alive = {'pid': 102, 'generation': 'two', 'happySessionId': 'alive', 'processStartMarker': 'marker'}
        with patch.object(activate, 'unchanged', side_effect=[True, False, True]), patch.object(activate, 'process_start', side_effect=[None, 'marker']):
            adopted = activate.merge_resume_records(self.hapi, [ended, alive])
        self.assertEqual(adopted, [alive])
        records = json.loads((self.hapi / 'runner.state.json.resume-processes.json').read_text())
        self.assertEqual([record['confirmedSessionId'] for record in records], ['alive'])

    def test_merge_keeps_alias_and_unrelated_record_and_drops_exited_snapshot(self):
        child = subprocess.Popen(['sleep', '30'])
        self.addCleanup(lambda: child.poll() is None and child.terminate())
        self.addCleanup(lambda: child.poll() is None and child.wait(timeout=1))
        # Cleanup runs backwards, so ensure termination precedes wait.
        self.addCleanup(child.terminate)
        current = activate.process(child.pid)
        marker = activate.process_start(child.pid)
        path = self.hapi / 'runner.state.json.resume-processes.json'
        unrelated = {'requestedSessionId': 'existing-unrelated', 'pid': 999999, 'processStartMarker': 'old'}
        path.write_text(json.dumps([{'requestedSessionId': 'original-alias', 'confirmedSessionId': 'old-confirmed', 'pid': child.pid, 'processStartMarker': marker}, unrelated]))
        snapshot = {**current, 'happySessionId': 'new-confirmed', 'processStartMarker': marker}
        adopted = activate.merge_resume_records(self.hapi, [snapshot, {'pid': 999998, 'generation': 'invalid', 'happySessionId': 'ended', 'processStartMarker': 'old'}])
        records = json.loads(path.read_text())
        self.assertEqual(adopted, [snapshot])
        self.assertIn(unrelated, records)
        self.assertIn({'requestedSessionId': 'original-alias', 'confirmedSessionId': 'new-confirmed', 'pid': child.pid, 'processStartMarker': marker}, records)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reused_pid_cannot_inherit_historical_alias(self):
        path = self.hapi / 'runner.state.json.resume-processes.json'
        path.write_text(json.dumps([{'requestedSessionId': 'unrelated-old-process', 'pid': 42, 'processStartMarker': 'old'}]))
        snapshot = {'pid': 42, 'generation': 'current', 'happySessionId': 'proven-current', 'processStartMarker': 'new'}
        with patch.object(activate, 'unchanged', return_value=True), patch.object(activate, 'process_start', return_value='new'):
            activate.merge_resume_records(self.hapi, [snapshot])
        self.assertEqual(json.loads(path.read_text())[0]['requestedSessionId'], 'proven-current')

    def test_recovery_always_activates_after_resume_error(self):
        with patch.object(activate, 'unchanged', return_value=False), patch.object(activate, 'unit_state', return_value={'ActiveState': 'failed'}), \
             patch.object(activate, 'merge_resume_records', side_effect=RuntimeError('synthetic merge failure')), \
             patch.object(activate, 'activate_runner', return_value={'pid': 123}) as start, \
             patch.object(activate, 'verify_adoption', return_value={'controllableSessionProcesses': []}):
            result = activate.recover_runner_after_signal(self.root, self.root / 'drop', self.root / 'transition', 'config',
                self.sops, 'args', self.hapi, {'pid': 1}, {'pid': 2}, [], False)
        start.assert_called_once()
        self.assertEqual(result['runnerPid'], 123)
        self.assertEqual(result['resumeRecoveryError'], 'synthetic merge failure')

    def test_recovery_never_rewrites_registry_after_start_attempt(self):
        with patch.object(activate, 'merge_resume_records') as merge, \
             patch.object(activate, 'activate_runner', return_value={'pid': 123}), \
             patch.object(activate, 'verify_adoption', return_value={}):
            activate.recover_runner_after_signal(self.root, self.root / 'drop', self.root / 'transition', 'config',
                self.sops, 'args', self.hapi, {'pid': 1}, {'pid': 2}, [], True)
        merge.assert_not_called()

    def test_runner_activation_removes_transition_and_records_failed_old_state(self):
        directory = self.root / 'units'
        drop = directory / (activate.RUNNER + '.d') / activate.DROP_NAME
        transition = drop.parent / '99-peer-transition.conf'
        transition.parent.mkdir(parents=True)
        transition.write_text('[Service]\nRestart=no\n')
        props = {'KillMode': 'process', 'Restart': 'always', 'RestartUSec': '5s'}
        with patch.object(activate, 'assert_dropins'), patch.object(activate, 'assert_exec'), \
             patch.object(activate, 'prop', side_effect=lambda unit, name: props[name]), \
             patch.object(activate, 'unit_state', return_value={'ActiveState': 'failed', 'Result': 'exit-code'}) as status, \
             patch.object(activate, 'run') as command, patch.object(activate, 'stable_runner', return_value={'pid': 42}):
            result = activate.activate_runner(directory, drop, transition, '[Service]\nRestartSec=5\n', self.sops, 'args', self.hapi)
        self.assertFalse(transition.exists())
        self.assertIn('RestartSec=5', drop.read_text())
        self.assertEqual(result['pid'], 42)
        calls = [item.args for item in command.call_args_list]
        self.assertIn(('systemctl', '--user', 'reset-failed', activate.RUNNER), calls)
        self.assertLess(calls.index(('systemctl', '--user', 'reset-failed', activate.RUNNER)), calls.index(('systemctl', '--user', 'start', activate.RUNNER)))
        status.assert_called_once()

    def test_stable_pid_check_waits_and_requires_state_agreement(self):
        began = time.monotonic()
        with patch.object(activate, 'prop', side_effect=lambda unit, name: 'active' if name == 'ActiveState' else '42'), \
             patch.object(activate, 'process', return_value={'pid': 42, 'generation': 'g', 'exe': str(self.sops)}), \
             patch.object(activate, 'runner_state', return_value={'pid': 41, 'httpPort': 1234}):
            with self.assertRaisesRegex(RuntimeError, 'Timed out'):
                activate.stable_runner(self.hapi, self.sops, timeout=0.3, stable_seconds=0.2)
        with patch.object(activate, 'prop', side_effect=lambda unit, name: 'active' if name == 'ActiveState' else '42'), \
             patch.object(activate, 'process', return_value={'pid': 42, 'generation': 'g', 'exe': str(self.sops)}), \
             patch.object(activate, 'runner_state', return_value={'pid': 42, 'httpPort': 1234}):
            self.assertEqual(activate.stable_runner(self.hapi, self.sops, timeout=1, stable_seconds=0.2)['pid'], 42)
        self.assertGreaterEqual(time.monotonic() - began, 0.5)


if __name__ == '__main__':
    unittest.main()
