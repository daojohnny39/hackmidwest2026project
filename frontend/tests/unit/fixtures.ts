import { FACE_OVAL } from '../../src/capture/gates.ts'
import type { Matrix, Observation, Pose } from '../../src/capture/types.ts'

export function matrix(pose: Partial<Pose> = {}, scale = 1): Matrix {
  const x = -(pose.pitch ?? 0) * Math.PI / 180, y = -(pose.yaw ?? 0) * Math.PI / 180, z = (pose.roll ?? 0) * Math.PI / 180
  const cx = Math.cos(x), sx = Math.sin(x), cy = Math.cos(y), sy = Math.sin(y), cz = Math.cos(z), sz = Math.sin(z)
  const rows = [
    [cz * cy, cz * sy * sx - sz * cx, cz * sy * cx + sz * sx],
    [sz * cy, sz * sy * sx + cz * cx, sz * sy * cx - cz * sx],
    [-sy, cy * sx, cy * cx],
  ]
  return { rows: 4, columns: 4, data: [
    ...rows.map((r) => r[0] * scale), 0, ...rows.map((r) => r[1] * scale), 0,
    ...rows.map((r) => r[2] * scale), 0, 0.5, -2, -45, 1,
  ] }
}
export function observation(pose: Partial<Pose> = {}, box = { x: .3, y: .225, width: .4, height: .55 }): Observation {
  const face = Array.from({ length: 478 }, () => ({ x: .5, y: .5, z: 0 }))
  FACE_OVAL.forEach((index, i) => {
    const angle = 2 * Math.PI * i / FACE_OVAL.length
    face[index] = { x: box.x + box.width / 2 + Math.cos(angle) * box.width / 2, y: box.y + box.height / 2 + Math.sin(angle) * box.height / 2, z: 0 }
  })
  return { faces: [face], matrices: [matrix(pose)] }
}
