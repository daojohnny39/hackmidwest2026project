import { matrixToPose } from './pose.ts'
import { FRONT_PRESET, type GateResult, type Observation } from './types.ts'

// FaceLandmarker.FACE_LANDMARKS_FACE_OVAL endpoints, without repeated vertices.
export const FACE_OVAL = [10, 338, 297, 332, 284, 251, 389, 356, 454, 323, 361, 288, 397, 365, 379, 378, 400, 377, 152, 148, 176, 149, 150, 136, 172, 58, 132, 93, 234, 127, 162, 21, 54, 103, 67, 109]

export function evaluateFront(observation: Observation): GateResult {
  const result: GateResult = { pose: null, faceBox: null, failedChecks: [] }
  const failures = result.failedChecks
  if (observation.faces.length !== 1) {
    failures.push(observation.faces.length ? 'multiple_faces' : 'no_face')
    return result
  }
  const points = FACE_OVAL.map((i) => observation.faces[0][i])
  if (points.some((p) => !p || !Number.isFinite(p.x) || !Number.isFinite(p.y))) {
    failures.push('invalid_landmarks')
  } else {
    const left = Math.min(...points.map((p) => p.x)), right = Math.max(...points.map((p) => p.x))
    const top = Math.min(...points.map((p) => p.y)), bottom = Math.max(...points.map((p) => p.y))
    const height = bottom - top, width = right - left
    result.faceBox = { x: left, y: top, width, height }
    const f = FRONT_PRESET.framing
    if (width <= 0 || height <= 0) failures.push('invalid_landmarks')
    if (left < f.margin || right > 1 - f.margin || top < f.margin || bottom > 1 - f.margin) failures.push('clipped')
    if (height > f.maxHeight) failures.push('too_close')
    if (height < f.minHeight) failures.push('too_far')
    if ((left + right) / 2 < f.minCenter || (left + right) / 2 > f.maxCenter || (top + bottom) / 2 < f.minCenter || (top + bottom) / 2 > f.maxCenter) failures.push('off_center')
  }
  result.pose = observation.matrices.length === 1 ? matrixToPose(observation.matrices[0]) : null
  if (!result.pose) failures.push('invalid_pose')
  else for (const axis of ['yaw', 'pitch', 'roll'] as const) {
    if (Math.abs(result.pose[axis]) > FRONT_PRESET.tolerance[axis]) failures.push(axis)
  }
  return result
}
