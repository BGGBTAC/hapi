import { describe, expect, it } from 'vitest'
import { mkdir, mkdtemp, readFile, unlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
import { spawn, type ChildProcess } from 'node:child_process'
import { configuration } from '@/configuration'
import { getProcessStartMarker } from '@/utils/process'
import { projectPath } from '@/projectPath'
import { ApiClient } from '@/api/api'
import type { ApiSessionClient } from '@/api/apiSession'

const runtime = process.env.HAPI_BUN_EXEC ?? 'bun'
async function until<T>(probe: () => Promise<T | undefined>, label: string): Promise<T> {
    const deadline = Date.now() + 15_000
    while (Date.now() < deadline) {
        const value = await probe()
        if (value !== undefined) return value
        await new Promise(resolve => setTimeout(resolve, 100))
    }
    throw new Error(`Timed out: ${label}`)
}
async function stop(child: ChildProcess) {
    if (child.exitCode !== null || child.signalCode !== null) return
    child.kill('SIGTERM')
    await until(async () => child.exitCode !== null || child.signalCode !== null ? true : undefined, 'synthetic child shutdown')
}

describe.skipIf(process.platform !== 'linux')('runner recovered peer-era sessions', () => {
    it('keeps verified resumed children visible and controllable and does not kill them on a new webhook', async () => {
        const originalId = randomUUID()
        const replacementId = randomUUID()
        const home = await mkdtemp(join(tmpdir(), 'hapi-peer-recovery-'))
        const probes = join(home, 'bin')
        await mkdir(probes)
        const denyMarker = join(home, 'deny-marker')
        await writeFile(join(probes, 'ps'), `#!/bin/sh\nif [ -f '${denyMarker}' ]; then\n  if [ -s '${denyMarker}' ]; then echo changed-generation; exit 0; fi\n  exit 1\nfi\nexec /usr/bin/ps "$@"\n`, { mode: 0o755 })
        const child = spawn(runtime, ['--eval', 'setInterval(() => {}, 1000)'], { stdio: 'ignore', detached: true })
        const unrelated = spawn(runtime, ['--eval', 'setInterval(() => {}, 1000)'], { stdio: 'ignore', detached: true })
        expect(child.pid).toBeDefined()
        expect(unrelated.pid).toBeDefined()
        const marker = getProcessStartMarker(child.pid!)
        expect(marker).not.toBeNull()
        await writeFile(join(home, 'runner.state.json.resume-processes.json'), JSON.stringify([
            { requestedSessionId: originalId, confirmedSessionId: originalId, pid: child.pid, processStartMarker: marker },
            { requestedSessionId: 'synthetic-stale', confirmedSessionId: 'synthetic-stale', pid: unrelated.pid, processStartMarker: 'wrong-generation' }
        ]))
        const runner = spawn(runtime, ['src/index.ts', 'runner', 'start-sync', '--workspace-root', home], {
            cwd: projectPath(),
            env: { ...process.env, PATH: `${probes}:${process.env.PATH}`, HAPI_HOME: home, HAPI_API_URL: configuration.apiUrl, HAPI_DISABLE_VERSION_HANDOFF: '1' },
            stdio: 'ignore'
        })
        let currentSession: ApiSessionClient | undefined
        try {
            const state = await until(async () => {
                try {
                    const record = JSON.parse(await readFile(join(home, 'runner.state.json'), 'utf8')) as { pid?: number; httpPort?: number; startedWithMachineId?: string }
                    return record.pid === runner.pid && record.httpPort && record.startedWithMachineId
                        ? record as { pid: number; httpPort: number; startedWithMachineId: string } : undefined
                } catch { return undefined }
            }, 'isolated runner ready')
            const post = async (path: string, body: unknown = {}) => {
                const response = await fetch(`http://127.0.0.1:${state.httpPort}${path}`, {
                    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body)
                })
                expect(response.status).toBe(200)
                return response.json()
            }
            expect(await post('/list')).toMatchObject({ children: [{ happySessionId: originalId, pid: child.pid }] })
            for (const markerFailure of ['', 'mismatched-generation']) {
                await writeFile(denyMarker, markerFailure)
                await post('/session-started', { sessionId: 'unverified-new-id', metadata: { hostPid: child.pid, startedBy: 'runner' } })
                await new Promise(resolve => setTimeout(resolve, 100))
                expect(child.exitCode).toBeNull()
                expect(child.signalCode).toBeNull()
                expect(await post('/list')).toMatchObject({ children: [{ happySessionId: originalId, pid: child.pid }] })
            }
            await unlink(denyMarker)
            await post('/session-started', { sessionId: replacementId, metadata: { hostPid: child.pid, startedBy: 'runner' } })
            expect(child.exitCode).toBeNull()
            expect(await post('/list')).toMatchObject({ children: [{ happySessionId: replacementId, pid: child.pid }] })
            // The real Hub resume path must use the replacement result, not the
            // stale success cached when the runner first recovered the child.
            const api = await ApiClient.create()
            for (const id of [originalId, replacementId]) {
                const session = await api.getOrCreateSession({ id, tag: `${home}-${id}`,
                    metadata: { path: home, host: 'synthetic', machineId: state.startedWithMachineId, flavor: 'claude' }, state: null })
                if (id === replacementId) currentSession = api.sessionSyncClient(session)
            }
            expect(await currentSession!.flush()).toBe(true)
            currentSession!.keepAlive(false, 'remote')
            await until(async () => (await api.getSession(replacementId)).active ? true : undefined, 'replacement session active')
            const auth = await fetch(`${configuration.apiUrl}/api/auth`, {
                method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ accessToken: process.env.CLI_API_TOKEN })
            }).then(response => response.json()) as { token: string }
            const resumed = await fetch(`${configuration.apiUrl}/api/sessions/${originalId}/resume`, {
                method: 'POST', headers: { authorization: `Bearer ${auth.token}`, 'content-type': 'application/json' }, body: '{}'
            })
            expect(resumed.status).toBe(200)
            expect(await resumed.json()).toMatchObject({ type: 'success', sessionId: replacementId })
            expect(await post('/stop-session', { sessionId: replacementId })).toMatchObject({ status: 'stopped' })
            await until(async () => child.exitCode !== null || child.signalCode !== null ? true : undefined, 'recovered child stopped')
            expect(unrelated.exitCode).toBeNull()
            expect(unrelated.signalCode).toBeNull()
        } finally {
            currentSession?.close()
            await stop(runner)
            await stop(child)
            await stop(unrelated)
        }
    }, 30_000)
})
