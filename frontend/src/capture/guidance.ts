import type { CaptureState, ResetReason } from './types.ts'

export const REASON_LABELS: Record<ResetReason, string> = {
  no_face: 'No face detected', multiple_faces: 'Multiple faces', invalid_landmarks: 'Unreliable face outline',
  clipped: 'Face near the frame edge', too_close: 'Too close', too_far: 'Too far away', off_center: 'Face off-center',
  invalid_pose: 'Unreliable angle estimate', yaw: 'Face turned', pitch: 'Chin angle', roll: 'Head tilted',
  stale_result: 'Analysis took too long', sample_gap: 'Gap between fresh frames', paused: 'Paused', page_hidden: 'Page hidden',
  stopped: 'Camera stopped', camera_changed: 'Camera changed', camera_lost: 'Camera disconnected', analysis_error: 'Analysis failed', encoding_error: 'Photo encoding failed',
}

export function guidance(state: CaptureState): string {
  switch (state.guidanceCode) {
    case 'idle': case 'stopped': case 'camera_changed': return 'Face the camera at eye level. Center your face in the outline.'
    case 'preparing': return 'Preparing camera and local face analysis…'
    case 'no_face': return 'Move your face into view.'
    case 'multiple_faces': return 'Keep one person in view.'
    case 'clipped': case 'too_close': return 'Move a little farther away and keep your whole face in view.'
    case 'too_far': return 'Move closer to the camera.'
    case 'off_center': return 'Center your face in the outline.'
    case 'yaw': return 'Turn your face toward the camera.'
    case 'pitch': return state.pose && state.pose.pitch > 0 ? 'Lower your chin slightly.' : 'Raise your chin slightly.'
    case 'roll': return 'Keep your head upright.'
    case 'invalid_pose': case 'invalid_landmarks': return 'Face the camera in clear, even lighting.'
    case 'hold': return 'Hold this position…'
    case 'stale_result': case 'sample_gap': return 'Checking your position… keep the camera steady.'
    case 'paused': case 'page_hidden': return 'Capture paused. Resume when you are ready.'
    case 'capturing': return 'Saving your front photo…'
    case 'complete': return 'Front photo captured.'
    default: return 'Restart the camera to try again.'
  }
}

export function troubleshooting(state: CaptureState): string[] {
  const counts = { ...state.diagnostics.blockedCounts }
  const top = (Object.keys(counts) as ResetReason[]).sort((a, b) => (counts[b] ?? 0) - (counts[a] ?? 0))[0]
  const tips: string[] = []
  if (top === 'stale_result' || top === 'sample_gap' || state.guidanceCode === 'sample_gap') tips.push('Processing is falling behind. Close other busy tabs or apps, keep this page visible, and try a faster device if it continues.')
  else if (top === 'no_face' || top === 'invalid_pose' || top === 'invalid_landmarks') tips.push('Use even light in front of you, avoid a bright window behind you, and keep your face unobstructed.')
  else if (top === 'multiple_faces') tips.push('Keep other people and pictures of faces outside the camera view.')
  else if (top === 'too_far') tips.push('Move closer until your face fills about half the image height.')
  else if (top === 'too_close' || top === 'clipped') tips.push('Move back until your whole face fits inside the frame with room around the edges.')
  else if (top === 'off_center') tips.push('Move your face toward the center of the oval and keep the camera at eye level.')
  else tips.push('Support the phone or webcam, face the lens at eye level, and relax your shoulders. Small angle changes can restart the hold.')
  tips.push('Follow one instruction at a time, then stay still for 400 ms. The ±5° limits have not changed.')
  return tips
}
