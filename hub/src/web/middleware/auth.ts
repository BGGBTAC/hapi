import type { MiddlewareHandler } from 'hono'
import { z } from 'zod'
import { jwtVerify } from 'jose'

export type WebAppEnv = {
    Variables: {
        userId: number
        namespace: string
    }
}

const jwtPayloadSchema = z.object({
    uid: z.number(),
    ns: z.string()
})

/** Used by HTTP middleware and the WebSocket upgrade path before provider credentials are accessed. */
export async function verifyOwnerJwt(token: string, jwtSecret: Uint8Array) {
    const verified = await jwtVerify(token, jwtSecret, { algorithms: ['HS256'] })
    const audiences = Array.isArray(verified.payload.aud) ? verified.payload.aud : [verified.payload.aud]
    if (audiences.includes('hapi-peer') || verified.payload.scope === 'peer:send') {
        throw new Error('Peer capabilities cannot access owner APIs')
    }
    return jwtPayloadSchema.parse(verified.payload)
}

export function createAuthMiddleware(jwtSecret: Uint8Array): MiddlewareHandler<WebAppEnv> {
    return async (c, next) => {
        const path = c.req.path
        if (path === '/api/auth' || path === '/api/bind') {
            await next()
            return
        }

        const authorization = c.req.header('authorization')
        const tokenFromHeader = authorization?.startsWith('Bearer ') ? authorization.slice('Bearer '.length) : undefined
        const tokenFromQuery = path === '/api/events' ? c.req.query().token : undefined
        const token = tokenFromHeader ?? tokenFromQuery

        if (!token) {
            return c.json({ error: 'Missing authorization token' }, 401)
        }

        try {
            const principal = await verifyOwnerJwt(token, jwtSecret)
            c.set('userId', principal.uid)
            c.set('namespace', principal.ns)
            await next()
            return
        } catch {
            return c.json({ error: 'Invalid token' }, 401)
        }
    }
}
