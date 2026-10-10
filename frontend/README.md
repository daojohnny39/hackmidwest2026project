# Frontend

React + TypeScript + Vite app that streams from the device camera and captures photos.

```bash
npm install
npm run dev       # http://localhost:5173
npm run build     # type-check + production build into dist/
npm run lint
```

## Camera access

Camera logic lives in [`src/hooks/useCamera.ts`](src/hooks/useCamera.ts). It wraps
`navigator.mediaDevices.getUserMedia` and exposes:

| Field | |
| --- | --- |
| `videoRef` | Attach to a `<video autoPlay playsInline muted>` element |
| `status` | `'idle' \| 'starting' \| 'active' \| 'error'` |
| `error` | Human-readable reason the camera couldn't start |
| `devices`, `activeDeviceId` | Available cameras and the one in use |
| `start(deviceId?)`, `stop()` | Turn the camera on/off, or switch cameras |
| `capture(type?, quality?)` | Current frame as a `Blob` (JPEG by default) |

The camera is released automatically when the component using the hook unmounts.

Browsers only allow camera access in a **secure context**: `localhost` works, but to
test from a phone on your LAN the page must be served over HTTPS (e.g. add
[`@vitejs/plugin-basic-ssl`](https://github.com/vitejs/vite-plugin-basic-ssl) and run
`npm run dev -- --host`).
