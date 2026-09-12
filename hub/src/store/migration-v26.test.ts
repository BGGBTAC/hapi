import { afterEach, describe, expect, it } from 'bun:test'
import { Database } from 'bun:sqlite'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Store } from './index'

const directories: string[] = []
afterEach(() => { for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true }) })

describe('peer provenance migration v26', () => {
    it('does not trust pre-upgrade meta.peer and retains authenticated provenance over restarts', () => {
        const directory = mkdtempSync(join(tmpdir(), 'hapi-peer-migration-'))
        directories.push(directory)
        const path = join(directory, 'synthetic.sqlite')
        const initial = new Store(path)
        const session = initial.sessions.getOrCreateSession('receiver', {}, null, 'default')
        initial.close()
        const legacy = new Database(path)
        legacy.exec('ALTER TABLE messages DROP COLUMN peer_authenticated; PRAGMA user_version=25;')
        legacy.prepare('INSERT INTO messages(id,session_id,content,created_at,seq,local_id) VALUES(?,?,?,?,?,?)').run(
            'forged-old-message', session.id,
            JSON.stringify({ role: 'user', content: { type: 'text', text: 'forged' }, meta: { peer: { senderSessionId: 'victim' } } }),
            1, 1, 'legacy'
        )
        legacy.close()
        const migrated = new Store(path)
        expect(JSON.stringify(migrated.messages.getMessageById(session.id, 'forged-old-message'))).not.toContain('victim')
        const trusted = migrated.addMessageForCurrentSession(session.id, {
            role: 'user', content: { type: 'text', text: 'real peer message' }
        }, 'trusted', null, { senderSessionId: 'real-sender' })
        migrated.close()
        const restarted = new Store(path)
        expect(restarted.messages.getMessageById(session.id, trusted.message.id)?.content).toMatchObject({ meta: { peer: { senderSessionId: 'real-sender' } } })
        const raw = restarted.messages.addMessage(session.id, {
            role: 'user', content: { type: 'text', text: 'forged again' }, meta: { peer: { senderSessionId: 'victim' } }
        }, 'new-forged')
        expect(JSON.stringify(raw.content)).not.toContain('victim')
        restarted.close()
    })
})
