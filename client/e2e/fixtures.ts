import fs from 'node:fs'
import path from 'node:path'
import { test as base, type Page } from '@playwright/test'

const runDir =
  process.env.DEMO_RUN_DIR ??
  path.resolve(import.meta.dirname, '../../reports/runs/local')

const FORBIDDEN =
  /\b(safe|no risk|will cause|causes|prevents|recommended|accurate|proven)\b/gi
const ALLOWED_PHRASES = /not a medical device/gi
const BAD_TOKENS = /\b(undefined|NaN|null)\b|\[object/g
const ERROR_SCREEN = /something went wrong|application error|unexpected error/i
const SLOW_STEP_MS = 3000
const CONTEXT_CHARS = 60

interface Problem {
  type: string
  detail: string
}

class Recorder {
  readonly testId: string
  readonly expectFailure: boolean
  screen = 'Concentration'
  problems: Problem[] = []
  private lastCheckpoint = Date.now()
  private seen = new Set<string>()
  private requestStart = new Map<unknown, number>()
  private page: Page | null = null

  constructor(testId: string, expectFailure: boolean) {
    this.testId = testId
    this.expectFailure = expectFailure
  }

  write(entry: Record<string, unknown>): void {
    fs.mkdirSync(runDir, { recursive: true })
    fs.appendFileSync(
      path.join(runDir, 'findings.jsonl'),
      JSON.stringify({ test: this.testId, screen: this.screen, ...entry }) + '\n',
    )
  }

  attach(page: Page): void {
    this.page = page
    page.on('console', (message) => {
      const level = message.type()
      if (level === 'error' || level === 'warning') {
        this.write({ kind: 'console', level, text: message.text() })
      }
    })
    page.on('pageerror', (error) => {
      this.write({ kind: 'console', level: 'pageerror', text: error.message })
    })
    page.on('request', (request) => {
      this.requestStart.set(request, Date.now())
    })
    page.on('requestfinished', (request) => {
      void request.response().then((response) => {
        const started = this.requestStart.get(request) ?? Date.now()
        const status = response?.status() ?? 0
        this.write({
          kind: 'network',
          method: request.method(),
          url: request.url(),
          status,
          failure: null,
          expected: false,
          ms: Date.now() - started,
        })
      }).catch(() => undefined)
    })
    page.on('requestfailed', (request) => {
      const failure = request.failure()?.errorText ?? 'failed'
      this.write({
        kind: 'network',
        method: request.method(),
        url: request.url(),
        status: 0,
        failure,
        expected: /ERR_ABORTED|aborted/i.test(failure),
        ms: Date.now() - (this.requestStart.get(request) ?? Date.now()),
      })
    })
  }

  async recordShot(name: string): Promise<string | null> {
    if (this.page === null || this.page.isClosed()) return null
    const dir = path.join(runDir, 'screenshots')
    fs.mkdirSync(dir, { recursive: true })
    const file = path.join(dir, `${name}.png`)
    await this.page.screenshot({ path: file, fullPage: true })
    this.write({ kind: 'shot', step: name, path: file })
    return file
  }

  async recordBug(expected: string, saw: string): Promise<void> {
    const slug = this.testId
      .replace(/[^a-z0-9]+/gi, '-')
      .replace(/^-|-$/g, '')
      .toLowerCase()
      .slice(0, 80)
    const file = await this.recordShot(`bug-${slug}`)
    this.write({ kind: 'bug', expected, saw, shot: file ?? '' })
  }

  async checkpoint(page: Page, step: string): Promise<void> {
    const now = Date.now()
    const elapsed = now - this.lastCheckpoint
    this.lastCheckpoint = now
    if (elapsed > SLOW_STEP_MS && !this.expectFailure) {
      this.write({ kind: 'slow', step, ms: elapsed })
    }

    const snapshot = await page
      .waitForFunction(
        () => {
          const main = document.querySelector('main')
          return main !== null && (main as HTMLElement).innerText.trim() !== ''
        },
        undefined,
        { timeout: 5000 },
      )
      .then(() => true)
      .catch(() => false)
    const state = await page
      .evaluate(() => {
        const main = document.querySelector('main') as HTMLElement | null
        const selected = document.querySelector('[role="tab"][aria-selected="true"]')
        return {
          screen: selected?.textContent?.trim() ?? 'Concentration',
          text: document.body.innerText,
          mainText: main?.innerText.trim() ?? null,
        }
      })
      .catch(() => null)
    if (state === null) return
    this.screen = state.screen

    const problems: Problem[] = []
    if (!snapshot || state.mainText === null || state.mainText === '') {
      problems.push({ type: 'blank', detail: 'main area is missing or empty' })
    }
    if (ERROR_SCREEN.test(state.text)) {
      problems.push({ type: 'error-boundary', detail: 'error screen is visible' })
    }
    for (const match of state.text.matchAll(BAD_TOKENS)) {
      const at = match.index ?? 0
      const around = state.text.slice(
        Math.max(0, at - CONTEXT_CHARS),
        at + match[0].length + CONTEXT_CHARS,
      )
      problems.push({ type: 'bad-token', detail: `${match[0]}: …${around}…` })
    }
    if (problems.length > 0) {
      this.write({ kind: 'check', step, problems })
      this.problems.push(...problems.map((p) => ({ ...p, detail: `${step}: ${p.detail}` })))
    }

    const scanned = state.text.replace(ALLOWED_PHRASES, ' ')
    for (const match of scanned.matchAll(FORBIDDEN)) {
      const at = match.index ?? 0
      const context = scanned
        .slice(Math.max(0, at - CONTEXT_CHARS), at + match[0].length + CONTEXT_CHARS)
        .replace(/\s+/g, ' ')
      const key = `${state.screen}|${match[0].toLowerCase()}|${context}`
      if (this.seen.has(key)) continue
      this.seen.add(key)
      this.write({
        kind: 'forbidden',
        step,
        word: match[0].toLowerCase(),
        context,
      })
    }
  }
}

let active: Recorder | null = null

export async function checkpoint(page: Page, step: string): Promise<void> {
  await active?.checkpoint(page, step)
}

export async function recordShot(name: string): Promise<void> {
  await active?.recordShot(name)
}

export async function recordBug(expected: string, saw: string): Promise<void> {
  await active?.recordBug(expected, saw)
}

export const test = base.extend<{ demo: undefined }>({
  demo: [
    async ({ page }, use, testInfo) => {
      const recorder = new Recorder(
        testInfo.titlePath.join(' > '),
        testInfo.expectedStatus === 'failed',
      )
      recorder.attach(page)
      active = recorder
      await use(undefined)
      if (testInfo.status === testInfo.expectedStatus && !page.isClosed()) {
        await recorder.checkpoint(page, 'end of test')
      }
      active = null
      if (recorder.problems.length > 0 && !recorder.expectFailure) {
        throw new Error(
          'text and layout rules failed:\n' +
            recorder.problems.map((p) => `${p.type}: ${p.detail}`).join('\n'),
        )
      }
    },
    { auto: true },
  ],
})

export { expect } from '@playwright/test'
