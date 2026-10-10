import { useCallback, useEffect, useRef, useState, type RefObject } from 'react'

export type CameraStatus = 'idle' | 'starting' | 'active' | 'error'

export interface UseCamera {
  /** Attach to a <video autoPlay playsInline muted> element. */
  videoRef: RefObject<HTMLVideoElement | null>
  status: CameraStatus
  error: string | null
  /** Video inputs on this machine. Labels are empty until permission is granted. */
  devices: MediaDeviceInfo[]
  /** The deviceId of the camera currently streaming, if any. */
  activeDeviceId: string | undefined
  /** Start the camera, optionally a specific device. Restarts if already running. */
  start: (deviceId?: string) => Promise<void>
  stop: () => void
  /** Grab the current frame as an image. Rejects if the camera isn't active. */
  capture: (type?: string, quality?: number) => Promise<Blob>
}

function describeError(err: unknown): string {
  if (err instanceof DOMException) {
    switch (err.name) {
      case 'NotAllowedError':
        return 'Camera permission was denied. Allow camera access in your browser’s site settings and try again.'
      case 'NotFoundError':
      case 'OverconstrainedError':
        return 'No camera was found.'
      case 'NotReadableError':
        return 'The camera is in use by another application.'
      case 'SecurityError':
        return 'Camera access is blocked on this page. It requires HTTPS or localhost.'
    }
  }
  return err instanceof Error ? err.message : 'Could not start the camera.'
}

export function useCamera(): UseCamera {
  const videoRef = useRef<HTMLVideoElement>(null)
  const streamRef = useRef<MediaStream | null>(null)
  // Bumped on every start/stop so a slow getUserMedia call that's been superseded
  // can tell it should discard its stream instead of clobbering the newer one.
  const requestRef = useRef(0)

  const [status, setStatus] = useState<CameraStatus>('idle')
  const [error, setError] = useState<string | null>(null)
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([])
  const [activeDeviceId, setActiveDeviceId] = useState<string>()

  const refreshDevices = useCallback(async () => {
    if (!navigator.mediaDevices?.enumerateDevices) return
    try {
      const all = await navigator.mediaDevices.enumerateDevices()
      setDevices(all.filter((d) => d.kind === 'videoinput' && d.deviceId))
    } catch {
      // Camera enumeration can be restricted independently of an active stream.
      // Keep the preview working even when camera switching is unavailable.
    }
  }, [])

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop())
    streamRef.current = null
    if (videoRef.current) videoRef.current.srcObject = null
  }, [])

  const stop = useCallback(() => {
    requestRef.current++
    releaseStream()
    setActiveDeviceId(undefined)
    setStatus('idle')
  }, [releaseStream])

  const start = useCallback(
    async (deviceId?: string) => {
      const request = ++requestRef.current
      releaseStream()
      setError(null)
      setActiveDeviceId(undefined)

      if (!navigator.mediaDevices?.getUserMedia) {
        setError(
          window.isSecureContext
            ? 'This browser does not support camera access.'
            : 'Camera access requires HTTPS or localhost.',
        )
        setStatus('error')
        return
      }

      setStatus('starting')
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: false,
          video: deviceId
            ? { deviceId: { exact: deviceId } }
            : { facingMode: { ideal: 'environment' } },
        })
        if (request !== requestRef.current) {
          stream.getTracks().forEach((track) => track.stop())
          return
        }

        streamRef.current = stream
        const video = videoRef.current
        if (!video) throw new Error('The camera preview is unavailable.')
        video.srcObject = stream
        video.muted = true
        video.playsInline = true

        const [track] = stream.getVideoTracks()
        setActiveDeviceId(track?.getSettings().deviceId)
        track?.addEventListener('ended', () => {
          // Fires when the camera is unplugged or revoked, not when we stop it ourselves.
          if (streamRef.current !== stream) return
          requestRef.current++
          releaseStream()
          setActiveDeviceId(undefined)
          setError('The camera disconnected.')
          setStatus('error')
        })

        // Wait for playback before allowing a capture. Autoplay timing differs
        // between browsers, especially Safari on mobile devices.
        await video.play()
        if (request !== requestRef.current) return
        setStatus('active')
        // Device labels only become visible once permission has been granted.
        await refreshDevices()
      } catch (err) {
        if (request !== requestRef.current) return
        releaseStream()
        setActiveDeviceId(undefined)
        setError(describeError(err))
        setStatus('error')
      }
    },
    [releaseStream, refreshDevices],
  )

  const capture = useCallback(
    (type = 'image/jpeg', quality = 0.92) =>
      new Promise<Blob>((resolve, reject) => {
        const video = videoRef.current
        if (!video || !streamRef.current || video.videoWidth === 0 || video.readyState < 2) {
          reject(new Error('Camera is not active.'))
          return
        }
        const canvas = document.createElement('canvas')
        canvas.width = video.videoWidth
        canvas.height = video.videoHeight
        const context = canvas.getContext('2d')
        if (!context) {
          reject(new Error('This browser could not capture the camera frame.'))
          return
        }
        context.drawImage(video, 0, 0)
        canvas.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error('Could not encode frame.'))),
          type,
          quality,
        )
      }),
    [],
  )

  useEffect(() => {
    const media = navigator.mediaDevices
    if (!media?.enumerateDevices) return
    let cancelled = false
    media.enumerateDevices().then((all) => {
      if (!cancelled) setDevices(all.filter((d) => d.kind === 'videoinput' && d.deviceId))
    }).catch(() => { /* Device labels may be unavailable until access is granted. */ })
    media.addEventListener?.('devicechange', refreshDevices)
    return () => {
      cancelled = true
      media.removeEventListener?.('devicechange', refreshDevices)
    }
  }, [refreshDevices])

  // Turn the camera off when the component using it unmounts.
  useEffect(
    () => () => {
      requestRef.current++
      releaseStream()
    },
    [releaseStream],
  )

  return { videoRef, status, error, devices, activeDeviceId, start, stop, capture }
}
