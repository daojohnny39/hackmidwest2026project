/** Proposed Member 1 contract. No live search endpoint is connected. */
export type InvestigationStatus = 'queued' | 'running' | 'completed' | 'failed'
export type EvidenceLevel = 'high' | 'medium' | 'low' | 'unknown'
export interface SubjectInput {
  name: string
  username: string
  context: string
  authorized: boolean
}
export interface CandidateImage {
  id: string
  imageUrl?: string
  sourceUrl?: string
  pageTitle?: string
  sourceType?: string
  relevance?: { classification: EvidenceLevel; reasons: string[]; reviewRequired: boolean }
  threat?: { level: EvidenceLevel; indicators: string[] }
}
export interface InvestigationResult {
  investigationId: string
  status: InvestigationStatus
  candidates?: CandidateImage[]
  warnings?: string[]
  error?: string
}
export interface SearchAccepted {
  investigationId: string
  status: InvestigationStatus
}
