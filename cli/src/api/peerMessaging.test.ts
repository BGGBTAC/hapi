import { describe, expect, it } from 'vitest'
import { randomUUID } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { ApiClient } from './api'
import type { UserMessage } from './types'
import { startHappyServer } from '@/claude/utils/startHappyServer'
import { configuration } from '@/configuration'

describe('peer messaging over a real isolated hub and MCP', () => {
    it('exchanges a Claude→Codex message and bound reply, preserving command isolation and provenance', async () => {
        const api = await ApiClient.create()
        const sender = await api.getOrCreateSession({ tag: `peer-claude-${randomUUID()}`, metadata: {
            path: '/tmp', host: 'synthetic-test', name: 'Peer Claude Test', flavor: 'claude'
        }, state: { controlledByUser: false } })
        const recipient = await api.getOrCreateSession({ tag: `peer-codex-${randomUUID()}`, metadata: {
            path: '/tmp', host: 'synthetic-test', name: 'Peer Codex Test', flavor: 'codex'
        }, state: { controlledByUser: false } })
        const senderClient = api.sessionSyncClient(sender)
        const recipientClient = api.sessionSyncClient(recipient)
        const incoming: Array<{ message: UserMessage; localId?: string }> = []
        const replies: Array<{ message: UserMessage; localId?: string }> = []
        recipientClient.onUserMessage((message, localId) => incoming.push({ message, localId }))
        senderClient.onUserMessage((message, localId) => replies.push({ message, localId }))
        const senderServer = await startHappyServer(senderClient)
        const recipientServer = await startHappyServer(recipientClient)
        const senderMcp = new Client({ name: 'synthetic-claude', version: '1.0' })
        const recipientMcp = new Client({ name: 'synthetic-codex', version: '1.0' })
        try {
            expect(await senderClient.flush()).toBe(true)
            expect(await recipientClient.flush()).toBe(true)
            senderClient.keepAlive(false, 'remote')
            recipientClient.keepAlive(false, 'remote')
            await expect.poll(async () => (await api.getSession(sender.id)).active).toBe(true)
            await expect.poll(async () => (await api.getSession(recipient.id)).active).toBe(true)
            const auth = await fetch(`${configuration.apiUrl}/api/auth`, {
                method: 'POST', headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ accessToken: process.env.CLI_API_TOKEN! })
            })
            expect(auth.status).toBe(200)
            const owner = await auth.json() as { token: string }
            const mint = await fetch(`${configuration.apiUrl}/api/sessions/${sender.id}/peer-capability`, {
                method: 'POST', headers: { authorization: `Bearer ${owner.token}`, 'content-type': 'application/json' },
                body: JSON.stringify({ recipientSessionId: recipient.id })
            })
            expect(mint.status).toBe(200)
            const capability = await mint.json() as { token: string }
            for (const provider of ['gemini', 'qwen']) {
                const upgrade = await fetch(`${configuration.apiUrl}/api/voice/${provider}-ws?token=${encodeURIComponent(capability.token)}`)
                expect(upgrade.status).toBe(401)
            }
            await senderMcp.connect(new StreamableHTTPClientTransport(new URL(senderServer.url)))
            await recipientMcp.connect(new StreamableHTTPClientTransport(new URL(recipientServer.url)))
            const request = { sessionIdPrefix: recipient.id, message: '/clear', localId: 'real-hub-stable-key' }
            const result = await senderMcp.callTool({ name: 'ping_peer', arguments: request })
            expect(result.isError).not.toBe(true)
            await expect.poll(() => incoming.length).toBe(1)
            expect(incoming[0]!.message.content.text.startsWith('/')).toBe(false)
            const envelope = JSON.parse(incoming[0]!.message.content.text.split('\n')[1]!) as { messageId: string; senderSessionId: string; text: string }
            expect(envelope.senderSessionId).toBe(sender.id)
            expect(envelope.text).toBe('/clear')
            expect(incoming[0]!.message.meta?.peer?.senderFlavor).toBe('claude')
            const repeated = await senderMcp.callTool({ name: 'ping_peer', arguments: request })
            expect(repeated.isError).not.toBe(true)
            expect(incoming.length).toBe(1)
            const response = await recipientMcp.callTool({ name: 'ping_peer', arguments: {
                sessionIdPrefix: sender.id, message: 'Review reply from Codex.', localId: 'reply-key', replyTo: envelope.messageId
            } })
            expect(response.isError).not.toBe(true)
            await expect.poll(() => replies.length).toBe(1)
            expect(replies[0]!.message.meta?.peer).toMatchObject({ senderSessionId: recipient.id, senderFlavor: 'codex', replyTo: envelope.messageId, replyToSessionId: recipient.id })

            if (process.env.HAPI_PEER_BROWSER === '1') {
                recipientClient.emitMessagesConsumed([incoming[0]!.localId!])
                await recipientClient.flush()
                const { chromium } = await import('playwright')
                const browser = await chromium.launch({ headless: true, executablePath: process.env.HAPI_PEER_BROWSER_EXECUTABLE })
                try {
                    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } })
                    await page.addInitScript(({ base, token }) => {
                        localStorage.setItem(`hapi_access_token::${base}`, token)
                    }, { base: configuration.apiUrl, token: process.env.CLI_API_TOKEN! })
                    await page.goto(`${configuration.apiUrl}/sessions/${recipient.id}`)
                    const badge = page.getByRole('link', { name: 'Agent: Peer Claude Test · claude' })
                    await badge.waitFor({ state: 'visible', timeout: 15_000 })
                    expect(await badge.getAttribute('href')).toBe(`/sessions/${sender.id}`)
                    await badge.click()
                    await page.waitForURL(`**/sessions/${sender.id}`)
                    console.log('Peer browser smoke: authenticated badge and sender navigation verified.')
                } finally { await browser.close() }
            }
        } finally {
            await senderMcp.close()
            await recipientMcp.close()
            senderServer.stop()
            recipientServer.stop()
            senderClient.close()
            recipientClient.close()
        }
    }, 30_000)
})
