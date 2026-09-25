import { describe, expect, it, vi } from 'vitest'
import type { AxiosInstance } from 'axios'
import { PeerCapabilityCache, pingPeer } from './pingPeer'

function setup() {
    let time = 1_000_000
    let rejectCapability = false
    let rejectSend = false
    let onceExpired = false
    const session = { id: 'recipient-session', active: true, metadata: { name: 'Target' } }
    const post = vi.fn(async (url: string, body: unknown) => {
        if (url.endsWith('/api/auth')) return { status: 200, data: { token: 'synthetic-owner-token' } }
        if (url.endsWith('/peer-capability')) return rejectCapability
            ? { status: 404, data: {} }
            : { status: 200, data: { token: 'synthetic-capability', expiresAt: time + 900_000 } }
        if (url.endsWith('/peer/messages')) {
            if (onceExpired) { onceExpired = false; return { status: 401, data: {} } }
            if (rejectSend) return { status: 409, data: { error: 'idempotency conflict' } }
            return { status: 200, data: { status: 'persisted', messageId: 'hub-message-id' } }
        }
        throw new Error('Unexpected route')
    })
    const get = vi.fn(async (url: string) => url.endsWith('/api/sessions')
        ? { status: 200, data: { sessions: [session] } }
        : { status: 200, data: { session } })
    const http = { post, get } as unknown as AxiosInstance
    const cache = new PeerCapabilityCache()
    const send = (localId?: string) => pingPeer({
        sessionIdPrefix: 'recipient', senderSessionId: 'trusted-wrapper-session', message: 'Review please',
        apiUrl: 'http://127.0.0.1:9876', accessToken: 'synthetic-cli-token', http,
        peerCapabilities: cache, localId, replyTo: 'received-hub-id', now: () => time
    })
    return {
        send, post, advance: (ms: number) => { time += ms },
        rejectCapability: () => { rejectCapability = true }, rejectSend: () => { rejectSend = true },
        expireOnce: () => { onceExpired = true }
    }
}

describe('authenticated pingPeer', () => {
    it('mints only for wrapper identity, caches capabilities and keeps explicit request/reply ids', async () => {
        const { send, post, advance } = setup()
        const receipt = await send('same-key')
        expect(receipt).toMatchObject({ localId: 'same-key', messageId: 'hub-message-id' })
        await send('same-key')
        expect(post.mock.calls.filter(([url]) => url.endsWith('/peer-capability'))).toHaveLength(1)
        const capabilityCall = post.mock.calls.find(([url]) => url.endsWith('/peer-capability'))!
        expect(capabilityCall[0]).toContain('/sessions/trusted-wrapper-session/peer-capability')
        expect(capabilityCall[1]).toEqual({ recipientSessionId: 'recipient-session' })
        const sent = post.mock.calls.find(([url]) => url.endsWith('/peer/messages'))!
        expect(sent[1]).toEqual({ recipientSessionId: 'recipient-session', text: 'Review please', localId: 'same-key', replyTo: 'received-hub-id' })
        advance(875_000)
        await send('next-key')
        expect(post.mock.calls.filter(([url]) => url.endsWith('/peer-capability'))).toHaveLength(2)
    })

    it('does not downgrade to human-message transport when capabilities are unavailable', async () => {
        const { send, post, rejectCapability } = setup()
        rejectCapability()
        await expect(send('key')).rejects.toThrow(/scoped peer capability/)
        expect(post.mock.calls.some(([url]) => url.endsWith('/recipient-session/messages'))).toBe(false)
    })

    it('renews once on 401 and preserves generated id across the retry', async () => {
        const { send, post, expireOnce } = setup()
        expireOnce()
        const receipt = await send()
        const requests = post.mock.calls.filter(([url]) => url.endsWith('/peer/messages'))
        expect(requests).toHaveLength(2)
        expect(requests[0]![1]).toEqual(requests[1]![1])
        expect(receipt.localId).toMatch(/^[0-9a-f-]{36}$/)
        expect(post.mock.calls.filter(([url]) => url.endsWith('/peer-capability'))).toHaveLength(2)
    })

    it('surfaces the request id on conflicting requests for a reviewable retry', async () => {
        const { send, rejectSend } = setup()
        rejectSend()
        await expect(send('stable-error-key')).rejects.toThrow(/stable-error-key/)
    })
})
