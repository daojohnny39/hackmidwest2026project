import { useCallback, useEffect, useRef, useState } from 'react'
import { createSearchFormData } from '../services/investigationContract'
import { SearchRequestError, fetchInvestigation, submitSearch } from '../services/searchClient'
import type { InvestigationResult, SubjectInput } from '../types/investigation'

const POLL_MS = 2000
// Stage 1 took one to two minutes in past runs, but its per-image deadline with Trace is about 6.5 minutes.
const DEADLINE_MS = 8 * 60_000
const MAX_POLL_FAILURES = 3

export type LiveSearch =
  | { phase: 'idle' }
  | { phase: 'submitting' }
  | { phase: 'waiting'; result: InvestigationResult }
  | { phase: 'done'; result: InvestigationResult }
  /** investigationId is set when the search may still be running, so its status can be checked again. */
  | { phase: 'error'; message: string; investigationId?: string }

function wait(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    const timer = setTimeout(resolve, ms)
    signal.addEventListener('abort', () => { clearTimeout(timer); reject(signal.reason) }, { once: true })
  })
}

/** One live investigation: submit, then poll one request at a time until it completes or fails.
 *  A new search, clear(), or unmount aborts the previous one, and late responses are dropped. */
export function useInvestigation() {
  const [search, setSearch] = useState<LiveSearch>({ phase: 'idle' })
  const controller = useRef<AbortController | null>(null)

  const cancel = useCallback(() => { controller.current?.abort(); controller.current = null }, [])
  useEffect(() => cancel, [cancel])

  const run = useCallback(async (begin: (signal: AbortSignal) => Promise<string>) => {
    cancel()
    const current = new AbortController()
    controller.current = current
    const { signal } = current
    const update = (next: LiveSearch) => { if (!signal.aborted) setSearch(next) }
    let investigationId: string | undefined
    try {
      investigationId = await begin(signal)
      const deadline = Date.now() + DEADLINE_MS
      let failures = 0
      while (Date.now() < deadline) {
        await wait(POLL_MS, signal)
        let result: InvestigationResult
        try {
          result = await fetchInvestigation(investigationId, signal)
        } catch (cause) {
          if (signal.aborted || ++failures < MAX_POLL_FAILURES) continue
          throw cause
        }
        failures = 0
        if (result.status === 'completed' || result.status === 'failed') return update({ phase: 'done', result })
        update({ phase: 'waiting', result })
      }
      update({ phase: 'error', investigationId, message: 'The search is taking longer than expected. Its status is unknown.' })
    } catch (cause) {
      if (signal.aborted) return
      const message = cause instanceof SearchRequestError ? cause.message : 'The service returned an unexpected response.'
      update({ phase: 'error', investigationId, message: investigationId ? `${message} The search status is unknown.` : message })
    }
  }, [cancel])

  const start = useCallback((input: SubjectInput, image: Blob) => run(async signal => {
    setSearch({ phase: 'submitting' })
    const accepted = await submitSearch(createSearchFormData(input, image), signal)
    if (!signal.aborted) setSearch({ phase: 'waiting', result: accepted })
    return accepted.investigationId
  }), [run])

  /** Resume polling a search whose status became unknown, without submitting it again. */
  const checkAgain = useCallback((investigationId: string) => run(async () => {
    setSearch({ phase: 'waiting', result: { investigationId, status: 'running' } })
    return investigationId
  }), [run])

  const clear = useCallback(() => { cancel(); setSearch({ phase: 'idle' }) }, [cancel])

  return { search, start, checkAgain, clear }
}
