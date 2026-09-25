import { Hono } from 'hono'
import { SignJWT, jwtVerify } from 'jose'
import { z } from 'zod'
import { randomUUID } from 'node:crypto'
import { SendPeerMessageRequestSchema } from '@hapi/protocol/schemas'
import type { SyncEngine } from '../../sync/syncEngine'
import { PeerMessageAccessError, PeerMessageConflictError } from '../../store/peerMetadata'
import type { WebAppEnv } from '../middleware/auth'

const capabilitySchema = z.object({
    aud: z.literal('hapi-peer'), scope: z.literal('peer:send'),
    sub: z.string().min(1), ns: z.string().min(1),
    recipientSessionId: z.string().min(1), jti: z.string().uuid(),
    iat: z.number().int(), exp: z.number().int()
}).strict()
const capabilityRequestSchema = z.object({
    recipientSessionId: z.string().min(1).max(200),
    ttlSeconds: z.number().int().min(30).max(900).default(900)
}).strict()

/** Mounted behind ordinary owner authentication; capabilities cannot mint capabilities. */
export function createPeerCapabilityRoutes(jwtSecret: Uint8Array, getSyncEngine: () => SyncEngine | null): Hono<WebAppEnv> {
    const app = new Hono<WebAppEnv>()
    app.post('/sessions/:id/peer-capability', async c => {
        const engine = getSyncEngine()
        if (!engine) return c.json({ error: 'Not connected' }, 503)
        const parsed = capabilityRequestSchema.safeParse(await c.req.json().catch(() => null))
        if (!parsed.success) return c.json({ error: 'Invalid body' }, 400)
        const session = engine.getSessionByNamespace(c.req.param('id'), c.get('namespace'))
        if (!session) return c.json({ error: 'Session not found' }, 404)
        if (!session.active) return c.json({ error: 'Sender session is inactive' }, 409)
        const recipient = engine.getSessionByNamespace(parsed.data.recipientSessionId, c.get('namespace'))
        if (!recipient) return c.json({ error: 'Recipient not found' }, 404)
        if (!recipient.active) return c.json({ error: 'Recipient session is inactive' }, 409)
        const issuedAt = Math.floor(Date.now() / 1000)
        const expiresAt = issuedAt + parsed.data.ttlSeconds
        const token = await new SignJWT({ ns: session.namespace, scope: 'peer:send', recipientSessionId: recipient.id })
            .setProtectedHeader({ alg: 'HS256' }).setAudience('hapi-peer')
            .setSubject(session.id).setJti(randomUUID()).setIssuedAt(issuedAt).setExpirationTime(expiresAt).sign(jwtSecret)
        c.header('Cache-Control', 'no-store')
        return c.json({ token, expiresAt: expiresAt * 1000, senderSessionId: session.id })
    })
    return app
}

/** Dedicated audience boundary, deliberately mounted outside /api owner middleware. */
export function createPeerMessagesRoutes(jwtSecret: Uint8Array, getSyncEngine: () => SyncEngine | null): Hono {
    const app = new Hono()
    const windows = new Map<string, { start: number; count: number }>()
    app.post('/messages', async c => {
        const authorization = c.req.header('authorization')
        if (!authorization?.startsWith('Bearer ')) return c.json({ error: 'Missing peer capability' }, 401)
        let principal: z.infer<typeof capabilitySchema>
        try {
            const verified = await jwtVerify(authorization.slice(7), jwtSecret, {
                algorithms: ['HS256'], audience: 'hapi-peer', maxTokenAge: '15m'
            })
            principal = capabilitySchema.parse(verified.payload)
            if (principal.exp <= principal.iat || principal.exp - principal.iat > 900) throw new Error('Invalid lifetime')
        } catch {
            return c.json({ error: 'Invalid peer capability' }, 401)
        }
        const parsed = SendPeerMessageRequestSchema.safeParse(await c.req.json().catch(() => null))
        if (!parsed.success) return c.json({ error: 'Invalid body' }, 400)
        if (parsed.data.recipientSessionId !== principal.recipientSessionId) {
            return c.json({ error: 'Peer capability is bound to a different recipient' }, 403)
        }
        const engine = getSyncEngine()
        if (!engine) return c.json({ error: 'Not connected' }, 503)
        const now = Date.now()
        for (const [key, window] of windows) if (now - window.start >= 60_000) windows.delete(key)
        const senderKey = JSON.stringify([principal.ns, principal.sub])
        const window = windows.get(senderKey) ?? { start: now, count: 0 }
        if (window.count >= 60) {
            c.header('Retry-After', String(Math.max(1, Math.ceil((window.start + 60_000 - now) / 1000))))
            return c.json({ error: 'Peer sender rate limit exceeded', code: 'peer_rate_limited' }, 429)
        }
        window.count++
        windows.set(senderKey, window)
        try {
            return c.json(await engine.sendPeerMessage(principal.sub, principal.ns, parsed.data))
        } catch (error) {
            if (error instanceof PeerMessageAccessError) return c.json({ error: error.message }, error.status)
            if (error instanceof PeerMessageConflictError) return c.json({ error: error.message, code: 'idempotency_conflict' }, 409)
            throw error
        }
    })
    return app
}
