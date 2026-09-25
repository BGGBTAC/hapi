#!/usr/bin/env python3
"""Run a release binary against a fresh loopback-only, synthetic HAPI home."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import time
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', required=True, type=Path)
    parser.add_argument('--port', default=3316, type=int)
    parser.add_argument('--directory', type=Path, help='Must not exist; never reuse a production data directory.')
    parser.add_argument('--duration', type=int, default=0, help='Stop after N seconds; zero waits for Ctrl-C.')
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    if not 1024 <= args.port <= 65535 or args.duration < 0:
        parser.error('port must be 1024..65535 and duration must be nonnegative')
    if args.directory:
        directory = args.directory.resolve()
        directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    else:
        directory = Path(tempfile.mkdtemp(prefix='hapi-peer-preview-'))
    base_url = f'http://127.0.0.1:{args.port}'
    # Deliberately public test data: never copy production credentials/settings here.
    synthetic_token = 'hapi-peer-preview-synthetic-only'
    environment = {key: os.environ[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TMPDIR') if key in os.environ}
    environment.update({
        'HAPI_HOME': str(directory / 'data'), 'DB_PATH': str(directory / 'data' / 'hapi.db'),
        'HAPI_LISTEN_HOST': '127.0.0.1', 'HAPI_LISTEN_PORT': str(args.port),
        'HAPI_PUBLIC_URL': base_url, 'CLI_API_TOKEN': synthetic_token,
        'TELEGRAM_NOTIFICATION': 'false', 'SERVERCHAN_NOTIFICATION': 'false',
    })
    wrapper = directory / 'hapi-peer-preview'
    wrapper_env = {key: environment[key] for key in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'TMPDIR') if key in environment}
    wrapper_env.update({'HAPI_HOME': str(directory / 'data'), 'HAPI_API_URL': base_url, 'CLI_API_TOKEN': synthetic_token})
    wrapper.write_text('#!/bin/sh\nexec env -i ' + ' '.join(shlex.quote(f'{key}={value}') for key, value in wrapper_env.items())
                       + ' ' + shlex.quote(str(binary)) + ' "$@"\n')
    wrapper.chmod(0o700)
    stopping = False

    def stop(_signal: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    with (directory / 'hub.log').open('w') as log:
        process = subprocess.Popen([str(binary), 'hub'], env=environment, cwd=directory,
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'Preview exited with status {process.returncode}; see {directory / "hub.log"}')
                try:
                    with urllib.request.urlopen(f'{base_url}/health', timeout=1) as response:
                        health = json.load(response)
                    if health.get('capabilities', {}).get('peerMessages') is not True:
                        raise RuntimeError('The selected binary does not support authenticated peers')
                    break
                except (OSError, ValueError):
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f'Preview did not become ready; see {directory / "hub.log"}')
                    time.sleep(0.1)
            print(json.dumps({'url': base_url, 'capabilities': health['capabilities'], 'wrapper': str(wrapper),
                              'directory': str(directory), 'credentials': 'Synthetic preview token is documented in this script.'}), flush=True)
            end = time.monotonic() + args.duration if args.duration else None
            while not stopping and (end is None or time.monotonic() < end):
                if process.poll() is not None:
                    raise RuntimeError(f'Preview exited with status {process.returncode}')
                time.sleep(0.2)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
