#!/usr/bin/env python3
"""Exercise the real configuration recovery in independent synthetic user units.

Only this test's UUID-owned Hub, Runner, and supervisor services are addressed.
SIGKILL is sent solely to the verified synthetic supervisor MainPID. Existing HAPI
services, configuration, credentials, and databases are never addressed.
"""

import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import time
import unittest


def import_source(name, source):
    spec = importlib.util.spec_from_file_location(name, source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


topology = import_source('peer_topology_supervisor_fixture',
                         Path(__file__).with_name('peer-service-topology.test.py'))


def isolated_module(directory):
    """Bind only external unit identities and health to this synthetic fixture."""
    plan = json.loads((directory / 'synthetic-manifest.json').read_text())
    namespace = plan['syntheticNamespace']
    if not re.fullmatch(r'hapi-peer-topology-test-[0-9a-f]{32}', namespace):
        raise ValueError('Invalid synthetic namespace')
    if not directory.name.startswith(namespace + '-'):
        raise ValueError('Manifest is outside its synthetic directory')
    expected = {role: namespace + '-' + role + '.service' for role in ('hub', 'runner', 'worker')}
    if (plan['syntheticHub'] != expected['hub'] or plan['syntheticRunner'] != expected['runner']
            or plan['supervisorUnit'] != expected['worker']):
        raise ValueError('Manifest unit names do not belong to this synthetic test')
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid())))
    target = runtime / 'systemd/user' / expected['runner']
    if Path(plan['unitPath']) != target:
        raise ValueError('Refusing a configuration path outside the synthetic Runner')
    module = import_source('peer_supervisor_staged_applier', directory / 'applier.py')
    module.HUB = expected['hub']
    module.RUNNER = expected['runner']
    real_systemctl = module.systemctl

    def scoped_systemctl(*arguments):
        if not arguments:
            raise ValueError('Empty fixture systemctl operation')
        if arguments[0] == 'show':
            if len(arguments) < 2 or arguments[1] not in expected.values():
                raise ValueError('Refusing a query outside synthetic service identities')
        elif arguments != ('daemon-reload',):
            raise ValueError('Refusing a target-service mutation inside the applier')
        return real_systemctl(*arguments)

    module.systemctl = scoped_systemctl
    module.healthy = lambda: module.prop(expected['hub'], 'ActiveState') == 'active'
    return module, plan


def synthetic_worker(action, directory):
    module, plan = isolated_module(directory)
    if action == 'recover':
        module.recover(plan, directory)
        return
    if action == 'commit':
        module.apply(plan, directory)
        return
    if action != 'hold':
        raise ValueError('Unknown synthetic supervisor action')
    # Same pre-mutation checks and write primitives as the real applier, with an
    # explicit crash-injection hold before the real committed marker is written.
    module.verify_supervisor(plan, directory)
    module.validate_graph(False)
    module.verify_survival(plan['before'])
    target = Path(plan['unitPath'])
    original = (directory / 'original.unit').read_bytes()
    replacement = (directory / 'replacement.unit').read_bytes()
    if (module.sha(original) != plan['originalSha256']
            or module.sha(replacement) != plan['replacementSha256']
            or target.read_bytes() != original
            or module.transform_runner_unit(original, module.HUB) != replacement):
        raise ValueError('Synthetic configuration changed before crash injection')
    module.atomic_file(target, replacement, plan['unitMode'])
    module.systemctl('daemon-reload')
    module.validate_graph(True)
    module.verify_survival(plan['before'])
    module.record(directory, 'worker-ready.json', {
        'pid': os.getpid(), 'generation': topology.generation(os.getpid()),
        'cgroup': module.prop(plan['supervisorUnit'], 'ControlGroup'),
    })
    while True:
        time.sleep(1)


class SupervisorTests(topology.ServiceTopologyTests):
    # Reuse the guarded fixture and heartbeat assertions, not its already-covered
    # dependency-cascade testcase. This file contains exactly the two new proofs.
    test_requires_cascades_and_wants_preserves_runner_and_child = None

    def setUp(self):
        super().setUp()
        self.supervisor = self.namespace + '-worker.service'
        self.supervisor_path = self.unit_dir / self.supervisor
        self.supervisor_created = False
        self.addCleanup(self.cleanup_supervisor)
        self.assertEqual(self.supervisor_prop('LoadState'), 'not-found')
        self.command('start', self.runner)
        topology.wait_for(lambda: (self.root / 'processes.json').exists(), 'synthetic Runner registration')
        self.children = self.process_record()
        self.assert_survives_with_output(self.children)
        self.hub_pid = int(self.prop(self.hub, 'MainPID'))
        self.hub_generation = topology.generation(self.hub_pid)
        source = Path(__file__).with_name('peer-service-config.py').read_bytes()
        (self.root / 'applier.py').write_bytes(source)
        self.module = import_source('peer_supervisor_test_primitives', self.root / 'applier.py')
        original = (self.unit_dir / self.runner).read_bytes()
        replacement = self.module.transform_runner_unit(original, self.hub)
        self.module.atomic_file(self.root / 'original.unit', original, 0o600)
        self.module.atomic_file(self.root / 'replacement.unit', replacement, 0o600)
        before = {'units': {}, 'processes': []}
        for unit in (self.hub, self.runner):
            before['units'][unit] = {'mainPid': int(self.prop(unit, 'MainPID')),
                                     'cgroup': self.prop(unit, 'ControlGroup')}
        for pid in (self.hub_pid, self.children['runnerPid'], self.children['childPid']):
            before['processes'].append(self.module.process(pid))
        self.plan = {'version': 1, 'unitPath': str(self.unit_dir / self.runner),
                     'unitMode': stat.S_IMODE((self.unit_dir / self.runner).stat().st_mode),
                     'originalSha256': self.module.sha(original),
                     'replacementSha256': self.module.sha(replacement), 'before': before,
                     'supervisorUnit': self.supervisor, 'applierSha256': self.module.sha(source),
                     'syntheticNamespace': self.namespace, 'syntheticHub': self.hub,
                     'syntheticRunner': self.runner}
        self.module.record(self.root, 'synthetic-manifest.json', self.plan)

    def supervisor_command(self, verb, *options, allow_failure=False):
        expected = self.namespace + '-worker.service'
        if (self.supervisor != expected
                or not re.fullmatch(r'hapi-peer-topology-test-[0-9a-f]{32}-worker\.service', expected)):
            raise ValueError('Refusing a supervisor outside this isolated fixture')
        if verb not in ('show', 'start', 'stop', 'reset-failed'):
            raise ValueError('Unexpected synthetic supervisor operation')
        if any(not option.startswith('--') or option == '--all' for option in options):
            raise ValueError('Unexpected synthetic supervisor option')
        result = subprocess.run(['systemctl', '--user', verb, self.supervisor, *options],
                                capture_output=True, text=True, timeout=15)
        if result.returncode and not allow_failure:
            self.fail('Synthetic supervisor command failed: ' + result.stderr.strip())
        return result.stdout.strip()

    def supervisor_prop(self, key):
        return self.supervisor_command('show', '--property=' + key, '--value')

    def cleanup_supervisor(self):
        if not self.supervisor_created:
            return
        self.supervisor_command('stop')
        self.supervisor_command('reset-failed', allow_failure=True)
        self.assertEqual(self.supervisor_path.parent, self.unit_dir)
        self.assertEqual(self.supervisor_path.name, self.namespace + '-worker.service')
        self.assertTrue(self.supervisor_path.read_text().startswith('# Owner: ' + self.namespace + '\n'))
        self.supervisor_path.unlink()
        self.reload()

    def start_supervisor(self, action):
        def invocation(command):
            return ' '.join(topology.unit_quote(value) for value in
                            (sys.executable, Path(__file__).resolve(), '--synthetic-worker', command, self.root))
        content = (f'# Owner: {self.namespace}\n[Unit]\nDescription=Isolated configuration recovery proof\n'
                   '[Service]\nType=exec\nRestart=no\nRuntimeMaxSec=60\nTimeoutStopSec=10\n'
                   + 'ExecStart=' + invocation(action) + '\nExecStopPost=' + invocation('recover') + '\n')
        with self.supervisor_path.open('x') as output:
            output.write(content)
        self.supervisor_created = True
        self.reload()
        self.supervisor_command('start')

    def assert_protected_processes(self):
        self.assertEqual(self.prop(self.hub, 'ActiveState'), 'active')
        self.assertEqual(int(self.prop(self.hub, 'MainPID')), self.hub_pid)
        self.assertEqual(topology.generation(self.hub_pid), self.hub_generation)
        return self.assert_survives_with_output(self.children)

    def wait_result(self):
        topology.wait_for(lambda: (self.root / 'result.json').exists(), 'real ExecStopPost recovery result')
        return json.loads((self.root / 'result.json').read_text())

    def test_sigkill_independent_worker_restores_original_without_service_restart(self):
        self.start_supervisor('hold')
        topology.wait_for(lambda: (self.root / 'worker-ready.json').exists(), 'supervised mutation hold')
        ready = json.loads((self.root / 'worker-ready.json').read_text())
        self.assertEqual(self.supervisor_prop('ActiveState'), 'active')
        self.assertEqual(int(self.supervisor_prop('MainPID')), ready['pid'])
        self.assertEqual(topology.generation(ready['pid']), ready['generation'])
        self.assertEqual(self.supervisor_prop('ControlGroup'), ready['cgroup'])
        self.assertIn(self.supervisor, ready['cgroup'])
        for protected in (self.hub, self.runner):
            group = self.prop(protected, 'ControlGroup')
            self.assertFalse(ready['cgroup'] == group or ready['cgroup'].startswith(group + '/'))
        self.assertNotIn(ready['pid'], (self.hub_pid, self.children['runnerPid'], self.children['childPid']))
        self.assertNotIn(self.hub, self.prop(self.runner, 'Requires').split())
        self.assertGreater(ready['pid'], 1)
        os.kill(ready['pid'], signal.SIGKILL)
        result = self.wait_result()
        self.assertEqual(result['status'], 'restored')
        self.assertEqual(result['serviceRestarts'], 0)
        self.assertEqual((self.unit_dir / self.runner).read_bytes(), (self.root / 'original.unit').read_bytes())
        self.assertIn(self.hub, self.prop(self.runner, 'Requires').split())
        count = self.assert_protected_processes()
        print(json.dumps({'proof': 'supervised-config-sigkill-recovery', 'namespace': self.namespace,
                          'applierSha256': self.plan['applierSha256'], 'killedOnlySupervisorPid': ready['pid'],
                          'supervisorCgroup': ready['cgroup'], 'result': result,
                          'preservedHubPid': self.hub_pid, 'preservedRunnerAndChild': self.children,
                          'capturedChildHeartbeat': count}), flush=True)

    def test_committed_marker_keeps_replacement_without_service_restart(self):
        self.start_supervisor('commit')
        result = self.wait_result()
        self.assertTrue((self.root / 'committed.json').exists())
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(result['serviceRestarts'], 0)
        self.assertEqual((self.unit_dir / self.runner).read_bytes(), (self.root / 'replacement.unit').read_bytes())
        self.assertNotIn(self.hub, self.prop(self.runner, 'Requires').split())
        self.assertIn(self.hub, self.prop(self.runner, 'Wants').split())
        count = self.assert_protected_processes()
        print(json.dumps({'proof': 'supervised-config-commit-preserved', 'namespace': self.namespace,
                          'applierSha256': self.plan['applierSha256'], 'result': result,
                          'preservedHubPid': self.hub_pid, 'preservedRunnerAndChild': self.children,
                          'capturedChildHeartbeat': count}), flush=True)


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--synthetic-worker':
        synthetic_worker(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main(verbosity=2)
