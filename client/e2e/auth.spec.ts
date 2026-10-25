import { expect, test } from './fixtures.ts'
import { API_URL, LOGGED_OUT, newCredentials } from './auth-state.ts'
import { shot } from './helpers.ts'
import type { Page } from '@playwright/test'

test.use({ storageState: LOGGED_OUT })

async function signUpInBrowser(page: Page, email: string, password: string) {
  await page.goto('/signup.html')
  await page.getByLabel('Email').fill(email)
  await page.getByLabel('Password', { exact: true }).fill(password)
  await page.getByLabel('Confirm password').fill(password)
  await page.getByRole('button', { name: 'Create account' }).click()
}

async function expectAppLoaded(page: Page) {
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Log out' })).toBeVisible()
}

test('sign up goes to the app', async ({ page }) => {
  const { email, password } = newCredentials()
  await signUpInBrowser(page, email, password)
  await expectAppLoaded(page)
  await expect(page.locator('.account-email')).toHaveText(email)
  await shot(page, 'auth-1-signed-up')
})

test('log out returns to the login page and locks the app again', async ({
  page,
}) => {
  const { email, password } = newCredentials()
  await signUpInBrowser(page, email, password)
  await expectAppLoaded(page)
  await page.getByRole('button', { name: 'Log out' }).click()
  await expect(page).toHaveURL(/\/login\.html$/)
  await shot(page, 'auth-2-logged-out')
  await page.goto('/')
  await expect(page).toHaveURL(/\/login\.html$/)
})

test('log in goes to the app', async ({ page, playwright }) => {
  const { email, password } = newCredentials()
  const api = await playwright.request.newContext({ baseURL: API_URL })
  const created = await api.post('/auth/signup', { data: { email, password } })
  expect(created.status()).toBe(201)
  await api.dispose()

  await page.goto('/login.html')
  await page.getByLabel('Email').fill(email.toUpperCase())
  await page.getByLabel('Password').fill(password)
  await page.getByRole('button', { name: 'Log in' }).click()
  await expectAppLoaded(page)
  await shot(page, 'auth-3-logged-in')
})

test('a logged-out visit to / lands on the login page', async ({ page }) => {
  await page.goto('/')
  await expect(page).toHaveURL(/\/login\.html$/)
  await expect(page.getByRole('heading', { name: 'Log in' })).toBeVisible()
  await shot(page, 'auth-4-redirected')
})

test('a wrong password shows the generic error and stays on login', async ({
  page,
}) => {
  await page.goto('/login.html')
  await page.getByLabel('Email').fill('nobody@example.com')
  await page.getByLabel('Password').fill('not the password')
  await page.getByRole('button', { name: 'Log in' }).click()
  await expect(page.getByText('Incorrect email or password.')).toBeVisible()
  await expect(page).toHaveURL(/\/login\.html$/)
  await expect(page.getByRole('button', { name: 'Log in' })).toBeEnabled()
})

test('sign-up shows inline errors before anything is sent', async ({ page }) => {
  let sent = false
  page.on('request', (request) => {
    if (request.url().includes('/auth/signup')) sent = true
  })
  await page.goto('/signup.html')
  await page.getByLabel('Email').fill('not-an-email')
  await page.getByLabel('Password', { exact: true }).fill('short')
  await page.getByLabel('Confirm password').fill('different')
  await page.getByRole('button', { name: 'Create account' }).click()
  await expect(page.getByText('Enter a valid email address.')).toBeVisible()
  await expect(page.getByText('Use at least 10 characters.')).toBeVisible()
  await expect(page.getByText('Passwords do not match.')).toBeVisible()
  await shot(page, 'auth-5-signup-errors')
  expect(sent).toBe(false)
})

test('login and sign-up link to each other', async ({ page }) => {
  await page.goto('/login.html')
  await page.getByRole('link', { name: 'Create an account' }).click()
  await expect(page).toHaveURL(/\/signup\.html$/)
  await page.getByRole('link', { name: 'Log in' }).click()
  await expect(page).toHaveURL(/\/login\.html$/)
})

test('both pages carry the educational-use disclaimer', async ({ page }) => {
  for (const url of ['/login.html', '/signup.html']) {
    await page.goto(url)
    await expect(
      page.getByText('Educational and demonstrative use only. Not a medical device.'),
    ).toBeVisible()
  }
})
