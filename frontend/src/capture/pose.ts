import type { Matrix, Pose } from './types.ts'

/** MediaPipe MatrixData is column-major. R = Rz(roll) Ry(yaw) Rx(pitch).
 * Geometry axes: x image-right, y up, z toward camera. Our signs are
 * participant-right yaw, chin-up pitch, participant-right-shoulder roll.
 * Translation and positive uniform scale do not affect the rotation.
 */
export function matrixToPose(matrix: Matrix | undefined): Pose | null {
  if (!matrix || matrix.rows !== 4 || matrix.columns !== 4 || matrix.data.length !== 16) return null
  const m = matrix.data
  if (!m.every(Number.isFinite) || Math.abs(m[15] - 1) > 0.001 || [m[3], m[7], m[11]].some((n) => Math.abs(n) > 0.001)) return null
  const columns = [[m[0], m[1], m[2]], [m[4], m[5], m[6]], [m[8], m[9], m[10]]]
  const scales = columns.map((c) => Math.hypot(...c))
  const scale = scales.reduce((a, b) => a + b) / 3
  if (scale < 1e-6 || scales.some((s) => Math.abs(s / scale - 1) > 0.02)) return null
  const [x, y, z] = columns.map((c) => c.map((v) => v / scale))
  const dot = (a: number[], b: number[]) => a.reduce((sum, v, i) => sum + v * b[i], 0)
  if ([dot(x, y), dot(x, z), dot(y, z)].some((v) => Math.abs(v) > 0.02)) return null
  const det = x[0] * (y[1] * z[2] - y[2] * z[1]) - y[0] * (x[1] * z[2] - x[2] * z[1]) + z[0] * (x[1] * y[2] - x[2] * y[1])
  if (Math.abs(det - 1) > 0.03) return null
  const degrees = 180 / Math.PI
  const yaw = Math.asin(Math.max(-1, Math.min(1, -x[2]))) * degrees
  if (Math.abs(yaw) >= 85) return null // Euler singularity is far outside this front preset.
  return { yaw: -yaw, pitch: -Math.atan2(y[2], z[2]) * degrees, roll: Math.atan2(x[1], x[0]) * degrees }
}
