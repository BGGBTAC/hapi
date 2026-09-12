#!/usr/bin/env python3
"""Prove Hub/Runner dependency semantics using only unique synthetic user units.

Run directly with Python on a Linux host with a running user systemd manager.
The test never addresses actual HAPI units, configuration, credentials, or databases.
Its temporary services retain KillMode=control-group throughout the experiment.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest
import uuid


UNIT_PATTERN = re.compile(r'hapi-peer-topology-test-[0-9a-f]{32}-(?:hub|runner)\.service')


def validate_unit(name, owned):
    if not UNIT_PATTERN.fullmatch(name) or name not in owned:
        raise ValueError('Refusing a unit outside this isolated test namespace')


def generation(pid):
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] in ('Z', 'X') else fields[19]
    except (OSError, IndexError):
        return None


def wait_for(predicate, description, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError('Timed out: ' + description)


def unit_quote(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def transform_fixture(content, hub):
    """Use the production pure transform against the isolated synthetic fixture."""
    source = Path(__file__).with_name('peer-service-config.py')
    spec = importlib.util.spec_from_file_location('peer_service_config_topology', source)
    module = importlib.util.module_from_spec(spec)
    # Registering supports pure modules using dataclass/type introspection.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.transform_runner_unit(content.encode(), hub).decode()


def worker(kind, directory):
    root = Path(directory)
    if not root.name.startswith('hapi-peer-topology-test-'):
        raise ValueError('Worker requires its synthetic temporary directory')
    if kind == 'hub':
        while True:
            time.sleep(1)
    if kind == 'child':
        count = 0
        while True:
            count += 1
            print(json.dumps({'childPid': os.getpid(), 'count': count}), flush=True)
            time.sleep(0.1)
    if kind != 'runner':
        raise ValueError('Unknown synthetic worker')
    child = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), '--worker', 'child', str(root)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    record = {'runnerPid': os.getpid(), 'runnerGeneration': generation(os.getpid()),
              'childPid': child.pid, 'childGeneration': generation(child.pid)}
    pending = root / 'processes.json.tmp'
    pending.write_text(json.dumps(record))
    os.replace(pending, root / 'processes.json')
    with (root / 'heartbeat-output.jsonl').open('a', buffering=1) as output:
        for line in child.stdout:
            # A real pipe remains owned by the Runner. Continuing captured output
            # proves more than checking that detached child PIDs still exist.
            output.write(line)
    raise RuntimeError('Synthetic child output closed unexpectedly')


class TargetGuardTests(unittest.TestCase):
    def test_only_the_exact_owned_names_are_accepted(self):
        namespace = 'hapi-peer-topology-test-' + 'a' * 32
        owned = {namespace + '-hub.service', namespace + '-runner.service'}
        for name in owned:
            validate_unit(name, owned)
        for name in ('hapi-hub.service', 'hapi-runner.service', '*.service',
                     'hapi-peer-topology-test-' + 'b' * 32 + '-hub.service',
                     namespace + '-runner.service --all', '../hapi-hub.service'):
            with self.assertRaises(ValueError):
                validate_unit(name, owned)

    def test_fixture_changes_only_the_dependency_line(self):
        content = '[Unit]\nRequires=synthetic.service\nAfter=synthetic.service\n[Service]\nKillMode=control-group\n'
        changed = transform_fixture(content, 'synthetic.service')
        self.assertEqual(changed, content.replace('Requires=', 'Wants=', 1))
        self.assertIn('KillMode=control-group\n', changed)
        with self.assertRaises(ValueError):
            transform_fixture(changed, 'synthetic.service')


class ServiceTopologyTests(unittest.TestCase):
    def setUp(self):
        if sys.platform != 'linux':
            self.fail('This integration proof requires Linux and a user systemd manager')
        self.namespace = 'hapi-peer-topology-test-' + uuid.uuid4().hex
        self.hub = self.namespace + '-hub.service'
        self.runner = self.namespace + '-runner.service'
        self.owned = {self.hub, self.runner}
        self.commands = []
        runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
        self.assertTrue(runtime.is_dir(), 'User runtime directory is unavailable')
        self.assertEqual(runtime.stat().st_uid, os.getuid())
        self.unit_dir = runtime / 'systemd/user'
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.created = []
        self.temporary = tempfile.TemporaryDirectory(prefix=self.namespace + '-')
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.cleanup_units)
        self.assertEqual(self.command('show', self.hub, '--property=LoadState', '--value'), 'not-found')
        self.assertEqual(self.command('show', self.runner, '--property=LoadState', '--value'), 'not-found')
        self.write_unit(self.hub, self.fixture('hub'))
        self.write_unit(self.runner, self.fixture('runner'))
        self.reload()

    def command(self, verb, name, *options, allow_failure=False):
        validate_unit(name, self.owned)
        if verb not in ('show', 'start', 'stop', 'restart', 'reset-failed'):
            raise ValueError('Unexpected synthetic service operation')
        if any(option == '--all' or not option.startswith('--') for option in options):
            raise ValueError('Unexpected synthetic service option')
        self.commands.append({'operation': verb, 'unit': name})
        result = subprocess.run(['systemctl', '--user', verb, name, *options],
                                capture_output=True, text=True, timeout=15)
        if result.returncode and not allow_failure:
            self.fail('Synthetic systemctl failed: ' + result.stderr.strip())
        return result.stdout.strip()

    def reload(self):
        result = subprocess.run(['systemctl', '--user', 'daemon-reload'],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def prop(self, unit, key):
        return self.command('show', unit, '--property=' + key, '--value')

    def fixture(self, role):
        dependency = 'Requires=' + self.hub + '\nAfter=' + self.hub + '\n' if role == 'runner' else ''
        executable = ' '.join(unit_quote(value) for value in
                              (sys.executable, Path(__file__).resolve(), '--worker', role, self.root))
        return (f'# Owner: {self.namespace}\n[Unit]\nDescription=Isolated HAPI topology proof\n'
                + dependency + '[Service]\nType=exec\nExecStart=' + executable
                + '\nKillMode=control-group\nRestart=no\nTimeoutStopSec=3\n')

    def write_unit(self, name, content, replace=False):
        validate_unit(name, self.owned)
        path = self.unit_dir / name
        if replace:
            self.assertIn(path, self.created)
            self.assertTrue(path.read_text().startswith('# Owner: ' + self.namespace + '\n'))
            fd, pending = tempfile.mkstemp(prefix=self.namespace + '-', suffix='.unit-tmp', dir=self.unit_dir)
            try:
                with os.fdopen(fd, 'w') as output:
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(pending, path)
            finally:
                if os.path.exists(pending):
                    os.unlink(pending)
        else:
            with path.open('x') as output:
                output.write(content)
            self.created.append(path)

    def cleanup_units(self):
        failures = []
        for name in (self.runner, self.hub):
            if self.unit_dir / name not in self.created:
                continue
            try:
                self.command('stop', name)
                self.command('reset-failed', name, allow_failure=True)
            except Exception as error:
                failures.append(str(error))
        for path in self.created:
            try:
                validate_unit(path.name, self.owned)
                self.assertEqual(path.parent, self.unit_dir)
                self.assertTrue(path.read_text().startswith('# Owner: ' + self.namespace + '\n'))
                path.unlink()
            except Exception as error:
                failures.append(str(error))
        if self.created:
            self.reload()
        if failures:
            raise AssertionError('Synthetic service cleanup failed: ' + '; '.join(failures))

    def process_record(self):
        return json.loads((self.root / 'processes.json').read_text())

    def output_count(self, child_pid):
        path = self.root / 'heartbeat-output.jsonl'
        if not path.exists():
            return 0
        counts = []
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record['childPid'] == child_pid:
                counts.append(record['count'])
        return max(counts, default=0)

    def assert_survives_with_output(self, record):
        self.assertEqual(self.prop(self.runner, 'ActiveState'), 'active')
        self.assertEqual(int(self.prop(self.runner, 'MainPID')), record['runnerPid'])
        self.assertEqual(generation(record['runnerPid']), record['runnerGeneration'])
        self.assertEqual(generation(record['childPid']), record['childGeneration'])
        before = self.output_count(record['childPid'])
        wait_for(lambda: self.output_count(record['childPid']) >= before + 3,
                 'three additional child heartbeats captured by the same Runner')
        return self.output_count(record['childPid'])

    def test_requires_cascades_and_wants_preserves_runner_and_child(self):
        self.command('start', self.runner)
        wait_for(lambda: (self.root / 'processes.json').exists(), 'first synthetic child registration')
        first = self.process_record()
        self.assert_survives_with_output(first)
        self.assertIn(self.hub, self.prop(self.runner, 'Requires').split())
        self.command('stop', self.hub)
        wait_for(lambda: self.prop(self.runner, 'ActiveState') == 'inactive', 'Requires stop cascade')
        wait_for(lambda: generation(first['runnerPid']) != first['runnerGeneration']
                 and generation(first['childPid']) != first['childGeneration'], 'cascaded process exit')

        self.command('start', self.runner)
        wait_for(lambda: self.process_record()['runnerPid'] != first['runnerPid'], 'second synthetic Runner registration')
        second = self.process_record()
        self.assert_survives_with_output(second)
        original = (self.unit_dir / self.runner).read_text()
        transformed = transform_fixture(original, self.hub)
        self.write_unit(self.runner, transformed, replace=True)
        self.reload()
        self.assertEqual(self.prop(self.runner, 'KillMode'), 'control-group')
        self.assertNotIn(self.hub, self.prop(self.runner, 'Requires').split())
        self.assertIn(self.hub, self.prop(self.runner, 'Wants').split())
        self.assertIn(self.hub, self.prop(self.runner, 'After').split())
        self.assertNotIn(self.runner, self.prop(self.hub, 'RequiredBy').split())

        self.command('stop', self.hub)
        stopped_count = self.assert_survives_with_output(second)
        self.command('start', self.hub)
        started_count = self.assert_survives_with_output(second)
        self.command('restart', self.hub)
        restarted_count = self.assert_survives_with_output(second)
        self.assertEqual(self.prop(self.hub, 'ActiveState'), 'active')
        print(json.dumps({'proof': 'isolated-systemd-topology', 'namespace': self.namespace,
                          'requiresCascadeObserved': True, 'onlyDependencyLineChanged': True,
                          'killMode': 'control-group', 'preservedProcesses': second,
                          'capturedChildHeartbeats': {'afterHubStop': stopped_count,
                                                     'afterHubStart': started_count,
                                                     'afterHubRestart': restarted_count},
                          'serviceTargets': sorted({entry['unit'] for entry in self.commands})}), flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main(verbosity=2)
