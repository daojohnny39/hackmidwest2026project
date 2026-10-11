import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer } from 'node:http'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'
import test, { type TestContext } from 'node:test'

const ready = { service: 'cyberpi-search', status: 'ok' }

async function apiServer(t: TestContext, body: Record<string, string> = ready) {
  const server = createServer((req, res) => {
    res.writeHead(req.url === '/api/health' ? 200 : 404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify(body))
  })
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve))
  t.after(() => new Promise<void>((resolve, reject) => {
    if (!server.listening) return resolve()
    server.close(error => error ? reject(error) : resolve())
    server.closeAllConnections()
  }))
  const address = server.address()
  assert(address && typeof address !== 'string')
  return { server, url: `http://127.0.0.1:${address.port}` }
}

function launcher(t: TestContext, api: string, mode: string[] = []) {
  let output = ''
  const child = spawn(process.execPath, ['scripts/dev.mjs', ...mode, '--host', '127.0.0.1', '--port', '0'], {
    cwd: fileURLToPath(new URL('../../', import.meta.url)),
    env: { ...process.env, CYBERPI_API_URL: api, CYBERPI_PYTHON: 'must-not-launch-python', NO_COLOR: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  child.stdout.on('data', data => { output += data })
  child.stderr.on('data', data => { output += data })
  const done = new Promise<number | null>((resolve, reject) => {
    child.once('error', reject)
    child.once('close', resolve)
  })
  t.after(async () => {
    if (child.exitCode === null && child.signalCode === null) child.kill('SIGTERM')
    await done
  })
  return { child, done, output: () => output }
}

test('dev refuses an unreachable custom API before starting the website', { timeout: 15_000 }, async t => {
  const { server, url } = await apiServer(t)
  await new Promise<void>(resolve => server.close(() => resolve()))
  const run = launcher(t, url)
  assert.equal(await run.done, 1)
  assert.match(run.output(), /CYBERPI_API_URL is unreachable/)
  assert.doesNotMatch(run.output(), /Local:/)
})

test('dev refuses a different service at the API address', { timeout: 15_000 }, async t => {
  const { url } = await apiServer(t, { service: 'unrelated-service', status: 'ok' })
  const run = launcher(t, url)
  assert.equal(await run.done, 1)
  assert.match(run.output(), /different or outdated service/)
  assert.doesNotMatch(run.output(), /Local:/)
})

test('dev reuses a healthy API, proxies to it, and leaves it running on shutdown', { timeout: 15_000 }, async t => {
  const { url } = await apiServer(t)
  const run = launcher(t, url)
  const deadline = Date.now() + 10_000
  let website: string | undefined
  while (!(website = run.output().match(/http:\/\/127\.0\.0\.1:\d+/)?.[0])) {
    assert.equal(run.child.exitCode, null, run.output())
    assert(Date.now() < deadline, run.output())
    await delay(50)
  }
  const response = await fetch(`${website}/api/health`)
  assert.equal(response.status, 200)
  assert.deepEqual(await response.json(), ready)
  run.child.kill('SIGTERM')
  assert.equal(await run.done, 0)
  assert.deepEqual(await (await fetch(`${url}/api/health`)).json(), ready)
  await assert.rejects(fetch(website))
})

test('dev:fake refuses an API that is not in dev mode', { timeout: 15_000 }, async t => {
  const { url } = await apiServer(t, { ...ready, search: 'rekognition' })
  const run = launcher(t, url, ['--fake-db'])
  assert.equal(await run.done, 1)
  assert.match(run.output(), /isn't running dev mode \(fake database\)/)
  assert.doesNotMatch(run.output(), /Local:/)
})

test('dev refuses a dev-mode API', { timeout: 15_000 }, async t => {
  const { url } = await apiServer(t, { ...ready, search: 'fakedb' })
  const run = launcher(t, url)
  assert.equal(await run.done, 1)
  assert.match(run.output(), /npm run dev:fake/)
  assert.doesNotMatch(run.output(), /Local:/)
})

test('dev:fake reuses a dev-mode API and proxies to it', { timeout: 15_000 }, async t => {
  const body = { ...ready, search: 'fakedb' }
  const { url } = await apiServer(t, body)
  const run = launcher(t, url, ['--fake-db'])
  const deadline = Date.now() + 10_000
  let website: string | undefined
  while (!(website = run.output().match(/http:\/\/127\.0\.0\.1:\d+/)?.[0])) {
    assert.equal(run.child.exitCode, null, run.output())
    assert(Date.now() < deadline, run.output())
    await delay(50)
  }
  assert.deepEqual(await (await fetch(`${website}/api/health`)).json(), body)
  run.child.kill('SIGTERM')
  assert.equal(await run.done, 0)
})
