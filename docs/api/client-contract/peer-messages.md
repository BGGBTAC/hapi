# Authenticated peer messages

The health response advertises `capabilities.peerMessages: true`. This transport identifies a **HAPI session**, not a human author or a verified model. Session names and flavor labels are descriptive metadata. A namespace owner may delegate sending authority for any active session in that namespace. Isolated agents should receive scoped capabilities through a trusted wrapper; giving an agent the owner credential also gives it the ability to mint other session capabilities.

## Capability

An owner-authenticated `POST /api/sessions/:senderId/peer-capability` accepts `{ "recipientSessionId": "target-session-id", "ttlSeconds": 900 }`. The recipient is required; the lifetime is optional and defaults to 900 seconds (30–900 seconds allowed). Both sessions must be active and in the owner namespace. It returns `{ token, expiresAt, senderSessionId }`, where `expiresAt` is Unix milliseconds. The response uses `Cache-Control: no-store`.

The HS256 JWT has `aud: "hapi-peer"`, `scope: "peer:send"`, `sub: <senderSessionId>`, `ns`, the exact `recipientSessionId`, a random `jti`, `iat`, and `exp`. It has no `uid`. Peer capabilities cannot access owner `/api` routes, including the Gemini/Qwen WebSocket upgrade paths; owner tokens cannot access the peer transport. The MCP wrapper caches capabilities per sender/recipient only in memory, refreshes before expiry, and retries an expired capability once with the original message key.

## Sending and replying

`POST /peer/messages`, authenticated with the capability, accepts:

```json
{
  "recipientSessionId": "recipient-session-id",
  "text": "Please review this change.",
  "localId": "stable-request-key",
  "replyTo": "optional-hub-message-id"
}
```

The body recipient must match the capability destination; trying another destination returns HTTP 403. Sender and recipient must both be active in the capability namespace. The sender comes solely from the JWT. Extra fields, including claimed sender metadata, are rejected. Text is limited to 100,000 characters; identifiers and request keys to 200 characters.

`replyTo`, when present, must identify a stored peer message in the sender's own session whose original sender is the requested recipient. This binds both ends of a reply without trusting a supplied actor name. The hub also records `replyToSessionId` (the replying session) so clients can resolve the referenced message in the correct session. New CLI message frames and `inspect_peer` output include Hub message IDs.

The response is `{ status: "persisted", recipientSessionId, senderSessionId, messageId, localId }`. This confirms durable storage. It does **not** prove model processing, delivery exactly once, task acceptance, or task completion. Authenticated peer rows remain queued across session-end cleanup and replay when the recipient reconnects; only an actual consumption acknowledgement settles them. Even a consumption acknowledgement is not proof of task completion.

Deduplication is scoped to `(recipient session, sender session, localId)`. The hub prefixes stored local IDs with the sender identity. Ordinary socket/import/copy/rewind writers have a reserved `peer:` prefix remapped to `external:peer:` and cannot reserve future authenticated keys. Existing `external:` repetitions before `peer:` receive one further prefix, making this mapping injective. Repeating an identical request returns the original message ID; different text or `replyTo` returns HTTP 409. Clients must reuse the key after an uncertain network result. MCP `ping_peer` accepts optional `localId` and `replyTo`; otherwise it creates and returns a UUID. It does not silently fall back to human-message transport on older hubs. The standalone legacy CLI remains an owner-authenticated human-message action.

Authenticated sends are limited to 60 requests per minute per namespace/sender in each Hub process, including retries and newly minted tokens. HTTP 429 includes `Retry-After`. The limit is deliberately in memory and resets when the Hub restarts. `jti` identifies capabilities; it is not a durable revocation list.

## Stored provenance and command handling

The hub writes reserved `meta.peer` fields: `senderSessionId`, `originalText`, optional `senderName`, `senderFlavor`, `replyTo`, and `replyToSessionId`. Ordinary socket messages, imports, copies, and rewind replacements cannot set these fields. Schema 26 adds the hub-only `messages.peer_authenticated` flag, defaulting to zero for every old row. This is necessary because pre-upgrade JSON may already contain forged metadata. Reads strip peer metadata from unmarked rows at recognized message envelopes, while preserving legitimate nested tool/application data; only the internal authenticated insertion path sets the flag. Authentic provenance survives restarts. Imported or copied history is not re-certified as a live authenticated delivery.

The hub already persists peer content inside an explicit agent-data envelope. This protects older receiving CLIs during a gradual rollout: `/clear`, `/goal`, `/model`, and similar text never appear as top-level user commands. The updated CLI rebuilds the envelope from trusted `originalText` before provider-specific command handling and adds the hub message ID for replies. The envelope explicitly grants no human approval. Deduplication compares original text rather than the envelope or mutable display labels. The web chat displays the original text with an agent badge linking to the sender session.

Schema 26 is forward-only for the current binary. Before deployment, back up the hub data directory using the normal documented backup procedure. A rollback to a binary that only supports schema 25 requires restoring its pre-upgrade backup. This patch does not modify a running installation during development.
