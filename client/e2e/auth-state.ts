import crypto from 'node:crypto'
import path from 'node:path'

export const API_URL = process.env.DEMO_API_URL ?? 'http://localhost:8000'

const runDir =
  process.env.DEMO_RUN_DIR ??
  path.resolve(import.meta.dirname, '../../reports/runs/local')

export const AUTH_FILE = path.join(runDir, '.auth', 'user.json')

export const LOGGED_OUT = { cookies: [], origins: [] }

export interface Credentials {
  email: string
  password: string
}

export function newCredentials(): Credentials {
  return {
    email: `e2e-${crypto.randomBytes(6).toString('hex')}@example.com`,
    password: crypto.randomBytes(18).toString('base64url'),
  }
}
