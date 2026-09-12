#!/usr/bin/env python3
"""Activate a reviewed peer release on the authorized dev-main user services.

Default is read-only preparation. Live activation is disabled after the 2026-09-12 outage.
No SQLite access, plaintext secret output, global npm replacement, or automatic downgrade.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

HUB = 'hapi-hub.service'
RUNNER = 'hapi-runner.service'
DROP_NAME = '90-peer-release.conf'


def run(*args: str) -> str:
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
    if result.returncode:
        # Commands here only query public process/unit metadata or operate services.
        # Never invoke `systemctl status`, `cat`, Environment, or journal output.
        raise RuntimeError(f'{args[0]} failed ({result.returncode}): {result.stderr.strip()[:2000]}')
    return result.stdout.strip()


def prop(unit: str, name: str) -> str:
    return run('systemctl', '--user', 'show', unit, f'--property={name}', '--value')


def digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def process(pid: int) -> dict | None:
    try:
        root = Path('/proc') / str(pid)
        fields = (root / 'stat').read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'parent': int(fields[1]), 'generation': fields[19], 'exe': str((root / 'exe').readlink())}
    except (OSError, ValueError):
        return None


def unchanged(snapshot: dict) -> bool:
    current = process(snapshot['pid'])
    return current is not None and current['generation'] == snapshot['generation']


def process_start(pid: int) -> str | None:
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5,
        env={**os.environ, 'LC_ALL': 'C', 'TZ': 'UTC'})
    return result.stdout.strip() if result.returncode == 0 else None


def wait_for(check, timeout: float, description: str) -> None:
    end = time.monotonic() + timeout
    while not check():
        if time.monotonic() >= end:
            raise RuntimeError(f'Timed out: {description}')
        time.sleep(0.2)


def emit(phase: str, **details) -> None:
    print(json.dumps({'phase': phase, **details}), flush=True)


def health(url: str, require_peer: bool = True) -> bool:
    try:
        with urllib.request.urlopen(url + '/health', timeout=2) as response:
            payload = json.load(response)
            return response.status == 200 and (not require_peer or payload.get('capabilities', {}).get('peerMessages') is True)
    except (OSError, ValueError):
        return False


def unit_state(unit: str) -> dict:
    return {name: prop(unit, name) for name in ('ActiveState', 'SubState', 'Result', 'ExecMainStatus', 'MainPID')}


def assert_dropins(unit: str, directory: Path, allowed: tuple[Path, ...] = ()) -> None:
    known = set(allowed)
    # systemd also loads drop-ins from /run, /etc and vendor directories.
    loaded = prop(unit, 'DropInPaths').split()
    paths = set((directory / (unit + '.d')).glob('*.conf')) | {Path(path) for path in loaded}
    if any(path not in known and path.name >= DROP_NAME for path in paths):
        raise RuntimeError(f'{unit}: a foreign drop-in sorts at/after the peer release; refusing to override it')
    if any(path.parent != directory / (unit + '.d') for path in paths):
        raise RuntimeError(f'{unit}: nonlocal drop-ins require a separate reviewed backup plan')


def assert_exec(unit: str, executable: Path, arguments: str) -> None:
    actual = prop(unit, 'ExecStart')
    prefix = '{ path=' + str(executable) + ' ; argv[]=' + str(executable) + ' ' + arguments + ' ; ignore_errors=no ; '
    if not actual.startswith(prefix) or actual.count('{ path=') != 1 or not actual.endswith(' }'):
        # Do not echo unexpected arguments: they could have been configured as secrets.
        raise RuntimeError(f'{unit}: ExecStart differs from the explicitly expected invocation')


def unit_members(unit: str) -> list[dict]:
    group = Path('/sys/fs/cgroup' + prop(unit, 'ControlGroup')) / 'cgroup.procs'
    return [entry for pid in group.read_text().split() if (entry := process(int(pid)))]


def fd_targets(members: list[dict]) -> set[str]:
    targets = set()
    for member in members:
        for fd in (Path('/proc') / str(member['pid']) / 'fd').glob('*'):
            try:
                targets.add(str(fd.readlink()))
            except OSError:
                pass
    return targets


def assert_listener(members: list[dict], port: int) -> None:
    targets = fd_targets(members)
    sockets = {target for target in targets if target.startswith('socket:[')}
    for table in ('/proc/net/tcp', '/proc/net/tcp6'):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and int(fields[1].rsplit(':', 1)[1], 16) == port and f'socket:[{fields[9]}]' in sockets:
                return
    raise RuntimeError('Expected loopback control/Hub port is not owned by the verified service process')


def assert_hub_resources(database: Path, members: list[dict], url: str) -> None:
    targets = fd_targets(members)
    if str(database) not in targets:
        raise RuntimeError('Actual Hub database file descriptor does not match the expected backup database')
    opened_databases = {target for target in targets if target.endswith(('.db', '.sqlite', '.sqlite3'))}
    if opened_databases != {str(database)}:
        raise RuntimeError('Hub has unexpected database file descriptors; backup scope must be reviewed')
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment or not parsed.port:
        raise RuntimeError('--url must be the explicitly known http://127.0.0.1:PORT Hub endpoint')
    assert_listener(members, parsed.port)
    if not health(url, require_peer=False):
        raise RuntimeError('Expected Hub health endpoint is not healthy before rollout')


def required_space(home: Path, paths: list[str], binary: Path, destination: Path) -> dict:
    size = 0
    files = 0
    for item in paths:
        root = home / item
        entries = [root]
        if root.is_dir() and not root.is_symlink():
            for directory, dirs, names in os.walk(root, followlinks=False):
                entries.extend(Path(directory) / name for name in names)
                entries.extend(Path(directory) / name for name in dirs)
        for entry in entries:
            info = entry.lstat()
            files += 1
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
    existing = destination
    while not existing.exists():
        existing = existing.parent
    # Incompressible tar + binary SOPS/base64 expansion, tar headers, release and headroom.
    needed = 2 * (size + files * 1024) + binary.stat().st_size + 512 * 1024 * 1024
    available = shutil.disk_usage(existing).free
    if available < needed:
        raise RuntimeError(f'Insufficient backup disk space: need {needed} bytes, have {available}')
    return {'sourceBytes': size, 'requiredBytes': needed, 'freeBytes': available}


def database_snapshot(database: Path) -> dict[str, tuple]:
    result = {}
    for path in (database, Path(str(database) + '-wal'), Path(str(database) + '-shm')):
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path == database:
                raise
            continue
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError('Database snapshot contains a symlink or nonregular file')
        result[str(path)] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    return result


def validate_envelope(path: Path, recipients: set[str]) -> None:
    # SOPS binary output has one potentially huge ciphertext string followed by
    # small JSON metadata. Read bounded ends, never load the complete backup.
    with path.open('rb') as source:
        prefix = source.read(512)
        source.seek(max(0, path.stat().st_size - 1024 * 1024))
        tail = source.read(1024 * 1024)
    if not re.match(rb'\s*\{\s*"data"\s*:\s*"ENC\[AES256_GCM,data:', prefix):
        raise RuntimeError('Invalid SOPS binary ciphertext envelope')
    boundary = re.search(rb'"\s*,\s*"sops"\s*:', tail)
    if not boundary:
        raise RuntimeError('Missing/oversized SOPS metadata')
    envelope = json.loads(b'{"sops":' + tail[boundary.end():])
    metadata = envelope.get('sops')
    if not isinstance(metadata, dict) or set(envelope) != {'sops'}:
        raise RuntimeError('Invalid SOPS metadata')
    age = metadata.get('age')
    actual = [entry.get('recipient') for entry in age] if isinstance(age, list) and all(isinstance(entry, dict) for entry in age) else []
    if len(actual) != len(recipients) or set(actual) != recipients:
        raise RuntimeError('SOPS age recipients do not exactly match the required public recipients')
    if any(metadata.get(backend) for backend in ('kms', 'gcp_kms', 'azure_kv', 'hc_vault', 'pgp', 'key_groups')):
        raise RuntimeError('Unexpected additional SOPS key recipients')
    if not all(isinstance(entry.get('enc'), str) and entry['enc'].startswith('-----BEGIN AGE ENCRYPTED FILE-----') for entry in age):
        raise RuntimeError('Invalid age encrypted key metadata')


def terminate_pipeline(processes: list[subprocess.Popen]) -> None:
    for child in processes:
        if child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for child in processes:
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=5)


def encrypted_backup(home: Path, paths: list[str], database: Path, backup: Path,
                     sops: Path, infra: Path, timestamp: str, recipients: set[str], timeout: float) -> dict:
    before = database_snapshot(database)
    partial = backup.with_suffix(backup.suffix + '.partial')
    members_path = backup.with_suffix(backup.suffix + '.members')
    children = []
    started = time.monotonic()
    try:
        with partial.open('xb') as encrypted, members_path.open('x') as members, tempfile.TemporaryFile() as tar_errors, tempfile.TemporaryFile() as sops_errors:
            archive = subprocess.Popen(['tar', '--warning=no-file-changed', '--warning=no-file-removed', '--index-file=/dev/fd/' + str(members.fileno()),
                '-czvf', '-', '-C', str(home), '--', *paths], stdout=subprocess.PIPE, stderr=tar_errors,
                pass_fds=(members.fileno(),), start_new_session=True)
            children.append(archive)
            assert archive.stdout is not None
            try:
                encryption = subprocess.Popen([str(sops), '--encrypt', '--input-type', 'binary', '--output-type', 'binary',
                    '--config', str(infra / '.sops.yaml'), '--filename-override', str(infra / 'secrets' / f'hapi-rollback-{timestamp}.tar.gz.enc'),
                    '/dev/stdin'], stdin=archive.stdout, stdout=encrypted, stderr=sops_errors, start_new_session=True)
                children.append(encryption)
            finally:
                archive.stdout.close()
            wait_for(lambda: all(child.poll() is not None for child in children), timeout, 'tar to SOPS backup pipeline')
            if encryption.returncode or archive.returncode not in (0, 1) or tar_errors.tell() or sops_errors.tell():
                raise RuntimeError('Tar/SOPS backup failed or reported diagnostics; no migration attempted')
            encrypted.flush()
            os.fsync(encrypted.fileno())
        if database_snapshot(database) != before:
            raise RuntimeError('Stopped database/WAL/SHM changed during backup; no migration attempted')
        required = {str(Path(path).relative_to(home)) for path in before}
        found = set()
        with members_path.open() as members:
            for line in members:
                name = line.rstrip('\n')
                if name in required:
                    found.add(name)
        if found != required:
            raise RuntimeError('Tar membership receipt does not include the database and every existing WAL/SHM')
        validate_envelope(partial, recipients)
        os.replace(partial, backup)
        return {'path': str(backup), 'sha256': digest(backup), 'bytes': backup.stat().st_size,
            'membersPath': str(members_path), 'membersSha256': digest(members_path), 'databaseMembers': sorted(found),
            'ageRecipients': sorted(recipients), 'seconds': round(time.monotonic() - started, 2),
            'decryptRestoreTest': False, 'reason': 'Private age identity remains on owner Mac'}
    finally:
        terminate_pipeline(children)


def runner_state(home: Path) -> dict:
    # This documented local state contains identity hashes, never token values.
    # Only PID and port are retained, emitted or used.
    with (home / 'runner.state.json').open() as source:
        raw = json.load(source)
    pid, port = raw.get('pid'), raw.get('httpPort')
    if type(pid) is not int or pid <= 0 or type(port) is not int or not 0 < port < 65536:
        raise RuntimeError('Invalid runner state PID/HTTP port')
    return {'pid': pid, 'httpPort': port}


def runner_children(home: Path, expected: dict) -> list[dict]:
    state = runner_state(home)
    if state['pid'] != expected['pid'] or not unchanged(expected):
        raise RuntimeError('Runner state disagrees with the verified service process generation')
    assert_listener([expected], state['httpPort'])
    request = urllib.request.Request(f'http://127.0.0.1:{state["httpPort"]}/list', data=b'{}',
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(request, timeout=5) as response:
        body = json.load(response)
    children = body.get('children')
    if not isinstance(children, list):
        raise RuntimeError('Invalid local runner /list response')
    result = []
    for child in children:
        if not isinstance(child, dict) or not isinstance(child.get('happySessionId'), str) or not child['happySessionId'] or type(child.get('pid')) is not int:
            raise RuntimeError('Invalid local runner child identity')
        result.append({'happySessionId': child['happySessionId'], 'pid': child['pid']})
    return result


def snapshot_children(home: Path, runner: dict) -> list[dict]:
    result = []
    for child in runner_children(home, runner):
        current = process(child['pid'])
        marker = process_start(child['pid']) if current else None
        if current and marker and unchanged(current):
            result.append({**current, **child, 'processStartMarker': marker})
        elif current and unchanged(current):
            raise RuntimeError('Cannot verify the start marker of a living runner child')
        else:
            emit('session-ended-before-snapshot', pid=child['pid'], sessionId=child['happySessionId'])
    return result


def merge_resume_records(home: Path, snapshots: list[dict]) -> list[dict]:
    path = home / 'runner.state.json.resume-processes.json'
    existing = json.loads(path.read_text()) if path.exists() else []
    if not isinstance(existing, list) or not all(isinstance(entry, dict) for entry in existing):
        raise RuntimeError('Existing runner resume persistence is invalid; refusing to overwrite it')
    records = list(existing)
    adopted = []
    for child in snapshots:
        if not unchanged(child):
            emit('session-ended-during-transition', pid=child['pid'], sessionId=child['happySessionId'])
            continue
        if process_start(child['pid']) != child['processStartMarker']:
            if not unchanged(child):
                emit('session-ended-during-transition', pid=child['pid'], sessionId=child['happySessionId'])
                continue
            raise RuntimeError('Living runner child start marker changed or could not be verified')
        matching = [record for record in records if record.get('pid') == child['pid']]
        current = next((record for record in matching if record.get('processStartMarker') == child['processStartMarker']), None)
        # A PID reused since a prior record cannot inherit the stale session ID.
        requested = current.get('requestedSessionId', current.get('sessionId')) if current else None
        record = {'requestedSessionId': requested if isinstance(requested, str) and requested else child['happySessionId'],
            'confirmedSessionId': child['happySessionId'], 'pid': child['pid'], 'processStartMarker': child['processStartMarker']}
        records = [item for item in records if item.get('pid') != child['pid']]
        records.append(record)
        adopted.append(child)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=home)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(records, output)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return adopted


def stable_runner(home: Path, release: Path, timeout: float = 45, stable_seconds: float = 5) -> dict:
    candidate = None
    since = None
    result = None

    def ready() -> bool:
        nonlocal candidate, since, result
        try:
            actual = process(int(prop(RUNNER, 'MainPID')))
            state = runner_state(home)
            valid = prop(RUNNER, 'ActiveState') == 'active' and actual and actual['exe'] == str(release) and state['pid'] == actual['pid']
            if not valid:
                candidate = since = None
                return False
            if candidate != (actual['pid'], actual['generation']):
                candidate, since = (actual['pid'], actual['generation']), time.monotonic()
            result = actual
            return time.monotonic() - since >= stable_seconds
        except (OSError, ValueError, RuntimeError):
            candidate = since = None
            return False

    wait_for(ready, timeout, 'new runner PID stable for at least five seconds and matching runner.state.json')
    assert result is not None
    return result


def activate_runner(unit_dir: Path, runner_drop: Path, transition: Path, config: str,
                    release: Path, arguments: str, hapi_home: Path) -> dict:
    assert_dropins(RUNNER, unit_dir, (runner_drop, transition))
    runner_drop.parent.mkdir(parents=True, exist_ok=True)
    runner_drop.write_text(config)
    transition.unlink(missing_ok=True)
    run('systemctl', '--user', 'daemon-reload')
    assert_exec(RUNNER, release, arguments)
    if prop(RUNNER, 'KillMode') != 'process' or prop(RUNNER, 'Restart') != 'always' or prop(RUNNER, 'RestartUSec') != '5s':
        raise RuntimeError('Final runner supervision/session protections did not apply')
    previous = unit_state(RUNNER)
    emit('runner-before-start', **previous)
    if previous['ActiveState'] == 'failed':
        # The old runner's bounded SIGTERM cleanup may exit 1. Record it before
        # explicitly clearing failed/start-limit state; never hide this outcome.
        run('systemctl', '--user', 'reset-failed', RUNNER)
    run('systemctl', '--user', 'start', RUNNER)
    return stable_runner(hapi_home, release)


def verify_adoption(home: Path, runner: dict, snapshots: list[dict]) -> dict:
    children = runner_children(home, runner)
    present = {(child['pid'], child['happySessionId']) for child in children}
    alive, ended, missing = [], [], []
    for child in snapshots:
        if not unchanged(child):
            ended.append(child['pid'])
        elif (child['pid'], child['happySessionId']) in present:
            alive.append(child['pid'])
        else:
            missing.append(child['pid'])
    emit('runner-session-adoption', controllableSessionProcesses=alive, endedSessionProcesses=ended, missingSessionProcesses=missing)
    if missing:
        raise RuntimeError('Surviving recorded sessions are missing from the new runner /list')
    return {'controllableSessionProcesses': alive, 'endedSessionProcesses': ended}


def recover_runner_after_signal(unit_dir: Path, runner_drop: Path, transition: Path, config: str,
                               release: Path, arguments: str, hapi_home: Path, runner: dict,
                               runner_main: dict, children: list[dict], start_attempted: bool) -> dict:
    recovery = {}
    if not start_attempted:
        try:
            wait_for(lambda: not unchanged(runner) and not unchanged(runner_main), 20, 'old runner exit before recovery merge')
            recovery['oldRunnerOutcome'] = unit_state(RUNNER)
            merge_resume_records(hapi_home, children)
        except BaseException as error:
            recovery['resumeRecoveryError'] = str(error)
    # Separate try: a merge failure must never prevent final supervision being
    # restored. Once a start was attempted, do not race a live runner's registry.
    try:
        actual = activate_runner(unit_dir, runner_drop, transition, config, release, arguments, hapi_home)
        recovery['runnerPid'] = actual['pid']
        recovery.update(verify_adoption(hapi_home, actual, children))
    except BaseException as error:
        recovery['runnerRecoveryError'] = str(error)
    return recovery


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--infra-root', required=True, type=Path)
    parser.add_argument('--sops', required=True, type=Path)
    parser.add_argument('--expected-age-recipient', required=True, action='append')
    parser.add_argument('--url', required=True, help='Explicitly known loopback Hub address; ownership is checked via /proc')
    parser.add_argument('--expected-hapi-home', type=Path, default=Path.home() / '.hapi')
    parser.add_argument('--expected-workspace-root', type=Path, default=Path.home() / 'work')
    parser.add_argument('--backup-timeout', type=int, default=300)
    parser.add_argument('--health-timeout', type=int, default=180)
    parser.add_argument('--create-path-shim', action='store_true')
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    # A Hub stop propagates through the Runner's Requires dependency. When this
    # process belongs to that Runner's cgroup, systemd also terminates the deployer
    # before its exception handler can recover either service. Do not re-enable
    # this path without independent supervision and dependency-aware validation.
    if args.execute:
        parser.error('Live activation disabled after the 2026-09-12 outage; this script is read-only. See docs/guide/peer-incident-2026-09-12.md.')
    if socket.gethostname() != 'dev-main' or os.getuid() == 0:
        parser.error('This rollout is restricted to the existing non-root dev-main user service owner')
    if args.backup_timeout <= 0 or args.health_timeout <= 0:
        parser.error('Timeouts must be positive')
    recipients = set(args.expected_age_recipient)
    if len(recipients) != len(args.expected_age_recipient) or not all(re.fullmatch(r'age1[02-9ac-hj-np-z]{58}', value) for value in recipients):
        parser.error('Expected recipients must be distinct public age recipient strings')
    os.umask(0o077)
    home = Path.home()
    binary = args.binary.resolve(strict=True)
    infra = args.infra_root.resolve(strict=True)
    sops = args.sops.resolve(strict=True)
    hapi_home = args.expected_hapi_home.absolute()
    workspace = args.expected_workspace_root.resolve(strict=True)
    if hapi_home.is_symlink() or not hapi_home.is_dir() or hapi_home.parent != home:
        parser.error('Expected HAPI home must be an actual direct child directory of the service owner home')
    database = hapi_home / 'hapi.db'
    database_snapshot(database)
    checksum = digest(binary)
    release = home / '.local/share/hapi/releases' / ('peer-' + checksum[:16]) / 'hapi'
    if not all(re.fullmatch(r'[A-Za-z0-9/._-]+', str(path)) for path in (release, workspace)):
        parser.error('Release/workspace paths require a reviewed systemd escaping plan')
    arguments = f'runner start-sync --workspace-root {workspace}'
    unit_dir = home / '.config/systemd/user'
    hub_drop = unit_dir / (HUB + '.d') / DROP_NAME
    runner_drop = unit_dir / (RUNNER + '.d') / DROP_NAME
    transition = unit_dir / (RUNNER + '.d') / '99-peer-transition.conf'
    if any(os.path.lexists(path) for path in (hub_drop, runner_drop, transition)):
        parser.error('Peer drop-in already exists; inspect the previous rollout before modifying it')
    for unit, invocation in ((HUB, 'hub'), (RUNNER, arguments)):
        if prop(unit, 'ActiveState') != 'active':
            parser.error(f'{unit} must be active before the initial rollout')
        if prop(unit, 'FragmentPath') != str(unit_dir / unit):
            parser.error(f'{unit}: unexpected unit FragmentPath')
        if prop(unit, 'WorkingDirectory').lstrip('!') != str(home):
            parser.error(f'{unit}: unexpected WorkingDirectory')
        assert_exec(unit, Path('/usr/bin/hapi'), invocation)
        assert_dropins(unit, unit_dir)
    hub_main = process(int(prop(HUB, 'MainPID')))
    runner_main = process(int(prop(RUNNER, 'MainPID')))
    if not hub_main or not runner_main:
        parser.error('Missing service main process')
    assert_hub_resources(database, unit_members(HUB), args.url)
    members = unit_members(RUNNER)
    # Signal only the actual Bun runner, never a process group or recursive stop.
    runners = [entry for entry in members if entry['parent'] == runner_main['pid'] and Path(entry['exe']).name == 'hapi']
    if Path(runner_main['exe']).name == 'hapi':
        runners = [runner_main]
    if len(runners) != 1:
        parser.error('Cannot unambiguously identify the actual runner executable')
    runner = runners[0]
    children = snapshot_children(hapi_home, runner)
    agents = [entry for entry in members if Path(entry['exe']).name == 'hapi' and entry['pid'] not in (runner['pid'], runner_main['pid'])]
    target = infra / 'services/hapi/peer-release'
    timestamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    backup_dir = home / '.local/share/hapi/rollback'
    backup = backup_dir / f'pre-peer-{timestamp}.tar.gz.sops'
    paths = [str(hapi_home.relative_to(home)), '.config/systemd/user/' + HUB, '.config/systemd/user/' + RUNNER]
    paths += [str(path.relative_to(home)) for path in (hub_drop.parent, runner_drop.parent) if path.exists()]
    space = required_space(home, paths, binary, backup_dir)
    emit('prepared', binary=str(binary), sha256=checksum, release=str(release), database=str(database), url=args.url,
         hubMain=hub_main['pid'], runnerMain=runner_main['pid'], runner=runner['pid'],
         observedSessionProcesses=[entry['pid'] for entry in agents], trackedSessionProcesses=[entry['pid'] for entry in children],
         backup=str(backup), ageRecipients=sorted(recipients), space=space, createPathShim=args.create_path_shim, execute=args.execute)
    if not args.execute:
        return
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.mkdir(parents=True, exist_ok=True)
    release.parent.mkdir(parents=True, exist_ok=True)
    if release.exists() and digest(release) != checksum:
        raise RuntimeError('Existing release path has unexpected content')
    if not release.exists():
        shutil.copy2(binary, release)
        release.chmod(0o755)
    hub_config = f'[Service]\nExecStart=\nExecStart={release} hub\n'
    runner_config = (f'[Service]\nExecStart=\nExecStart={release} {arguments}\n'
        f'Environment=HAPI_CLI_EXECUTABLE={release}\nEnvironment=HAPI_DISABLE_VERSION_HANDOFF=1\nKillMode=process\nRestart=always\nRestartSec=5\n')
    migrated = False
    hub_stopped = False
    runner_signaled = False
    runner_start_attempted = False
    old_runner_outcome = None
    adopted = []
    started = time.monotonic()
    try:
        # Recheck assumptions immediately before downtime; preparation is read-only.
        assert_exec(HUB, Path('/usr/bin/hapi'), 'hub')
        assert_exec(RUNNER, Path('/usr/bin/hapi'), arguments)
        assert_dropins(HUB, unit_dir)
        assert_dropins(RUNNER, unit_dir)
        assert_hub_resources(database, unit_members(HUB), args.url)
        children = snapshot_children(hapi_home, runner)
        hub_stopped = True
        run('systemctl', '--user', 'stop', HUB)
        if prop(HUB, 'ActiveState') == 'active' or unchanged(hub_main):
            raise RuntimeError('Old hub did not stop')
        backup_receipt = encrypted_backup(home, paths, database, backup, sops, infra, timestamp, recipients, args.backup_timeout)
        emit('encrypted-backup', **backup_receipt)
        for name, content in [('hub.conf', hub_config), ('runner.conf', runner_config)]:
            (target / name).write_text(content)
        assert_dropins(HUB, unit_dir)
        hub_drop.parent.mkdir(parents=True, exist_ok=True)
        hub_drop.write_text(hub_config)
        run('systemctl', '--user', 'daemon-reload')
        assert_exec(HUB, release, 'hub')
        migrated = True  # Never automatically start the old binary after this boundary.
        run('systemctl', '--user', 'start', HUB)
        wait_for(lambda: health(args.url), args.health_timeout, 'new hub peer capability')
        hub_stopped = False
        emit('hub-active', downtimeSeconds=round(time.monotonic() - started, 2), release=str(release))
        assert_dropins(RUNNER, unit_dir)
        transition.parent.mkdir(parents=True, exist_ok=True)
        transition.write_text('[Service]\nKillMode=process\nRestart=no\n')
        run('systemctl', '--user', 'daemon-reload')
        if prop(RUNNER, 'KillMode') != 'process' or prop(RUNNER, 'Restart') != 'no':
            raise RuntimeError('Runner transition protections did not apply')
        if not unchanged(runner) or not unchanged(runner_main):
            raise RuntimeError('Runner generation changed during rollout; no signal sent')
        # Refresh /list immediately before the signal to include sessions started
        # while backup/migration ran. The verified generation snapshot gates merge.
        children = snapshot_children(hapi_home, runner)
        runner_signaled = True
        os.kill(runner['pid'], signal.SIGTERM)
        wait_for(lambda: not unchanged(runner) and not unchanged(runner_main), 20, 'old runner and launcher exit')
        old_runner_outcome = unit_state(RUNNER)
        emit('old-runner-exited', **old_runner_outcome)
        adopted = merge_resume_records(hapi_home, children)
        runner_start_attempted = True
        actual = activate_runner(unit_dir, runner_drop, transition, runner_config, release, arguments, hapi_home)
        adoption = verify_adoption(hapi_home, actual, children)
        ended = [entry['pid'] for entry in agents if not unchanged(entry)]
        emit('session-process-observation', surviving=[entry['pid'] for entry in agents if unchanged(entry)], ended=ended,
            limitation='Process checks do not prove terminal pipe continuity or agent workload health')
        shim_created = False
        if args.create_path_shim:
            shim = home / '.local/bin/hapi'
            shim.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(shim):
                raise RuntimeError('Explicit PATH shim target already exists; refusing to replace it')
            shim.symlink_to(release)
            shim_created = True
        receipt = {'binary': str(release), 'sha256': checksum, 'backup': backup_receipt,
                   'schema': 26, 'hubPeerMessages': health(args.url), 'runnerPid': actual['pid'],
                   'oldRunnerOutcome': old_runner_outcome, **adoption, 'endedObservedProcesses': ended, 'pathShimCreated': shim_created,
                   'restoreTest': 'Not performed: private age identity remains on owner Mac'}
        (target / 'activation.json').write_text(json.dumps(receipt, indent=2) + '\n')
        emit('activated', **receipt)
    except BaseException:
        recovery = {}
        if runner_signaled:
            # Always restore final supervision after SIGTERM, including ordinary
            # child exits and failed old cleanup. Each recovery action is attempted
            # even if resume merging fails; never leave Restart=no pinned in place.
            recovery.update(recover_runner_after_signal(unit_dir, runner_drop, transition, runner_config,
                release, arguments, hapi_home, runner, runner_main, children, runner_start_attempted))
        elif transition.exists():
            try:
                transition.unlink()
                run('systemctl', '--user', 'daemon-reload')
                recovery['transitionRemoved'] = True
            except BaseException as error:
                recovery['transitionRecoveryError'] = str(error)
        # Before the new binary can run, removing our Hub drop-in is safe. Beyond
        # this boundary the old binary requires restoring the encrypted snapshot.
        if hub_stopped and not migrated:
            try:
                hub_drop.unlink(missing_ok=True)
                run('systemctl', '--user', 'daemon-reload')
                run('systemctl', '--user', 'start', HUB)
                recovery['oldHubRestarted'] = True
            except BaseException as error:
                recovery['hubRecoveryError'] = str(error)
        try:
            recovery['hubState'] = unit_state(HUB)
            recovery['runnerState'] = unit_state(RUNNER)
            recovery['hubPeerMessages'] = health(args.url)
        except BaseException as error:
            recovery['stateProbeError'] = str(error)
        emit('failed', mayHaveMigrated=migrated, backup=str(backup), recovery=recovery,
             rollback='Restore encrypted complete pre-upgrade home before using old Hub binary' if migrated else 'Check reported old Hub recovery outcome')
        raise


if __name__ == '__main__':
    main()
