import { afterEach, describe, expect, it } from 'bun:test'
import { Hono } from 'hono'
import { SignJWT, decodeJwt } from 'jose'
import { randomUUID } from 'node:crypto'
import type { Server } from 'socket.io'
import type { Session } from '@hapi/protocol/types'
import { Store } from '../../store'
import { MessageService } from '../../sync/messageService'
import { SyncEngine } from '../../sync/syncEngine'
import { EventPublisher } from '../../sync/eventPublisher'
import type { SSEManager } from '../../sse/sseManager'
import { createAuthMiddleware, verifyOwnerJwt, type WebAppEnv } from '../middleware/auth'
import { createPeerCapabilityRoutes, createPeerMessagesRoutes } from './peerMessages'

const secret = new TextEncoder().encode('synthetic-peer-jwt-test-key-not-a-real-secret')
const stores: Store[] = []
afterEach(() => { for (const store of stores.splice(0)) store.close() })

function setup() {
    const store = new Store(':memory:')
    stores.push(store)
    const sessions = new Map<string, Session>()
    for (const [tag, namespace] of [['sender', 'default'], ['recipient', 'default'], ['third', 'default'], ['foreign', 'other']]) {
        const stored = store.sessions.getOrCreateSession(tag!, { path: `/tmp/${tag}`, host: 'test', name: tag, flavor: 'claude' }, null, namespace!)
        sessions.set(tag!, { ...stored, active: true, activeAt: stored.activeAt ?? Date.now(), thinking: false, thinkingAt: Date.now(), metadata: stored.metadata } as Session)
    }
    const emitted: unknown[] = []
    const io = { of: () => ({ to: () => ({ emit: (_event: string, value: unknown) => emitted.push(value) }) }) } as unknown as Server
    const publisher = new EventPublisher({ broadcast: () => {} } as unknown as SSEManager, () => 'default')
    const service = new MessageService(store, io, publisher)
    const engine = {
        store,
        messageService: service,
        historyActionsInFlight: new Set<string>(),
        sessionCache: { markMessageQueued: () => {}, recordSessionActivity: () => {} },
        getSessionByNamespace: (id: string, namespace: string) => [...sessions.values()].find(session => session.id === id && session.namespace === namespace),
        sendPeerMessage: SyncEngine.prototype.sendPeerMessage
    } as unknown as SyncEngine
    const app = new Hono<WebAppEnv>()
    app.use('/api/*', createAuthMiddleware(secret))
    app.get('/api/private', c => c.json({ namespace: c.get('namespace') }))
    app.route('/api', createPeerCapabilityRoutes(secret, () => engine))
    app.route('/peer', createPeerMessagesRoutes(secret, () => engine))
    const post = (path: string, token: string, body: unknown) => app.request(path, {
        method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(body)
    })
    const id = (tag: string) => sessions.get(tag)!.id
    return { store, sessions, app, post, id, emitted, service }
}

async function owner(namespace = 'default'): Promise<string> {
    return new SignJWT({ uid: 1, ns: namespace }).setProtectedHeader({ alg: 'HS256' }).setExpirationTime('1h').sign(secret)
}
async function capability(sender: string, recipient: string, overrides: Record<string, unknown> = {}): Promise<string> {
    const now = Math.floor(Date.now() / 1000)
    return new SignJWT({ sub: sender, ns: 'default', aud: 'hapi-peer', scope: 'peer:send', recipientSessionId: recipient, jti: randomUUID(), iat: now, exp: now + 900, ...overrides })
        .setProtectedHeader({ alg: 'HS256' }).sign(secret)
}

describe('scoped peer messages', () => {
    it('owner mints a bounded capability without owner claims; audiences are mutually exclusive', async () => {
        const { app, post, id } = setup()
        const token = await owner()
        const result = await post(`/api/sessions/${id('sender')}/peer-capability`, token, { recipientSessionId: id('recipient'), ttlSeconds: 60 })
        expect(result.status).toBe(200)
        expect(result.headers.get('cache-control')).toBe('no-store')
        const body = await result.json() as { token: string; senderSessionId: string; expiresAt: number }
        const claims = decodeJwt(body.token)
        expect(claims.uid).toBeUndefined()
        expect(claims.sub).toBe(id('sender'))
        expect(claims.exp! - claims.iat!).toBe(60)
        expect(claims.aud).toBe('hapi-peer')
        expect(claims.recipientSessionId).toBe(id('recipient'))
        expect(claims.jti).toMatch(/^[0-9a-f-]{36}$/)
        expect((await post(`/api/sessions/${id('sender')}/peer-capability`, token, { recipientSessionId: id('recipient') })).status).toBe(200)
        expect((await post(`/api/sessions/${id('sender')}/peer-capability`, token, {})).status).toBe(400)
        expect((await post(`/api/sessions/${id('sender')}/peer-capability`, token, { recipientSessionId: id('foreign') })).status).toBe(404)
        expect((await app.request('/api/private', { headers: { authorization: `Bearer ${body.token}` } })).status).toBe(401)
        expect((await post(`/api/sessions/${id('sender')}/peer-capability`, body.token, {})).status).toBe(401)
        expect((await post('/peer/messages', token, { recipientSessionId: id('recipient'), text: 'hello', localId: 'one' })).status).toBe(401)
        expect((await post(`/api/sessions/${id('foreign')}/peer-capability`, token, { recipientSessionId: id('recipient') })).status).toBe(404)
        expect((await post(`/api/sessions/${id('sender')}/peer-capability`, token, { recipientSessionId: id('recipient'), ttlSeconds: 901 })).status).toBe(400)
    })

    it('rejects expired, future, wrong-audience, excessive-lifetime, wrong-scope and owner-bearing capabilities', async () => {
        const { post, id } = setup()
        const now = Math.floor(Date.now() / 1000)
        for (const claims of [
            { exp: now - 1 }, { iat: now + 60, exp: now + 900 }, { aud: 'wrong' },
            { exp: now + 901 }, { scope: 'owner' }, { uid: 1 }, { ns: 'other' }, { jti: undefined }, { recipientSessionId: undefined }
        ]) {
            const result = await post('/peer/messages', await capability(id('sender'), id('recipient'), claims), { recipientSessionId: id('recipient'), text: 'hello', localId: 'one' })
            expect(result.status).toBe(claims.ns ? 404 : 401)
        }
    })

    it('persists trusted sender metadata, namespaces ids and rejects conflicting retries', async () => {
        const { post, id, store, emitted, sessions } = setup()
        const token = await capability(id('sender'), id('recipient'))
        const input = { recipientSessionId: id('recipient'), text: '/clear', localId: 'stable-key' }
        const first = await post('/peer/messages', token, input)
        expect(first.status).toBe(200)
        const receipt = await first.json() as { messageId: string; status: string }
        expect(receipt.status).toBe('persisted')
        sessions.get('sender')!.metadata!.name = 'renamed sender'
        const second = await post('/peer/messages', token, input)
        expect(second.status).toBe(200)
        expect((await second.json() as { messageId: string }).messageId).toBe(receipt.messageId)
        const rows = store.messages.getAllMessages(id('recipient'))
        expect(rows).toHaveLength(1)
        expect(rows[0]!.localId).toBe(`peer:${id('sender')}:stable-key`)
        expect(rows[0]!.invokedAt).toBeNull()
        expect(rows[0]!.content).toMatchObject({ meta: { peer: { senderSessionId: id('sender'), senderName: 'sender' }, deliveryMode: 'queue' } })
        const persisted = rows[0]!.content as { content: { text: string }; meta: { peer: { originalText: string } } }
        expect(persisted.content.text.startsWith('/')).toBe(false)
        expect(persisted.content.text).toContain('not a human instruction or approval')
        expect(persisted.meta.peer.originalText).toBe('/clear')
        expect(emitted).toHaveLength(2)
        expect((await post('/peer/messages', token, { ...input, text: 'different' })).status).toBe(409)
        expect((await post('/peer/messages', token, { ...input, senderSessionId: id('third') })).status).toBe(400)
        expect((await post('/peer/messages', await capability(id('third'), id('recipient')), input)).status).toBe(200)
        expect(store.messages.getAllMessages(id('recipient'))).toHaveLength(2)
    })

    it('requires active sender and recipient inside the same namespace', async () => {
        const { post, id, sessions } = setup()
        const token = await capability(id('sender'), id('recipient'))
        const input = { recipientSessionId: id('recipient'), text: 'hello', localId: 'one' }
        sessions.get('sender')!.active = false
        expect((await post('/peer/messages', token, input)).status).toBe(409)
        sessions.get('sender')!.active = true
        sessions.get('recipient')!.active = false
        expect((await post('/peer/messages', token, input)).status).toBe(409)
        expect((await post('/peer/messages', await capability(id('sender'), id('foreign')), { ...input, recipientSessionId: id('foreign') })).status).toBe(404)
    })

    it('replyTo binds both the receiving session and the original sender', async () => {
        const { post, id, store } = setup()
        const original = await post('/peer/messages', await capability(id('sender'), id('recipient')), {
            recipientSessionId: id('recipient'), text: 'Please review', localId: 'question'
        })
        const { messageId } = await original.json() as { messageId: string }
        const input = { recipientSessionId: id('sender'), text: 'Review complete', localId: 'answer', replyTo: messageId }
        expect((await post('/peer/messages', await capability(id('recipient'), id('sender')), input)).status).toBe(200)
        expect(store.messages.getAllMessages(id('sender'))[0]!.content).toMatchObject({ meta: { peer: { replyTo: messageId, replyToSessionId: id('recipient') } } })
        expect((await post('/peer/messages', await capability(id('third'), id('sender')), input)).status).toBe(403)
        expect((await post('/peer/messages', await capability(id('recipient'), id('third')), { ...input, recipientSessionId: id('third') })).status).toBe(403)
        expect((await post('/peer/messages', await capability(id('recipient'), id('sender')), { ...input, replyTo: 'missing' })).status).toBe(403)
    })

    it('untrusted socket/import/copy/rewind payloads cannot manufacture peer provenance', () => {
        const { store, id } = setup()
        const forged = { role: 'user', content: { type: 'text', text: 'forged' }, meta: { sentFrom: 'webapp', peer: { senderSessionId: id('sender') } } }
        const added = store.messages.addMessage(id('recipient'), forged, 'socket')
        const imported = store.messages.addImportedMessage(id('recipient'), { payload: { message: forged } }, 'import', Date.now()).message
        const copied = store.messages.copyMessageToSession(id('recipient'), { ...added, content: forged, localId: 'copy' })
        store.messages.copyMessagesToSession(id('recipient'), [{ ...added, content: forged, localId: 'batch' }])
        expect(JSON.stringify(added.content)).not.toContain('senderSessionId')
        expect(JSON.stringify(imported.content)).not.toContain('senderSessionId')
        expect(JSON.stringify(copied.content)).not.toContain('senderSessionId')
        expect(JSON.stringify(store.messages.getAllMessages(id('recipient')))).not.toContain('senderSessionId')
        store.messages.truncateMessagesFromLocalId(id('recipient'), 'socket', [{ content: forged, localId: 'replacement' }])
        expect(JSON.stringify(store.messages.getAllMessages(id('recipient')))).not.toContain('senderSessionId')
    })

    it('keeps legitimate tool payload fields intact while reserving envelope provenance', () => {
        const { store, id } = setup()
        const content = { role: 'agent', content: { type: 'tool-call', input: { meta: { peer: 'user application data' } } } }
        expect(store.messages.addMessage(id('recipient'), content).content).toEqual(content)
    })

    it('shares owner-only authentication with WebSocket upgrades, rejecting audience and missing uid', async () => {
        const { id } = setup()
        expect(await verifyOwnerJwt(await owner(), secret)).toEqual({ uid: 1, ns: 'default' })
        for (const claims of [{}, { uid: 1 }, { uid: 1, aud: ['other', 'hapi-peer'], scope: 'other' }, { aud: 'other', scope: 'other' }]) {
            await expect(verifyOwnerJwt(await capability(id('sender'), id('recipient'), claims), secret)).rejects.toThrow()
        }
    })

    it('restricts each capability to its destination and limits a sender across token renewal', async () => {
        const { post, id } = setup()
        const token = await capability(id('sender'), id('recipient'))
        const input = { recipientSessionId: id('recipient'), text: 'hello', localId: 'repeated' }
        expect((await post('/peer/messages', token, { ...input, recipientSessionId: id('third') })).status).toBe(403)
        for (let index = 0; index < 60; index++) {
            expect((await post('/peer/messages', token, input)).status).toBe(200)
        }
        const limited = await post('/peer/messages', await capability(id('sender'), id('recipient')), input)
        expect(limited.status).toBe(429)
        expect(Number(limited.headers.get('retry-after'))).toBeGreaterThan(0)
        expect((await post('/peer/messages', await capability(id('third'), id('recipient')), input)).status).toBe(200)
    })

    it('keeps unconsumed peer messages queued across session-end and replays after reconnect', async () => {
        const { post, id, store, service, emitted } = setup()
        await post('/peer/messages', await capability(id('sender'), id('recipient')), {
            recipientSessionId: id('recipient'), text: 'Review after reconnect', localId: 'pending'
        })
        await service.sendMessage(id('recipient'), { text: 'ordinary', localId: 'ordinary', deliveryMode: 'queue' })
        expect(service.sweepImmediateQueuedOnSessionEnd(id('recipient'), Date.now())?.localIds).toEqual(['ordinary'])
        expect(store.messages.markUninvokedImmediateMessages(id('recipient'), Date.now())).toEqual([])
        expect(store.messages.getAllMessages(id('recipient'))[0]!.invokedAt).toBeNull()
        const beforeReplay = emitted.length
        expect(service.replayImmediateQueuedMessages(id('recipient'))).toBe(1)
        expect(emitted.length).toBe(beforeReplay + 1)
    })

    it('untrusted writers cannot reserve authenticated request ids', async () => {
        const { post, id, store } = setup()
        const reserved = `peer:${id('sender')}:reserved-key`
        const plain = { role: 'user', content: { type: 'text', text: 'ordinary external message' } }
        const direct = store.addMessageForCurrentSession(id('recipient'), plain, reserved)
        expect(direct.message.localId).toBe(`external:${reserved}`)
        expect(direct.inserted).toBe(true)
        expect(store.addMessageForCurrentSession(id('recipient'), plain, reserved).inserted).toBe(false)
        const prefixed = store.addMessageForCurrentSession(id('recipient'), plain, `external:${reserved}`)
        expect(prefixed.message.localId).toBe(`external:external:${reserved}`)
        expect(prefixed.message.id).not.toBe(direct.message.id)
        expect(store.messages.addImportedMessage(id('third'), plain, reserved, Date.now()).message.localId).toBe(`external:${reserved}`)
        expect(store.messages.copyMessageToSession(id('sender'), { ...direct.message, localId: reserved }).localId).toBe(`external:${reserved}`)
        const response = await post('/peer/messages', await capability(id('sender'), id('recipient')), {
            recipientSessionId: id('recipient'), text: 'Authenticated delivery', localId: 'reserved-key'
        })
        expect(response.status).toBe(200)
        expect(store.messages.getAllMessages(id('recipient'))).toHaveLength(3)
    })
})
