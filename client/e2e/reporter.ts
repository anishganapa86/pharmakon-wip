import fs from 'node:fs'
import path from 'node:path'
import type {
  FullResult,
  Reporter,
  TestCase,
  TestResult,
} from '@playwright/test/reporter'

interface Row {
  id: string
  project: string
  title: string
  status: string
  expected: string
  durationMs: number
  error: string | null
  annotations: { type: string; description: string }[]
}

function stripAnsi(text: string): string {
  return text.split(String.fromCharCode(27)).map((part, i) => (i === 0 ? part : part.replace(/^\[[0-9;]*m/, ''))).join('')
}

export default class DemoReporter implements Reporter {
  private rows: Row[] = []
  private runDir: string

  constructor(options: { runDir: string }) {
    this.runDir = options.runDir
  }

  onTestEnd(test: TestCase, result: TestResult) {
    this.rows.push({
      id: test.titlePath().slice(2).join(' > '),
      project: test.parent.project()?.name ?? '',
      title: test.title,
      status: result.status,
      expected: test.expectedStatus,
      durationMs: result.duration,
      error: result.error?.message
        ? stripAnsi(result.error.message).slice(0, 4000)
        : null,
      annotations: test.annotations.map((a) => ({
        type: a.type,
        description: a.description ?? '',
      })),
    })
  }

  onEnd(result: FullResult) {
    fs.mkdirSync(this.runDir, { recursive: true })
    const file = path.join(this.runDir, 'pw_results.json')
    const existing: Row[] = fs.existsSync(file)
      ? (JSON.parse(fs.readFileSync(file, 'utf8')) as { tests: Row[] }).tests
      : []
    const merged = [
      ...existing.filter(
        (e) => !this.rows.some((r) => r.id === e.id && r.project === e.project),
      ),
      ...this.rows,
    ]
    fs.writeFileSync(
      file,
      JSON.stringify({ status: result.status, tests: merged }, null, 2),
    )
  }
}
