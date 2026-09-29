import { SESSION_LIFECYCLE_IDLE } from '@hapi/protocol'

type LivenessInput = {
    active: boolean
    metadata?: { lifecycleState?: string } | null
}

/**
 * Connected, but the hub has seen nothing except keepalives for the idle
 * window (tiann/hapi#1820). `active` stays true on purpose — the socket is
 * up — so anything that wants to read "the agent is alive" must ask here
 * instead of `session.active`.
 */
export function isKeepaliveIdle(session: LivenessInput): boolean {
    return session.active && session.metadata?.lifecycleState === SESSION_LIFECYCLE_IDLE
}

/** Connected and not idle-marked: the only sessions that should read as live. */
export function isLiveSession(session: LivenessInput): boolean {
    return session.active && !isKeepaliveIdle(session)
}

/** Sort key: live first, then keepalive-idle, then disconnected. */
export function sessionLivenessRank(session: LivenessInput): number {
    if (isLiveSession(session)) return 0
    return session.active ? 1 : 2
}
