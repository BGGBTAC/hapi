import { isObject } from '@hapi/protocol'

function stripRecord(value: unknown): unknown {
    if (!isObject(value) || !isObject(value.meta) || !Object.hasOwn(value.meta, 'peer')) return value
    const { peer: _reserved, ...meta } = value.meta
    return { ...value, meta }
}

/** Covers every envelope understood by unwrapRoleWrappedRecordEnvelope, without changing tool data. */
export function stripPeerMetadata(value: unknown): unknown {
    const record = stripRecord(value)
    if (!isObject(record)) return record
    let result = record
    const message = stripRecord(record.message)
    if (message !== record.message) result = { ...result, message }
    for (const key of ['data', 'payload'] as const) {
        const wrapper = record[key]
        if (!isObject(wrapper)) continue
        const nested = stripRecord(wrapper.message)
        if (nested !== wrapper.message) result = { ...result, [key]: { ...wrapper, message: nested } }
    }
    return result
}

export class PeerMessageConflictError extends Error {
    constructor() {
        super('Peer localId has already been used with different content')
        this.name = 'PeerMessageConflictError'
    }
}

export class PeerMessageAccessError extends Error {
    constructor(message: string, readonly status: 403 | 404 | 409) {
        super(message)
        this.name = 'PeerMessageAccessError'
    }
}

/** Normal/import writers cannot reserve identifiers owned by authenticated peers. */
export function externalLocalId(localId: string): string {
    return /^(external:)*peer:/.test(localId) ? `external:${localId}` : localId
}
