import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'

const repo = fileURLToPath(new URL('../../', import.meta.url))
const frontend = fileURLToPath(new URL('../', import.meta.url))
const localPython = fileURLToPath(new URL(
  process.platform === 'win32' ? '../../.venv/Scripts/python.exe' : '../../.venv/bin/python',
  import.meta.url,
))
const python = process.env.CYBERPI_PYTHON || (existsSync(localPython) ? localPython : 'python3')
// `npm run dev:fake` passes --fake-db: dev mode searches the team's fake image database instead of the
// internet. It gets its own API and website ports, so it can run next to `npm run dev`.
const fakeDb = process.argv.includes('--fake-db')
const viteArgs = process.argv.slice(2).filter(arg => arg !== '--fake-db')
if (fakeDb && !viteArgs.some(arg => arg === '--port' || arg.startsWith('--port='))) viteArgs.push('--port', '5180')
const api = process.env.CYBERPI_API_URL || (fakeDb ? 'http://127.0.0.1:8001' : 'http://127.0.0.1:8000')
const children = new Set()
let stopping = false

async function stop(code) {
  if (stopping) return
  stopping = true
  process.exitCode = code
  const running = [...children]
  const exited = running.map(child => new Promise(resolve => child.once('close', resolve)))
  for (const child of running) child.kill('SIGTERM')
  // A provider call may still be running; don't leave owned servers behind after Ctrl-C.
  const timer = setTimeout(() => {
    for (const child of running) if (children.has(child)) child.kill('SIGKILL')
  }, 5000)
  await Promise.all(exited)
  clearTimeout(timer)
}

function launch(command, args, cwd, label, env = process.env) {
  const child = spawn(command, args, { cwd, env, stdio: 'inherit' })
  children.add(child)
  child.once('error', error => {
    console.error(`${label} could not start (${error.code}).`)
    if (label === 'Search API') console.error('Install requirements.txt in the repository .venv, or set CYBERPI_PYTHON.')
    void stop(1)
  })
  child.once('close', code => {
    children.delete(child)
    if (!stopping) {
      console.error(`${label} stopped (exit ${code ?? 'signal'}).`)
      void stop(code || 1)
    }
  })
  return child
}

async function apiReady() {
  let response
  try {
    response = await fetch(new URL('/api/health', api), { signal: AbortSignal.timeout(1000) })
  } catch (error) {
    if (error.code === 'ERR_INVALID_URL') throw new Error('CYBERPI_API_URL must be an HTTP URL.')
    return false
  }
  const body = await response.json().catch(() => null)
  if (!response.ok || body?.service !== 'cyberpi-search' || body?.status !== 'ok') {
    throw new Error('The API address is serving a different or outdated service. Stop it or update CYBERPI_API_URL.')
  }
  if (fakeDb && body.search !== 'fakedb') {
    throw new Error(`The API at ${api} isn't running dev mode (fake database). Stop it or run \`npm run dev\`.`)
  }
  if (!fakeDb && body.search === 'fakedb') {
    throw new Error(`The API at ${api} is dev mode (fake database). Stop it or run \`npm run dev:fake\`.`)
  }
  return true
}

process.once('SIGINT', () => { void stop(0) })
process.once('SIGTERM', () => { void stop(0) })

try {
  if (await apiReady()) {
    console.log('Using the running CyberPI search API.')
  } else if (process.env.CYBERPI_API_URL) {
    // A custom target is managed separately; never silently launch a different backend.
    throw new Error('The search API at CYBERPI_API_URL is unreachable. Start that service first.')
  } else if (!stopping) {
    console.log(fakeDb
      ? 'Starting the CyberPI search API in dev mode (fake image database)…'
      : 'Starting the CyberPI search API with the repository configuration…')
    launch(python, ['-u', '-m', 'pipeline', 'serve', '--models', '--port', new URL(api).port,
      ...(fakeDb ? ['--fake-db'] : [])], repo, 'Search API')
    const deadline = Date.now() + 30_000
    while (!stopping && !await apiReady()) {
      if (Date.now() >= deadline) throw new Error('The search API did not become ready within 30 seconds. Check the startup errors above.')
      await delay(250)
    }
  }
  if (!stopping) {
    // vite.config.ts proxies /api to CYBERPI_API_URL.
    launch(process.execPath, [fileURLToPath(new URL('../node_modules/vite/bin/vite.js', import.meta.url)),
      ...viteArgs], frontend, 'Website', { ...process.env, CYBERPI_API_URL: api })
  }
} catch (error) {
  console.error(error.message)
  await stop(1)
}
