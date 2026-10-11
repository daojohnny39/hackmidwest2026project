import { useCallback, useEffect, useRef, useState } from 'react'
import { useCamera } from './useCamera'
import { FrontCaptureEngine } from '../capture/engine'
import { BrowserCaptureSource } from '../capture/browserSource'
import { emptyState, type FrontCaptureResult } from '../capture/types'

/** Camera lifecycle adapter; a future UI can consume these actions and state. */
export function useAutoCapture(onCaptured?: (result: FrontCaptureResult) => void) {
  const engine = useRef<FrontCaptureEngine | null>(null)
  const onDisconnected = useCallback((message: string) => engine.current?.fail('camera_error', message), [])
  const camera = useCamera(onDisconnected)
  const { start: startCamera, stop: stopCamera, videoRef } = camera
  const [state, setState] = useState(emptyState)
  const [result, setResult] = useState<FrontCaptureResult | null>(null)
  const source = useRef<BrowserCaptureSource | null>(null)
  const operation = useRef(0)
  const selectedDevice = useRef<string | undefined>(undefined)
  const callback = useRef(onCaptured)
  useEffect(() => { callback.current = onCaptured }, [onCaptured])

  const release = useCallback(() => {
    operation.current++
    source.current?.close()
    source.current = null
    stopCamera()
  }, [stopCamera])

  const interrupt = useCallback((reason: 'paused' | 'page_hidden' | 'stopped' | 'camera_changed') => {
    engine.current?.interrupt(reason)
    release()
  }, [release])

  const boot = useCallback(async (current: FrontCaptureEngine, deviceId?: string) => {
    const op = ++operation.current
    const started = await startCamera(deviceId)
    if (op !== operation.current) return
    if (!started || !videoRef.current) {
      current.fail('camera_error', 'Camera could not start. Check camera access and try again.')
      return
    }
    if (document.hidden) { interrupt('page_hidden'); return }
    const live = new BrowserCaptureSource(videoRef.current, current)
    source.current = live
    await live.start()
  }, [startCamera, videoRef, interrupt])

  const start = useCallback((deviceId?: string) => {
    if (deviceId) selectedDevice.current = deviceId
    if (engine.current?.state.phase === 'error') {
      engine.current.resume()
      void boot(engine.current, selectedDevice.current)
      return
    }
    engine.current?.interrupt('camera_changed')
    engine.current?.dispose()
    release()
    const current = new FrontCaptureEngine(crypto.randomUUID(), {
      onState: (next) => {
        setState(next)
        if (next.phase === 'error') release()
      },
      onCaptured: (capture) => {
        setResult(capture)
        release()
        callback.current?.(capture)
      },
    })
    engine.current = current
    setState(current.state)
    void boot(current, selectedDevice.current)
  }, [boot, release])

  const resume = useCallback(() => {
    const current = engine.current
    if (!current || current.state.phase !== 'paused') return
    current.resume()
    void boot(current, selectedDevice.current)
  }, [boot])

  const changeCamera = useCallback((deviceId: string) => {
    selectedDevice.current = deviceId
    const current = engine.current
    if (!current || current.state.phase === 'complete') { start(deviceId); return }
    interrupt('camera_changed')
    current.resume()
    void boot(current, deviceId)
  }, [boot, interrupt, start])

  useEffect(() => {
    if (camera.status === 'error' && engine.current && ['preparing', 'guiding', 'holding', 'capturing'].includes(engine.current.state.phase)) {
      engine.current.fail('camera_error', camera.error ?? 'The camera disconnected. Please retry.')
    }
  }, [camera.status, camera.error])

  useEffect(() => {
    const hide = () => {
      if (document.hidden && engine.current && ['preparing', 'guiding', 'holding', 'capturing'].includes(engine.current.state.phase)) interrupt('page_hidden')
    }
    document.addEventListener('visibilitychange', hide)
    return () => document.removeEventListener('visibilitychange', hide)
  }, [interrupt])

  useEffect(() => () => {
    engine.current?.dispose()
    release()
  }, [release])

  return {
    camera, state, result,
    start, retake: start, retry: start, resume, changeCamera,
    pause: () => interrupt('paused'), stop: () => interrupt('stopped'),
    deleteResult: () => setResult(null),
  }
}
