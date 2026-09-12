/** Creates only two clearly named transport-check sessions on an already upgraded local Hub. */
import assert from 'node:assert/strict'
import { createHash, randomUUID } from 'node:crypto'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import { ApiClient } from '../../cli/src/api/api'
import type { UserMessage } from '../../cli/src/api/types'
import { configuration } from '../../cli/src/configuration'
import { initializeToken } from '../../cli/src/ui/tokenInit'
import { startHappyServer } from '../../cli/src/claude/utils/startHappyServer'

let phase = 'arguments'

async function until(predicate: () => boolean | Promise<boolean>, label: string) {
    const deadline = Date.now() + 15_000
    while (!await predicate()) {
        if (Date.now() > deadline) throw new Error(`Timed out: ${label}`)
        await new Promise(resolve => setTimeout(resolve, 100))
    }
}

async function main() {
    if (!process.argv.includes('--execute')) throw new Error('Pass --execute to create two new local-Hub transport-check sessions')
    const holdIndex = process.argv.indexOf('--hold-seconds')
    const holdSeconds = holdIndex === -1 ? 0 : Number(process.argv[holdIndex + 1])
    assert(Number.isInteger(holdSeconds) && holdSeconds >= 0 && holdSeconds <= 300, 'hold-seconds must be 0..300')
    phase = 'initialize-cli-auth'
    await initializeToken() // Normal CLI credential loading, never printed or copied to artifacts.
    phase = 'local-url-guard'
    const url = new URL(configuration.apiUrl)
    assert(['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname), 'Live check is restricted to the explicitly selected local Hub')
    phase = 'hub-health'
    const health = await fetch(`${configuration.apiUrl}/health`).then(response => response.json()) as { capabilities?: { peerMessages?: boolean } }
    assert.equal(health.capabilities?.peerMessages, true)
    phase = 'create-api-client'
    const api = await ApiClient.create()
    const sessions = []
    for (const flavor of ['claude', 'codex'] as const) {
        phase = `create-test-session-${flavor}`
        sessions.push(await api.getOrCreateSession({
            tag: `hapiimprovr-peer-check-${flavor}-${randomUUID()}`,
            metadata: { path: '/home/benedict/work/hapiimprovr', host: 'dev-main', name: `hapiimprovr-peer-check-${flavor} (transport test)`, flavor },
            state: { controlledByUser: false }
        }))
    }
    const [sender, recipient] = sessions
    assert(sender && recipient)
    phase = 'create-session-clients'
    const senderClient = api.sessionSyncClient(sender)
    const recipientClient = api.sessionSyncClient(recipient)
    const received: Array<{ message: UserMessage; localId?: string }> = []
    const replies: Array<{ message: UserMessage; localId?: string }> = []
    let holding = false
    recipientClient.onUserMessage((message, localId) => {
        received.push({ message, localId })
        if (holding) {
            const text = message.meta?.peer?.originalText ?? ''
            console.log(JSON.stringify({ kind: 'companion-message-received', localId,
                senderSessionId: message.meta?.peer?.senderSessionId,
                authenticatedSenderMatches: message.meta?.peer?.senderSessionId === sender.id,
                originalTextSha256: createHash('sha256').update(text).digest('hex'), originalTextBytes: Buffer.byteLength(text) }))
        }
    })
    senderClient.onUserMessage((message, localId) => replies.push({ message, localId }))
    phase = 'start-mcp-servers'
    const senderServer = await startHappyServer(senderClient)
    const recipientServer = await startHappyServer(recipientClient)
    const senderMcp = new Client({ name: 'hapiimprovr-transport-check-claude', version: '1' })
    const recipientMcp = new Client({ name: 'hapiimprovr-transport-check-codex', version: '1' })
    let browserVerified = false
    try {
        phase = 'connect-transport'
        assert(await senderClient.flush())
        assert(await recipientClient.flush())
        senderClient.keepAlive(false, 'remote')
        recipientClient.keepAlive(false, 'remote')
        await until(async () => (await api.getSession(sender.id)).active, 'sender active')
        await until(async () => (await api.getSession(recipient.id)).active, 'recipient active')
        await senderMcp.connect(new StreamableHTTPClientTransport(new URL(senderServer.url)))
        await recipientMcp.connect(new StreamableHTTPClientTransport(new URL(recipientServer.url)))
        phase = 'peer-delivery'
        const request = { sessionIdPrefix: recipient.id, message: '/clear', localId: `live-check-${randomUUID()}` }
        assert.notEqual((await senderMcp.callTool({ name: 'ping_peer', arguments: request })).isError, true)
        await until(() => received.length === 1, 'first message')
        assert(!received[0]!.message.content.text.startsWith('/'))
        const envelope = JSON.parse(received[0]!.message.content.text.split('\n')[1]!) as { messageId: string; senderSessionId: string; text: string }
        assert.equal(envelope.senderSessionId, sender.id)
        assert.equal(envelope.text, '/clear')
        assert.notEqual((await senderMcp.callTool({ name: 'ping_peer', arguments: request })).isError, true)
        await new Promise(resolve => setTimeout(resolve, 200))
        assert.equal(received.length, 1)
        assert.notEqual((await recipientMcp.callTool({ name: 'ping_peer', arguments: {
            sessionIdPrefix: sender.id, message: 'Transport-check reply received. No model was invoked.',
            localId: `live-reply-${randomUUID()}`, replyTo: envelope.messageId
        } })).isError, true)
        await until(() => replies.length === 1, 'bound reply')
        assert.equal(replies[0]!.message.meta?.peer?.replyToSessionId, recipient.id)
        recipientClient.emitMessagesConsumed([received[0]!.localId!])
        senderClient.emitMessagesConsumed([replies[0]!.localId!])
        await recipientClient.flush()
        await senderClient.flush()
        if (process.env.HAPI_PEER_BROWSER === '1') {
            phase = 'browser'
            const { chromium } = await import('playwright')
            const browser = await chromium.launch({ headless: true, executablePath: process.env.HAPI_PEER_BROWSER_EXECUTABLE })
            try {
                const page = await browser.newPage()
                await page.addInitScript(({ base, token }) => localStorage.setItem(`hapi_access_token::${base}`, token), {
                    base: configuration.apiUrl, token: configuration.cliApiToken
                })
                await page.goto(`${configuration.apiUrl}/sessions/${recipient.id}`)
                const badge = page.getByRole('link', { name: 'Agent: hapiimprovr-peer-check-claude (transport test) · claude' })
                await badge.waitFor({ state: 'visible', timeout: 15_000 })
                assert.equal(await badge.getAttribute('href'), `/sessions/${sender.id}`)
                await badge.click()
                await page.waitForURL(`**/sessions/${sender.id}`)
                browserVerified = true
            } finally { await browser.close() }
        }
        console.log(JSON.stringify({ kind: 'real-hub-and-mcp-transport-check', modelsInvoked: false,
            senderSessionId: sender.id, recipientSessionId: recipient.id, messageId: envelope.messageId,
            authenticatedSender: true, slashCommandIsolated: true, deduplicated: true, boundReply: true, browserVerified }))
        if (holdSeconds) {
            holding = true
            console.log(JSON.stringify({ phase: 'holding-for-companion-check', holdSeconds,
                senderSessionId: sender.id, recipientSessionId: recipient.id }))
            const keepAlive = setInterval(() => {
                senderClient.keepAlive(false, 'remote')
                recipientClient.keepAlive(false, 'remote')
            }, 2_000)
            try { await new Promise(resolve => setTimeout(resolve, holdSeconds * 1_000)) }
            finally {
                clearInterval(keepAlive)
                console.log(JSON.stringify({ kind: 'companion-hold-complete', additionalReceivedMessages: received.length - 1 }))
            }
        }
    } finally {
        await senderMcp.close()
        await recipientMcp.close()
        senderServer.stop()
        recipientServer.stop()
        senderClient.close()
        recipientClient.close()
    }
}

main().catch((error: unknown) => {
    // Axios errors can carry credentials; do not serialize arbitrary error objects.
    const status = (error as { response?: { status?: unknown } } | null)?.response?.status
    console.error(JSON.stringify({ kind: 'live-peer-check-failed', phase,
        ...(typeof status === 'number' && Number.isInteger(status) ? { httpStatus: status } : {}) }))
    process.exitCode = 1
})
