import { describe, expect, it } from 'vitest'
import { decideUntrackedRunnerWebhook } from './lateRunnerWebhook'

describe('decideUntrackedRunnerWebhook', () => {
    it('never kills a shared Codex root (siblings live in the same wrapper)', () => {
        expect(decideUntrackedRunnerWebhook({ concurrentClients: true, timedOutByThisRunner: false })).toBe('adopt')
        expect(decideUntrackedRunnerWebhook({ concurrentClients: true, timedOutByThisRunner: true })).toBe('adopt')
    })

    it('kills a nonshared CLI whose spawn this runner generation timed out', () => {
        expect(decideUntrackedRunnerWebhook({ concurrentClients: false, timedOutByThisRunner: true })).toBe('kill')
    })

    it('adopts a nonshared CLI that reports after a runner restart', () => {
        // No timeout stamp in this generation: the child belongs to the
        // previous runner. Ignoring it would leave an unlisted, unstoppable
        // process (no HAPI id on argv, no durable PID record).
        expect(decideUntrackedRunnerWebhook({ concurrentClients: false, timedOutByThisRunner: false })).toBe('adopt')
    })
})
