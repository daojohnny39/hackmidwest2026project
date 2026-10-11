import type { CandidateImage, InvestigationResult } from '../types/investigation'
const first: CandidateImage = {
  id: 'sample-profile', imageUrl: '/demo/source-placeholder.svg', sourceUrl: 'https://example.com/profile',
  pageTitle: 'Sample professional profile', sourceType: 'Professional profile',
  relevance: { classification: 'high', reasons: ['The sample name and public work context overlap.'], reviewRequired: true },
}
const second: CandidateImage = {
  id: 'sample-article', imageUrl: '/demo/source-placeholder.svg', sourceUrl: 'https://example.org/article',
  pageTitle: 'Sample community article', sourceType: 'Article',
  relevance: { classification: 'medium', reasons: ['A similar display name appears; the context is incomplete.'], reviewRequired: true },
  threat: { level: 'unknown', indicators: [] },
}
const third: CandidateImage = {
  id: 'sample-unknown', imageUrl: 'invalid-thumbnail-url', sourceUrl: 'https://example.net/reference',
  relevance: { classification: 'unknown', reasons: [], reviewRequired: true },
}
const fourth: CandidateImage = {
  id: 'sample-low', pageTitle: 'Sample public directory', sourceType: 'Directory',
  relevance: { classification: 'low', reasons: ['The public context differs from the supplied example.'], reviewRequired: false },
}
export const scenarioLabels = {
  many: 'Multiple findings', one: 'One finding', empty: 'No findings', partial: 'Partial findings',
  missing: 'Missing metadata', queued: 'Queued', running: 'Running', failed: 'Failed',
} as const
export type MockScenario = keyof typeof scenarioLabels
export type PreviewState = 'idle' | MockScenario
// Static, fictional states. No timers imply that a real investigation has run.
export const investigationFixtures: Record<MockScenario, InvestigationResult> = {
  many: { investigationId: 'sample-many', status: 'completed', candidates: [first, second, third, fourth] },
  one: { investigationId: 'sample-one', status: 'completed', candidates: [first] },
  empty: { investigationId: 'sample-empty', status: 'completed', candidates: [] },
  partial: { investigationId: 'sample-partial', status: 'completed', candidates: [first, third], warnings: ['Some sources are unavailable. These findings are incomplete.'] },
  missing: { investigationId: 'sample-missing', status: 'completed', candidates: [{ id: 'missing-all' }, third] },
  queued: { investigationId: 'sample-queued', status: 'queued' },
  running: { investigationId: 'sample-running', status: 'running', candidates: [second] },
  failed: { investigationId: 'sample-failed', status: 'failed', error: 'The search could not finish. Try again.' },
}
