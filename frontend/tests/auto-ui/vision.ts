import { observation } from '../unit/fixtures'
export interface TestVision { mode: 'no_face' | 'valid' | 'resets' | 'pitch_up' | 'pitch_down'; calls: number; closed: number; failure?: boolean; inferenceWidth?: number; inferenceHeight?: number }
declare global { interface Window { testVision: TestVision } }
export const FilesetResolver = { forVisionTasks: async () => ({}) }
export class FaceLandmarker {
  static async createFromOptions() {
    if (window.testVision.failure) throw new Error('Test model failure')
    return new FaceLandmarker()
  }
  detectForVideo(image: HTMLCanvasElement) {
    const settings = window.testVision
    settings.calls++
    settings.inferenceWidth = image.width
    settings.inferenceHeight = image.height
    let value = observation()
    if (settings.mode === 'no_face') value = { faces: [], matrices: [] }
    if (settings.mode === 'resets' && settings.calls % 3 === 0) value = observation({ yaw: 6 })
    if (settings.mode === 'pitch_up') value = observation({ pitch: 12 })
    if (settings.mode === 'pitch_down') value = observation({ pitch: -12 })
    return { faceLandmarks: value.faces, facialTransformationMatrixes: value.matrices }
  }
  close() { window.testVision.closed++ }
}
