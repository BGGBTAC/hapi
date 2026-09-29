/**
 * Decision for a `startedBy: 'runner'` session webhook whose PID this runner
 * generation is not tracking (no TrackedSession, no persisted resume record).
 *
 * - Shared Codex root: never kill — the wrapper hosts sibling roots; the
 *   runtime registry is what attaches and stops those.
 * - Nonshared, and this generation timed its spawn out: a ghost whose tree
 *   was already killed at the source; finish the job.
 * - Nonshared, not timed out here (typical after a runner restart while the
 *   child was still bootstrapping): adopt. A fresh Claude spawn carries no
 *   HAPI id on argv, so nothing durable names it yet; ignoring the webhook
 *   leaves an unreapable, unlisted CLI behind (tiann/hapi#1910 / #1911).
 */

export type UntrackedRunnerWebhookDecision = 'kill' | 'adopt'

export function decideUntrackedRunnerWebhook(opts: {
    concurrentClients: boolean
    timedOutByThisRunner: boolean
}): UntrackedRunnerWebhookDecision {
    if (opts.concurrentClients) {
        return 'adopt'
    }
    if (opts.timedOutByThisRunner) {
        return 'kill'
    }
    return 'adopt'
}
