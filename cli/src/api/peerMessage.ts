import type { UserMessage } from './types'
import { formatPeerMessageText } from '@hapi/protocol'

/** Applied before any provider-specific slash-command/queue handling. */
export function framePeerMessage(message: UserMessage, messageId?: string): UserMessage {
    const peer = message.meta?.peer
    if (!peer) return message
    return {
        ...message,
        content: {
            ...message.content,
            text: formatPeerMessageText(peer, peer.originalText ?? message.content.text, messageId)
        }
    }
}
