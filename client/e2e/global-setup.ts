import fs from 'node:fs'
import path from 'node:path'
import { request } from '@playwright/test'
import { API_URL, AUTH_FILE, newCredentials } from './auth-state.ts'

export default async function globalSetup(): Promise<void> {
  const api = await request.newContext({ baseURL: API_URL })
  const response = await api.post('/auth/signup', { data: newCredentials() })
  if (response.status() !== 201) {
    throw new Error(`e2e sign-up failed with status ${response.status()}`)
  }
  fs.mkdirSync(path.dirname(AUTH_FILE), { recursive: true })
  await api.storageState({ path: AUTH_FILE })
  await api.dispose()
}
