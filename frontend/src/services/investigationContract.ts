import type { CandidateImage, ConfidenceLevel, EvidenceLevel, InvestigationResult, SearchAccepted, SearchStage, SubjectInput } from '../types/investigation.ts'

// Frontend limits for this draft; Member 1 must confirm server limits before connection.
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024
export const FIELD_LIMITS = { firstName: 120, lastName: 120, city: 120 } as const
export type FormErrors = Partial<Record<keyof SubjectInput | 'image', string>>
export function validateSubject(input: SubjectInput, image?: Blob): FormErrors {
  const errors: FormErrors = {}
  for (const key of ['firstName', 'lastName', 'city'] as const) {
    if (input[key].trim().length > FIELD_LIMITS[key]) errors[key] = `Use ${FIELD_LIMITS[key]} characters or fewer.`
  }
  const age = input.age.trim()
  if (age && (!/^\d+$/.test(age) || !Number.isSafeInteger(Number(age)))) errors.age = 'Enter an age as a whole number of 0 or more.'
  if (!input.authorized) errors.authorized = 'Confirm that you have authorization.'
  if (!input.externalProcessing) errors.externalProcessing = 'Agree to send the photo to the search services.'
  if (!image || image.size === 0) errors.image = 'Take and select a reference photo first.'
  else if (!['image/jpeg', 'image/png'].includes(image.type)) errors.image = 'Use a JPEG or PNG photo.'
  else if (image.size > MAX_IMAGE_BYTES) errors.image = 'Use a photo no larger than 5 MiB.'
  return errors
}
/** Builds the multipart payload; searchClient.ts sends it. */
export function createSearchFormData(input: SubjectInput, image: Blob): FormData {
  if (Object.keys(validateSubject(input, image)).length) throw new Error('Check the subject details, consent, and photo.')
  const form = new FormData()
  form.append('image', image, image.type === 'image/png' ? 'reference.png' : 'reference.jpg')
  for (const key of ['firstName', 'lastName', 'city', 'age'] as const) {
    if (input[key].trim()) form.append(key, input[key].trim())
  }
  form.append('authorized', 'true')
  form.append('externalProcessing', 'true')
  return form
}
export function safePublicUrl(value?: string): string | undefined {
  if (!value) return undefined
  try {
    const url = new URL(value)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return undefined
    return url.href
  } catch { return undefined }
}
function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}
function text(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}
function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.flatMap(item => text(item) ? [text(item)!] : []) : []
}
function level(value: unknown): EvidenceLevel {
  return value === 'high' || value === 'medium' || value === 'low' ? value : 'unknown'
}
const CONFIDENCE_LEVELS: readonly unknown[] = ['strong', 'likely', 'possible', 'weak', 'unlikely']
const SEARCH_STAGES: readonly unknown[] = ['preparing', 'searching', 'comparing', 'reviewing', 'checking', 'finalizing']
function confidenceLevel(value: unknown): ConfidenceLevel {
  return CONFIDENCE_LEVELS.includes(value) ? value as ConfidenceLevel : 'unknown'
}
function score(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 100 ? Math.round(value) : undefined
}
export function parseSearchAccepted(value: unknown): SearchAccepted {
  const data = record(value)
  const investigationId = text(data?.investigationId)
  const status = data?.status
  if (!investigationId || typeof status !== 'string' || !['queued', 'running', 'completed', 'failed'].includes(status)) {
    throw new Error('The service returned an unsupported investigation response.')
  }
  return { investigationId, status: status as SearchAccepted['status'] }
}
/** Unknown states are errors, not successful results. Optional metadata stays optional. */
export function parseInvestigation(value: unknown): InvestigationResult {
  const accepted = parseSearchAccepted(value)
  const data = record(value)!
  if (data.candidates !== undefined && !Array.isArray(data.candidates)) {
    throw new Error('The service returned an unreadable candidate list.')
  }
  if (accepted.status === 'completed' && !Array.isArray(data.candidates)) {
    throw new Error('The completed response is missing its candidate list.')
  }
  let skipped = false
  const candidates: CandidateImage[] = []
  if (Array.isArray(data.candidates)) data.candidates.forEach((item, index) => {
    const candidate = record(item)
    if (!candidate) { skipped = true; return }
    const relevance = record(candidate.relevance)
    const confidence = record(candidate.confidence)
    const threat = record(candidate.threat)
    candidates.push({
      id: text(candidate.id) ?? `candidate-${index + 1}`,
      imageUrl: safePublicUrl(text(candidate.imageUrl)), sourceUrl: safePublicUrl(text(candidate.sourceUrl)),
      pageTitle: text(candidate.pageTitle), sourceType: text(candidate.sourceType),
      relevance: relevance ? { classification: level(relevance.classification), reasons: strings(relevance.reasons), reviewRequired: relevance.reviewRequired !== false } : undefined,
      confidence: confidence ? { score: score(confidence.score), level: confidenceLevel(confidence.level), reasons: strings(confidence.reasons) } : undefined,
      threat: threat ? { score: score(threat.score), level: level(threat.level), indicators: strings(threat.indicators) } : undefined,
    })
  })
  // Never display arbitrary backend errors, traces, or warning strings in the UI.
  const warnings = strings(data.warnings).length || skipped
    ? ['Some findings are unavailable or incomplete. Review the available sources.'] : []
  const stage = accepted.status === 'running' && SEARCH_STAGES.includes(data.stage) ? data.stage as SearchStage : undefined
  return { ...accepted, stage, candidates, warnings, error: accepted.status === 'failed' ? 'The search could not finish. Try again.' : undefined }
}
