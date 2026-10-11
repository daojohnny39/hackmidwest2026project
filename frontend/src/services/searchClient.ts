import type { InvestigationResult, SearchAccepted } from '../types/investigation.ts'
import { parseInvestigation, parseSearchAccepted } from './investigationContract.ts'

/** A request the service refused or couldn't answer. message is controlled UI copy, never server text. */
export class SearchRequestError extends Error {
  readonly status?: number
  constructor(message: string, status?: number) {
    super(message)
    this.status = status
  }
}

const NOT_REACHABLE = 'The search service is not reachable. Check that it is running.'

const MESSAGES: Record<number, string> = {
  400: 'The service could not read this photo. Retake it and try again.',
  403: 'Confirm both consent checkboxes, then try again.',
  413: 'Use a photo no larger than 5 MiB.',
  429: 'Other searches are waiting. Try again in a minute.',
  // The pipeline server never sends these; Vite's proxy does when `python -m pipeline serve` isn't running.
  502: NOT_REACHABLE,
  503: NOT_REACHABLE,
  504: NOT_REACHABLE,
}

async function request(path: string, init: RequestInit): Promise<unknown> {
  let response: Response
  try {
    // Relative /api: Vite proxies it to `python -m pipeline serve`, so the page's connect-src stays 'self'.
    response = await fetch(path, { ...init, cache: 'no-store', credentials: 'same-origin' })
  } catch (cause) {
    if (init.signal?.aborted) throw cause
    throw new SearchRequestError(NOT_REACHABLE)
  }
  if (!response.ok) throw new SearchRequestError(MESSAGES[response.status] ?? 'The search service is unavailable. Try again.', response.status)
  try {
    return await response.json()
  } catch (cause) {
    if (init.signal?.aborted) throw cause
    throw new SearchRequestError('The service returned an unreadable response.', response.status)
  }
}

export async function submitSearch(form: FormData, signal: AbortSignal): Promise<SearchAccepted> {
  // The browser sets the multipart boundary; no Content-Type header here.
  return parseSearchAccepted(await request('/api/search', { method: 'POST', body: form, signal }))
}

export async function fetchInvestigation(investigationId: string, signal: AbortSignal): Promise<InvestigationResult> {
  return parseInvestigation(await request(`/api/search/${encodeURIComponent(investigationId)}`, { signal }))
}
