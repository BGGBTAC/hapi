import { describe, expect, it } from 'vitest'
import { mkdtemp, readFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'
import { spawn, type ChildProcess } from 'node:child_process'
import { configuration } from '@/configuration'
import { projectPath } from '@/projectPath'
import { isProcessAlive, killProcessTreeByPid } from '@/utils/process'

/**
 * A fresh spawn (no reserved HAPI id: the hub only passes one for resume)
 * used to leave no durable PID record, and an untracked `startedBy: 'runner'`
 * webhook was killed on sight. After a runner restart that left the child
 * invisible to /list and unstoppable — or dead, if its webhook was still in
 * flight. These tests drive the real runner over its control server.
 */

const runtime = process.env.HAPI_BUN_EXEC ?? 'bun'
type RunnerState = { pid: number; httpPort: number }
type ResumeRecord = { requestedSessionId: string; confirmedSessionId?: string; pid: number; processStartMarker: string }

async function until<T>(probe: () => Promise<T | undefined>, label: string, timeoutMs = 15_000): Promise<T> {
    const deadline = Date.now() + timeoutMs
    while (Date.now() < deadline) {
        const value = await probe()
        if (value !== undefined) return value
        await new Promise(resolve => setTimeout(resolve, 100))
    }
    throw new Error(`Timed out: ${label}`)
}
async function stop(child: ChildProcess | undefined) {
    if (!child || child.exitCode !== null || child.signalCode !== null) return
    child.kill('SIGTERM')
    await until(async () => child.exitCode !== null || child.signalCode !== null ? true : undefined, 'child shutdown')
}
function startRunner(home: string): ChildProcess {
    return spawn(runtime, ['src/index.ts', 'runner', 'start-sync', '--workspace-root', home], {
        cwd: projectPath(),
        env: { ...process.env, HAPI_HOME: home, HAPI_API_URL: configuration.apiUrl, HAPI_DISABLE_VERSION_HANDOFF: '1' },
        stdio: 'ignore'
    })
}
async function runnerReady(home: string, runner: ChildProcess): Promise<RunnerState> {
    return until(async () => {
        try {
            const record = JSON.parse(await readFile(join(home, 'runner.state.json'), 'utf8')) as Partial<RunnerState>
            return record.pid === runner.pid && record.httpPort ? record as RunnerState : undefined
        } catch { return undefined }
    }, 'isolated runner ready')
}
function poster(state: RunnerState) {
    return async (path: string, body: unknown = {}) => {
        const response = await fetch(`http://127.0.0.1:${state.httpPort}${path}`, {
            method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body)
        })
        expect(response.status).toBe(200)
        return response.json() as Promise<Record<string, unknown>>
    }
}
async function readResumeRecords(home: string): Promise<ResumeRecord[]> {
    return JSON.parse(await readFile(join(home, 'runner.state.json.resume-processes.json'), 'utf8')) as ResumeRecord[]
}

describe.skipIf(process.platform !== 'linux')('runner fresh-spawn recovery', () => {
    it('adopts an untracked runner-spawned webhook after a restart instead of killing the child, and keeps it listed and stoppable', async () => {
        const sessionId = randomUUID()
        const home = await mkdtemp(join(tmpdir(), 'hapi-fresh-spawn-adopt-'))
        // Stands in for a Claude child spawned by the previous runner
        // generation: nothing on disk names it (no resume record at all).
        const child = spawn(runtime, ['--eval', 'setInterval(() => {}, 1000)'], { stdio: 'ignore', detached: true })
        expect(child.pid).toBeDefined()
        let runner: ChildProcess | undefined = startRunner(home)
        let restarted: ChildProcess | undefined
        try {
            const post = poster(await runnerReady(home, runner))
            await post('/session-started', { sessionId, metadata: { hostPid: child.pid, startedBy: 'runner' } })
            await new Promise(resolve => setTimeout(resolve, 300))
            expect(child.exitCode).toBeNull()
            expect(child.signalCode).toBeNull()
            expect(await post('/list')).toMatchObject({ children: [{ happySessionId: sessionId, pid: child.pid }] })
            expect(await readResumeRecords(home)).toMatchObject([{ pid: child.pid, requestedSessionId: sessionId, confirmedSessionId: sessionId }])

            // Another restart: the durable record is all the new generation has.
            await stop(runner)
            runner = undefined
            restarted = startRunner(home)
            const postAgain = poster(await runnerReady(home, restarted))
            expect(await postAgain('/list')).toMatchObject({ children: [{ happySessionId: sessionId, pid: child.pid }] })
            expect(await postAgain('/stop-session', { sessionId })).toMatchObject({ status: 'stopped' })
            await until(async () => child.exitCode !== null || child.signalCode !== null ? true : undefined, 'adopted child stopped')
        } finally {
            await stop(runner)
            await stop(restarted)
            await stop(child)
        }
    }, 30_000)

    it('persists a fresh spawn once its webhook names the row, so a restarted runner still lists and stops it', async () => {
        const home = await mkdtemp(join(tmpdir(), 'hapi-fresh-spawn-persist-'))
        let runner: ChildProcess | undefined = startRunner(home)
        let restarted: ChildProcess | undefined
        let childPid: number | undefined
        try {
            const post = poster(await runnerReady(home, runner))
            // No sessionId: exactly what the hub sends for a new session.
            const spawned = await post('/spawn-session', { directory: home })
            expect(spawned).toMatchObject({ success: true })
            const sessionId = spawned.sessionId as string
            expect(typeof sessionId).toBe('string')

            const records = await readResumeRecords(home)
            expect(records).toMatchObject([{ confirmedSessionId: sessionId }])
            childPid = records[0].pid
            expect(isProcessAlive(childPid)).toBe(true)
            expect(await post('/list')).toMatchObject({ children: [{ happySessionId: sessionId, pid: childPid }] })

            await stop(runner)
            runner = undefined
            restarted = startRunner(home)
            const postAgain = poster(await runnerReady(home, restarted))
            expect(await postAgain('/list')).toMatchObject({ children: [{ happySessionId: sessionId, pid: childPid }] })
            expect(await postAgain('/stop-session', { sessionId })).toMatchObject({ status: 'stopped' })
            await until(async () => isProcessAlive(childPid!) ? undefined : true, 'fresh child stopped')
        } finally {
            await stop(runner)
            await stop(restarted)
            if (childPid && isProcessAlive(childPid)) await killProcessTreeByPid(childPid, true)
        }
    }, 60_000)
})
