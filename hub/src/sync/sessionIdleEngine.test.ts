import { beforeAll, describe, expect, it, mock, setSystemTime } from 'bun:test'
import { createConfiguration } from '../configuration'
import { Store } from '../store'
import { registerCliHandlers } from '../socket/handlers/cli'
import { RpcRegistry } from '../socket/rpcRegistry'
import { createSocketServer } from '../socket/server'
import { TerminalRegistry } from '../socket/terminalRegistry'
import type { SessionCache } from './sessionCache'
import { SyncEngine } from './syncEngine'

/**
 * tiann/hapi#1820 end-to-end wiring. `sessionIdle.test.ts` pins the pure
 * rules and `registerSessionHandlers`; these tests pin the chain around it —
 * createSocketServer → registerCliHandlers → the progress hook, and the
 * SyncEngine tick that actually writes `idle` — so a refactor dropping any
 * one pass-through fails loudly instead of silently false-idling (or never
 * idling) every session.
 */

const HOUR = 60 * 60 * 1000

const RUNNING = { path: '/tmp/project', host: 'localhost', flavor: 'claude', lifecycleState: 'running' } as const

function createEngine() {
    const store = new Store(':memory:')
    const engine = new SyncEngine(store, {} as never, new RpcRegistry(), { broadcast() {} } as never)
    return { store, engine }
}

/** Just enough of a socket.io CLI socket for the handler registrations. */
class FakeCliSocket {
    readonly id = 'cli-socket-1'
    readonly data = { namespace: 'default' }
    readonly handshake = { auth: {} }
    private readonly handlers = new Map<string, (data: unknown, ack?: (response: unknown) => void) => void>()

    on(event: string, handler: (data: unknown, ack?: (response: unknown) => void) => void): this {
        this.handlers.set(event, handler)
        return this
    }
    join(): void {}
    emit(): void {}
    to(): { emit: () => void } {
        return { emit: () => {} }
    }
    trigger(event: string, data: unknown): void {
        this.handlers.get(event)?.(data)
    }
}

function agentMessage(): string {
    return JSON.stringify({ role: 'agent', content: { type: 'text', text: 'still working' } })
}

function withIdleWindow<T>(windowMs: number, run: () => T): T {
    const previous = process.env.HAPI_SESSION_IDLE_TIMEOUT_MS
    process.env.HAPI_SESSION_IDLE_TIMEOUT_MS = String(windowMs)
    try {
        return run()
    } finally {
        if (previous === undefined) {
            delete process.env.HAPI_SESSION_IDLE_TIMEOUT_MS
        } else {
            process.env.HAPI_SESSION_IDLE_TIMEOUT_MS = previous
        }
    }
}

beforeAll(async () => {
    await createConfiguration()
})

describe('keepalive-idle progress wiring', () => {
    it('registerCliHandlers forwards a stored message to onAgentProgress', () => {
        const store = new Store(':memory:')
        const session = store.sessions.getOrCreateSession('wiring-cli', RUNNING, null, 'default')
        const onAgentProgress = mock((_sessionId: string, _at: number) => {})
        const socket = new FakeCliSocket()

        registerCliHandlers(socket as never, {
            io: { of: () => ({ sockets: new Map() }) } as never,
            store,
            rpcRegistry: new RpcRegistry(),
            terminalRegistry: new TerminalRegistry({ idleTimeoutMs: 60_000 }),
            onAgentProgress
        })
        socket.trigger('message', { sid: session.id, message: agentMessage() })

        expect(onAgentProgress).toHaveBeenCalledTimes(1)
        expect(onAgentProgress.mock.calls[0][0]).toBe(session.id)
        expect(typeof onAgentProgress.mock.calls[0][1]).toBe('number')
    })

    it('createSocketServer hands onAgentProgress through to the /cli connection handler', () => {
        const store = new Store(':memory:')
        const session = store.sessions.getOrCreateSession('wiring-server', RUNNING, null, 'default')
        const onAgentProgress = mock((_sessionId: string, _at: number) => {})

        const { io } = createSocketServer({
            store,
            jwtSecret: new Uint8Array(32),
            corsOrigins: ['*'],
            onAgentProgress
        })
        // No network: drive the namespace's own connection listener with a
        // fake socket, exactly what socket.io would do after the auth
        // middleware.
        const connection = io.of('/cli').listeners('connection')
        expect(connection).toHaveLength(1)
        const socket = new FakeCliSocket()
        ;(connection[0] as (socket: unknown) => void)(socket)
        socket.trigger('message', { sid: session.id, message: agentMessage() })

        expect(onAgentProgress).toHaveBeenCalledTimes(1)
        expect(onAgentProgress.mock.calls[0][0]).toBe(session.id)
        io.close()
    })
})

describe('SyncEngine keepalive-idle tick', () => {
    it('the inactivity tick reconciles a keepalive-only session to idle', () => {
        withIdleWindow(HOUR, () => {
            const { engine } = createEngine()
            try {
                const session = engine.getOrCreateSession('tick', RUNNING, null, 'default')
                engine.handleSessionAlive({ sid: session.id, time: Date.now() })

                // Two hours of nothing but keepalives.
                setSystemTime(new Date(Date.now() + 2 * HOUR))
                engine.handleSessionAlive({ sid: session.id, time: Date.now() })
                ;(engine as unknown as { expireInactive(): void }).expireInactive()

                const reconciled = engine.getSession(session.id)!
                expect(reconciled.metadata?.lifecycleState).toBe('idle')
                // The socket is up: `active` is not the tick's to flip.
                expect(reconciled.active).toBe(true)
            } finally {
                setSystemTime()
                engine.stop()
            }
        })
    })

    it('abort forgets background tasks so the session can reconcile idle', async () => {
        const { engine } = createEngine()
        try {
            const session = engine.getOrCreateSession('abort', RUNNING, null, 'default')
            engine.handleSessionAlive({ sid: session.id, time: Date.now(), thinking: true })
            engine.handleBackgroundTaskDelta(session.id, { started: 1, completed: 0 })
            engine.handleSessionAlive({ sid: session.id, time: Date.now(), thinking: false })
            expect(engine.getSession(session.id)?.backgroundTaskCount).toBe(1)
            const cache = (engine as unknown as { sessionCache: SessionCache }).sessionCache
            const later = Date.now() + 87 * HOUR
            // A counter that can never be closed pins the session on "working".
            expect(cache.reconcileKeepaliveIdle(later, 12 * HOUR)).toEqual([])

            const gateway = (engine as unknown as { rpcGateway: { abortSession: unknown } }).rpcGateway
            // The CLI refuses (no handler): nothing was killed, keep the count.
            gateway.abortSession = mock(async () => { throw new Error('handler-not-registered') })
            await expect(engine.abortSession(session.id)).rejects.toThrow()
            expect(engine.getSession(session.id)?.backgroundTaskCount).toBe(1)

            // The CLI acknowledged: its process tree, background shells
            // included, is gone and no <task-notification> will follow.
            gateway.abortSession = mock(async () => {})
            await engine.abortSession(session.id)
            expect(engine.getSession(session.id)?.backgroundTaskCount).toBe(0)
            expect(cache.reconcileKeepaliveIdle(later, 12 * HOUR)).toEqual([session.id])
        } finally {
            engine.stop()
        }
    })
})
