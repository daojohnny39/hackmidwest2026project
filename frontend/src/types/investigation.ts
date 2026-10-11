/** Member 1 contract, served by `python -m pipeline serve` (pipeline/server.py). */
export type InvestigationStatus = 'queued' | 'running' | 'completed' | 'failed'
export type SearchStage = 'preparing' | 'searching' | 'comparing' | 'reviewing' | 'checking' | 'finalizing'
export type EvidenceLevel = 'high' | 'medium' | 'low' | 'unknown'
/** How closely the face in a source matches the reference photo (Amazon Rekognition bands). */
export type ConfidenceLevel = 'strong' | 'likely' | 'possible' | 'weak' | 'unlikely' | 'unknown'
export interface SubjectInput {
  firstName: string
  lastName: string
  city: string
  age: string
  authorized: boolean
  externalProcessing: boolean
}
export interface CandidateImage {
  id: string
  imageUrl?: string
  sourceUrl?: string
  pageTitle?: string
  sourceType?: string
  relevance?: { classification: EvidenceLevel; reasons: string[]; reviewRequired: boolean }
  /** score is the 0-100 face similarity; absent when the backend has none. */
  confidence?: { score?: number; level: ConfidenceLevel; reasons: string[] }
  /** The source site's threat rating, score 0-100. Only dev mode sets it (pipeline/search/fakedb_backend.py). */
  threat?: { score?: number; level: EvidenceLevel; indicators: string[] }
}
export interface InvestigationResult {
  investigationId: string
  status: InvestigationStatus
  stage?: SearchStage
  candidates?: CandidateImage[]
  warnings?: string[]
  error?: string
}
export interface SearchAccepted {
  investigationId: string
  status: InvestigationStatus
}
