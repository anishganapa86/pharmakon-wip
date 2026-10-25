import fs from 'node:fs'
import path from 'node:path'
import vm from 'node:vm'
import { expect, test } from '@playwright/test'

const publicDir = path.resolve(import.meta.dirname, '../public')
const indexHtml = fs.readFileSync(path.resolve(import.meta.dirname, '../index.html'), 'utf-8')

function pngSize(file: string): { width: number; height: number } {
  const data = fs.readFileSync(path.join(publicDir, file))
  expect([...data.subarray(0, 8)]).toEqual([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
  return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) }
}

interface Harness {
  listeners: Record<string, (event: unknown) => void>
  cache: Map<string, unknown>
  deleted: string[]
  fetched: string[]
}

function loadWorker(responses: Record<string, { status: number; cacheControl?: string }>) {
  const state: Harness = { listeners: {}, cache: new Map(), deleted: [], fetched: [] }
  const store = new Map<string, Map<string, unknown>>([['old-cache', new Map()]])
  const headers = (control?: string) => ({
    get: (name: string) => (name.toLowerCase() === 'cache-control' ? (control ?? null) : null),
  })
  const makeResponse = (url: string) => {
    const spec = responses[new URL(url).pathname] ?? { status: 200 }
    return {
      status: spec.status,
      type: 'basic',
      headers: headers(spec.cacheControl),
      clone() {
        return this
      },
    }
  }
  const caches = {
    open: async (name: string) => {
      const bucket = store.get(name) ?? new Map<string, unknown>()
      store.set(name, bucket)
      return {
        addAll: async () => undefined,
        put: async (request: { url: string }, response: unknown) => {
          bucket.set(request.url, response)
          state.cache.set(request.url, response)
        },
      }
    },
    match: async (request: { url: string }) => {
      for (const bucket of store.values()) {
        if (bucket.has(request.url)) return bucket.get(request.url)
      }
      return undefined
    },
    keys: async () => [...store.keys()],
    delete: async (name: string) => {
      state.deleted.push(name)
      return store.delete(name)
    },
  }
  const self = {
    location: { origin: 'https://demo.example.com' },
    addEventListener: (type: string, fn: (event: unknown) => void) => {
      state.listeners[type] = fn
    },
    skipWaiting: () => undefined,
    clients: { claim: () => undefined },
  }
  const context = vm.createContext({
    self,
    caches,
    URL,
    Promise,
    fetch: async (request: { url: string }) => {
      state.fetched.push(request.url)
      return makeResponse(request.url)
    },
  })
  const source = fs.readFileSync(path.join(publicDir, 'sw.js'), 'utf-8').replaceAll(
    '__PHARMAKON_BUILD_ID__',
    'test',
  )
  vm.runInContext(source, context)
  return state
}

async function dispatch(
  state: Harness,
  url: string,
  options: { method?: string; mode?: string; headers?: Record<string, string> } = {},
): Promise<{ handled: boolean }> {
  const headerMap = new Map(
    Object.entries(options.headers ?? {}).map(([k, v]) => [k.toLowerCase(), v]),
  )
  let handled = false
  let pending: Promise<unknown> = Promise.resolve()
  state.listeners.fetch({
    request: {
      url,
      method: options.method ?? 'GET',
      mode: options.mode ?? 'cors',
      headers: { has: (name: string) => headerMap.has(name.toLowerCase()) },
    },
    respondWith: (promise: Promise<unknown>) => {
      handled = true
      pending = promise
    },
  })
  await pending
  await new Promise((resolve) => setTimeout(resolve, 0))
  return { handled }
}

const ORIGIN = 'https://demo.example.com'

test.describe('manifest and icons', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(publicDir, 'manifest.json'), 'utf-8'))

  test('colors come from the palette and the old dark theme is gone', () => {
    expect(manifest.theme_color).toBe('#f4f7f6')
    expect(manifest.background_color).toBe('#f4f7f6')
    expect(JSON.stringify(manifest)).not.toContain('#16171d')
    expect(indexHtml).not.toContain('#16171d')
    expect(manifest.display).toBe('standalone')
  })

  test('192 and 512 icons exist as plain and maskable PNGs', () => {
    for (const size of [192, 512]) {
      for (const purpose of ['any', 'maskable']) {
        const icon = manifest.icons.find(
          (i: { sizes: string; purpose: string }) =>
            i.sizes === `${size}x${size}` && i.purpose === purpose,
        )
        expect(icon, `${purpose} ${size}`).toBeTruthy()
        expect(pngSize(icon.src.slice(1))).toEqual({ width: size, height: size })
      }
    }
  })

  test('the apple touch icon is 180px and the iOS meta tags are present', () => {
    expect(pngSize('apple-touch-icon.png')).toEqual({ width: 180, height: 180 })
    expect(indexHtml).toContain('rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png"')
    expect(indexHtml).toContain('name="apple-mobile-web-app-capable" content="yes"')
    expect(indexHtml).toContain('name="apple-mobile-web-app-title" content="Pharmakon"')
    expect(indexHtml).toContain('name="apple-mobile-web-app-status-bar-style"')
    expect(indexHtml).toContain('viewport-fit=cover')
  })
})

test.describe('service worker', () => {
  test('is versioned', () => {
    const source = fs.readFileSync(path.join(publicDir, 'sw.js'), 'utf-8')
    expect(source).toMatch(/SW_VERSION = [2-9]/)
  })

  test('API calls, non-GET and authenticated requests are network-only', async () => {
    const state = loadWorker({})
    expect((await dispatch(state, `${ORIGIN}/api/users/abc/doses`)).handled).toBe(false)
    expect((await dispatch(state, `${ORIGIN}/api/auth/me`)).handled).toBe(false)
    expect((await dispatch(state, `${ORIGIN}/auth/me`)).handled).toBe(false)
    expect((await dispatch(state, `${ORIGIN}/users/abc/doses`)).handled).toBe(false)
    expect((await dispatch(state, `${ORIGIN}/assets/app.js`, { method: 'POST' })).handled).toBe(false)
    expect(
      (await dispatch(state, `${ORIGIN}/assets/app.js`, { headers: { Authorization: 'x' } })).handled,
    ).toBe(false)
    expect((await dispatch(state, 'https://api.example.com/assets/app.js')).handled).toBe(false)
    expect(state.fetched).toEqual([])
    expect(state.cache.size).toBe(0)
  })

  test('static assets are cached and served from the cache', async () => {
    const state = loadWorker({})
    expect((await dispatch(state, `${ORIGIN}/assets/app.js`)).handled).toBe(true)
    expect(state.cache.has(`${ORIGIN}/assets/app.js`)).toBe(true)
    expect((await dispatch(state, `${ORIGIN}/assets/app.js`)).handled).toBe(true)
    expect(state.fetched).toEqual([`${ORIGIN}/assets/app.js`])
  })

  test('a no-store or private response is never cached, even for a static path', async () => {
    const state = loadWorker({
      '/assets/a.js': { status: 200, cacheControl: 'no-store' },
      '/assets/b.js': { status: 200, cacheControl: 'private, max-age=60' },
      '/assets/c.js': { status: 500 },
    })
    for (const name of ['a', 'b', 'c']) {
      await dispatch(state, `${ORIGIN}/assets/${name}.js`)
    }
    expect(state.cache.size).toBe(0)
  })

  test('only the static shell pages are handled as navigations', async () => {
    const state = loadWorker({})
    expect((await dispatch(state, `${ORIGIN}/`, { mode: 'navigate' })).handled).toBe(true)
    expect((await dispatch(state, `${ORIGIN}/login.html`, { mode: 'navigate' })).handled).toBe(true)
    expect((await dispatch(state, `${ORIGIN}/api/users/abc/doses`, { mode: 'navigate' })).handled).toBe(false)
    expect((await dispatch(state, `${ORIGIN}/users/abc/doses`, { mode: 'navigate' })).handled).toBe(false)
  })

  test('the clear-caches message deletes every cache', async () => {
    const state = loadWorker({})
    await dispatch(state, `${ORIGIN}/assets/app.js`)
    let pending: Promise<unknown> = Promise.resolve()
    state.listeners.message({
      data: 'clear-caches',
      waitUntil: (promise: Promise<unknown>) => {
        pending = promise
      },
    })
    await pending
    expect(state.deleted.length).toBeGreaterThanOrEqual(2)
    expect(state.deleted).toContain('old-cache')
  })

  test('activation removes caches from older versions', async () => {
    const state = loadWorker({})
    let pending: Promise<unknown> = Promise.resolve()
    state.listeners.activate({
      waitUntil: (promise: Promise<unknown>) => {
        pending = promise
      },
    })
    await pending
    expect(state.deleted).toContain('old-cache')
  })
})
