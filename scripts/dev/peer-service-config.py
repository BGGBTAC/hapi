#!/usr/bin/env python3
"""Detach Hub stop propagation without stopping or restarting any HAPI process.

The live application already matches the reviewed build. This module changes only
one Runner Unit dependency. An independent systemd service supervises the write;
ExecStopPost restores the original file unless the verified commit was persisted.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from uuid import uuid4

HUB = 'hapi-hub.service'
RUNNER = 'hapi-runner.service'


def transform_runner_unit(contents: bytes, hub_unit: str) -> bytes:
    if not re.fullmatch(r'[a-zA-Z0-9_.@-]+\.service', hub_unit):
        raise ValueError('Invalid Hub unit name')
    lines = contents.splitlines(keepends=True)
    section = None
    changed = 0
    expected = b'Requires=' + hub_unit.encode()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(b'['):
            section = stripped
        if section == b'[Unit]' and stripped == expected:
            lines[index] = line.replace(b'Requires=', b'Wants=', 1)
            changed += 1
    if changed != 1:
        raise ValueError('Expected exactly one explicit Runner Requires=Hub line')
    return b''.join(lines)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def systemctl(*arguments: str) -> str:
    # This applier has no target-service stop/start/restart operation, including
    # its recovery path. Only the independent supervisor is launched separately.
    if not arguments or arguments[0] not in ('show', 'daemon-reload'):
        raise RuntimeError('Only read-only queries and daemon-reload are permitted')
    result = subprocess.run(['systemctl', '--user', *arguments], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError('systemd query/reload failed; raw diagnostics withheld')
    return result.stdout.strip()


def prop(unit: str, key: str) -> str:
    return systemctl('show', unit, f'--property={key}', '--value')


def process(pid: int) -> dict | None:
    try:
        path = Path('/proc') / str(pid)
        fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'generation': fields[19], 'exe': str((path / 'exe').readlink())}
    except (OSError, ValueError):
        return None


def healthy() -> bool:
    try:
        with urllib.request.urlopen('http://127.0.0.1:3006/health', timeout=3) as response:
            return response.status == 200 and json.load(response).get('capabilities', {}).get('peerMessages') is True
    except (OSError, ValueError):
        return False


def atomic_file(path: Path, data: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def record(directory: Path, name: str, value: dict) -> None:
    atomic_file(directory / name, (json.dumps(value, indent=2) + '\n').encode(), 0o600)


def snapshot() -> dict:
    units, processes = {}, {}
    for unit in (HUB, RUNNER):
        if prop(unit, 'ActiveState') != 'active':
            raise RuntimeError('Both existing services must be active')
        main = int(prop(unit, 'MainPID'))
        group = prop(unit, 'ControlGroup')
        units[unit] = {'mainPid': main, 'cgroup': group}
        for text in (Path('/sys/fs/cgroup' + group) / 'cgroup.procs').read_text().split():
            item = process(int(text))
            if item and (item['pid'] == main or Path(item['exe']).name == 'hapi'):
                processes[str(item['pid'])] = item
    if not healthy():
        raise RuntimeError('Existing Hub health check failed')
    return {'units': units, 'processes': list(processes.values())}


def verify_survival(before: dict) -> None:
    for unit, data in before['units'].items():
        if prop(unit, 'ActiveState') != 'active' or int(prop(unit, 'MainPID')) != data['mainPid']:
            raise RuntimeError('An existing service changed state or MainPID')
    for expected in before['processes']:
        if process(expected['pid']) != expected:
            raise RuntimeError('An observed existing HAPI process changed or exited')
    if not healthy():
        raise RuntimeError('Hub health check failed after configuration reload')


def validate_graph(after: bool) -> None:
    required = set(prop(HUB, 'RequiredBy').split())
    if required != (set() if after else {RUNNER}):
        raise RuntimeError('Unexpected Hub RequiredBy topology')
    for key in ('BoundBy', 'ConsistsOf', 'PropagatesStopTo'):
        if prop(HUB, key):
            raise RuntimeError('Unexpected additional Hub stop propagation')
    for key in ('BindsTo', 'PartOf', 'StopPropagatedFrom'):
        if prop(RUNNER, key):
            raise RuntimeError('Unexpected additional Runner stop dependency')
    requires = set(prop(RUNNER, 'Requires').split())
    if (HUB in requires) == after:
        raise RuntimeError('Runner hard dependency does not match expected phase')
    if after and HUB not in prop(RUNNER, 'Wants').split():
        raise RuntimeError('Runner no longer requests Hub startup')
    if prop(RUNNER, 'KillMode') != 'control-group':
        raise RuntimeError('Runner KillMode changed unexpectedly')


def prepare() -> tuple[dict, bytes, bytes]:
    if socket.gethostname() != 'dev-main' or os.getuid() != 1000:
        raise RuntimeError('Restricted to the authorized dev-main service owner')
    path = Path.home() / '.config/systemd/user' / RUNNER
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError('Runner unit must be an owned regular file')
    for unit in (HUB, RUNNER):
        if prop(unit, 'FragmentPath') != str(path.parent / unit) or prop(unit, 'DropInPaths'):
            raise RuntimeError('Unexpected unit location or existing drop-ins')
    validate_graph(False)
    original = path.read_bytes()  # Opaque unit configuration; never emitted.
    replacement = transform_runner_unit(original, HUB)
    return ({'version': 1, 'unitPath': str(path), 'unitMode': stat.S_IMODE(info.st_mode),
        'originalSha256': sha(original), 'replacementSha256': sha(replacement),
        'before': snapshot()}, original, replacement)


def load_manifest(path: Path) -> tuple[dict, Path]:
    path = path.resolve(strict=True)
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('Private owned manifest required')
    directory = path.parent
    if stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise RuntimeError('Private deployment directory required')
    plan = json.loads(path.read_text())
    if plan['version'] != 1 or Path(plan['unitPath']) != Path.home() / '.config/systemd/user' / RUNNER:
        raise RuntimeError('Unexpected deployment target')
    if not re.fullmatch(r'hapi-peer-config-[a-f0-9]{16}\.service', plan['supervisorUnit']):
        raise RuntimeError('Unexpected supervisor unit')
    return plan, directory


def verify_supervisor(plan: dict, directory: Path) -> None:
    unit = plan['supervisorUnit']
    own = Path('/proc/self/cgroup').read_text().strip().split('::', 1)[-1]
    expected = prop(unit, 'ControlGroup')
    if not expected or own != expected or unit not in own:
        raise RuntimeError('Execution must run in its independent supervisor unit')
    for protected in (HUB, RUNNER):
        group = prop(protected, 'ControlGroup')
        if own == group or own.startswith(group + '/'):
            raise RuntimeError('Supervisor shares a protected service cgroup')
        for key in ('Requires', 'BindsTo', 'PartOf', 'After', 'StopPropagatedFrom'):
            if protected in prop(unit, key).split():
                raise RuntimeError('Supervisor depends on a protected service')
    if prop(unit, 'RuntimeMaxUSec') != '1min' or not prop(unit, 'ExecStopPost'):
        raise RuntimeError('Independent supervision/recovery is missing')
    if sha((directory / 'applier.py').read_bytes()) != plan['applierSha256']:
        raise RuntimeError('Staged applier changed')


def apply(plan: dict, directory: Path) -> None:
    verify_supervisor(plan, directory)
    validate_graph(False)
    verify_survival(plan['before'])
    target = Path(plan['unitPath'])
    original = (directory / 'original.unit').read_bytes()
    replacement = (directory / 'replacement.unit').read_bytes()
    if sha(original) != plan['originalSha256'] or sha(replacement) != plan['replacementSha256']:
        raise RuntimeError('Staged unit checksum mismatch')
    if target.read_bytes() != original or transform_runner_unit(original, HUB) != replacement:
        raise RuntimeError('Unit changed since preparation')
    record(directory, 'phase.json', {'phase': 'supervisor-ready', 'at': time.time()})
    atomic_file(target, replacement, plan['unitMode'])
    systemctl('daemon-reload')
    validate_graph(True)
    verify_survival(plan['before'])
    record(directory, 'committed.json', {'phase': 'committed', 'at': time.time(), 'configOnly': True,
        'serviceRestarts': 0, 'preservedProcesses': plan['before']['processes']})


def recover(plan: dict, directory: Path) -> None:
    # Runs as ExecStopPost even after failure, SIGKILL, or RuntimeMaxSec. It never
    # stops, starts or restarts target services. No completed commit is reverted.
    verify_supervisor(plan, directory)
    target = Path(plan['unitPath'])
    current = target.read_bytes()
    if (directory / 'committed.json').exists():
        if sha(current) != plan['replacementSha256']:
            raise RuntimeError('Committed unit was changed externally')
        validate_graph(True)
        verify_survival(plan['before'])
        record(directory, 'result.json', {'status': 'committed', 'at': time.time(), 'serviceRestarts': 0})
        return
    if sha(current) not in (plan['originalSha256'], plan['replacementSha256']):
        raise RuntimeError('External unit edit detected; refusing to overwrite it')
    original = (directory / 'original.unit').read_bytes()
    if sha(original) != plan['originalSha256']:
        raise RuntimeError('Original unit checksum mismatch')
    atomic_file(target, original, plan['unitMode'])
    systemctl('daemon-reload')
    verify_survival(plan['before'])
    record(directory, 'result.json', {'status': 'restored', 'at': time.time(), 'serviceRestarts': 0})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('inspect', 'launch', 'apply', 'recover'))
    parser.add_argument('--manifest', type=Path)
    args = parser.parse_args()
    if args.action in ('apply', 'recover'):
        if args.manifest is None:
            parser.error('Internal supervised action requires a manifest')
        plan, directory = load_manifest(args.manifest)
        (apply if args.action == 'apply' else recover)(plan, directory)
        return
    plan, original, replacement = prepare()
    if args.action == 'inspect':
        print(json.dumps(plan))
        return
    os.umask(0o077)
    root = Path.home() / '.local/state/hapi-peer-config'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = uuid4().hex[:16]
    directory = root / identifier
    directory.mkdir(mode=0o700)
    atomic_file(directory / 'original.unit', original, 0o600)
    atomic_file(directory / 'replacement.unit', replacement, 0o600)
    source = Path(__file__).read_bytes()
    atomic_file(directory / 'applier.py', source, 0o600)
    plan.update({'supervisorUnit': f'hapi-peer-config-{identifier}.service', 'applierSha256': sha(source)})
    record(directory, 'manifest.json', plan)
    command = ['/usr/bin/python3', str(directory / 'applier.py')]
    manifest = str(directory / 'manifest.json')
    if any(not re.fullmatch(r'[A-Za-z0-9/._-]+', item) for item in (*command, manifest)):
        raise RuntimeError('Unexpected supervisor path escaping requirement')
    subprocess.run(['systemd-run', '--user', '--unit', plan['supervisorUnit'], '--property=Type=exec',
        '--property=RuntimeMaxSec=60', '--property=TimeoutStopSec=10', '--property=Restart=no',
        '--property=StandardOutput=journal', '--property=StandardError=journal',
        '--property=ExecStopPost=' + ' '.join([*command, 'recover', '--manifest', manifest]),
        *command, 'apply', '--manifest', manifest], check=True)
    print(json.dumps({'supervisorUnit': plan['supervisorUnit'], 'directory': str(directory)}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Configuration-only rollout failed: {type(error).__name__}: {error}', file=sys.stderr)
        sys.exit(1)
