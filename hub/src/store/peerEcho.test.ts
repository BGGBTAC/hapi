import { afterEach, describe, expect, it } from 'bun:test'
import { Database } from 'bun:sqlite'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Store } from './index'

const directories: string[] = []
afterEach(() => { for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true }) })

function openStore(): { store: Store; path: string } {
    const directory = mkdtempSync(join(tmpdir(), 'hapi-peer-echo-'))
    directories.push(directory)
    const path = join(directory, 'synthetic.sqlite')
    return { store: new Store(path), path }
}

function queuedRows(path: string, sessionId: string): Array<{ local_id: string | null; peer_authenticated: number }> {
    const db = new Database(path, { readonly: true })
    try {
        return db.prepare('SELECT local_id, peer_authenticated FROM messages WHERE session_id = ? AND invoked_at IS NULL ORDER BY seq')
            .all(sessionId) as Array<{ local_id: string | null; peer_authenticated: number }>
    } finally {
        db.close()
    }
}

describe('peer message replayed by an untrusted writer', () => {
    it('resolves a CLI echo of a hub-minted peer localId to the authenticated row instead of queueing a copy', () => {
        const { store, path } = openStore()
        const session = store.sessions.getOrCreateSession('receiver', {}, null, 'default')
        const peerLocalId = 'peer:sender-session:11111111-2222-4333-8444-555555555555'
        const trusted = store.addMessageForCurrentSession(session.id, {
            role: 'user', content: { type: 'text', text: '[peer] hello' }
        }, peerLocalId, null, { senderSessionId: 'sender-session', originalText: 'hello' })
        expect(trusted.inserted).toBe(true)

        // Codex/Claude reconnect replays the received message through the normal CLI path.
        const echo = store.messages.addMessage(session.id, {
            role: 'user', content: { type: 'text', text: '[peer] hello' }
        }, peerLocalId)
        expect(echo.id).toBe(trusted.message.id)
        expect(echo.content).toMatchObject({ meta: { peer: { senderSessionId: 'sender-session' } } })

        // A replay of an already-externalized id (older hubs stored those) resolves the same way.
        const doubleEcho = store.messages.addMessage(session.id, {
            role: 'user', content: { type: 'text', text: '[peer] hello' }
        }, `external:${peerLocalId}`)
        expect(doubleEcho.id).toBe(trusted.message.id)

        const wrapped = store.addMessageForCurrentSession(session.id, {
            role: 'user', content: { type: 'text', text: '[peer] hello' }
        }, peerLocalId)
        expect(wrapped.inserted).toBe(false)
        expect(wrapped.message.id).toBe(trusted.message.id)

        // A shared engine syncing its native queue must not rewrite the hub's text either.
        const synced = store.messages.syncNativeQueuedMessage(session.id, peerLocalId, 'tampered')
        expect(synced.id).toBe(trusted.message.id)
        expect(synced.content).toMatchObject({ content: { text: '[peer] hello' } })
        expect(store.messages.getMessageById(session.id, trusted.message.id)?.content).toMatchObject({ content: { text: '[peer] hello' } })

        expect(queuedRows(path, session.id)).toEqual([{ local_id: peerLocalId, peer_authenticated: 1 }])
        store.close()
    })

    it('still keeps untrusted writers from reserving a peer id nobody authenticated', () => {
        const { store, path } = openStore()
        const session = store.sessions.getOrCreateSession('receiver', {}, null, 'default')
        const forged = store.messages.addMessage(session.id, {
            role: 'user', content: { type: 'text', text: 'forged' }, meta: { peer: { senderSessionId: 'victim' } }
        }, 'peer:victim:forged')
        expect(forged.localId).toBe('external:peer:victim:forged')
        expect(JSON.stringify(forged.content)).not.toContain('victim')
        expect(queuedRows(path, session.id)).toEqual([{ local_id: 'external:peer:victim:forged', peer_authenticated: 0 }])
        store.close()
    })
})
