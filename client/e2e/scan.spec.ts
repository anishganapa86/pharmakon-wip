import zlib from 'node:zlib'
import { expect, test } from './fixtures.ts'
import { shot } from './helpers.ts'
import type { Page } from '@playwright/test'

const API = 'http://localhost:8000'

function crc32(buffer: Buffer): number {
  let crc = 0xffffffff
  for (const byte of buffer) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

function chunk(kind: string, payload: Buffer): Buffer {
  const body = Buffer.concat([Buffer.from(kind), payload])
  const length = Buffer.alloc(4)
  length.writeUInt32BE(payload.length)
  const check = Buffer.alloc(4)
  check.writeUInt32BE(crc32(body))
  return Buffer.concat([length, body, check])
}

function makePng(width: number, height: number): Buffer {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(width, 0)
  header.writeUInt32BE(height, 4)
  header[8] = 8
  header[9] = 2
  const row = Buffer.concat([Buffer.from([0]), Buffer.alloc(width * 3, 0x90)])
  const raw = Buffer.concat(Array.from({ length: height }, () => row))
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', zlib.deflateSync(raw)),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

function jpegSize(data: Buffer): { width: number; height: number } {
  let at = 2
  while (at < data.length) {
    const marker = data[at + 1]
    const length = data.readUInt16BE(at + 2)
    if (marker >= 0xc0 && marker <= 0xc3) {
      return { height: data.readUInt16BE(at + 5), width: data.readUInt16BE(at + 7) }
    }
    at += 2 + length
  }
  throw new Error('no frame header')
}

const LABEL = {
  product_name: 'Barrier Cream',
  brand: 'Example Brand',
  product_type: 'topical',
  ingredients: [
    { name: 'Water', strength: null, unit: null, role: 'inactive' },
    { name: 'Acetaminophen', strength: '500', unit: 'mg', role: 'active' },
    { name: 'Zorbium', strength: '2', unit: '%', role: 'active' },
  ],
  label_text_quality: 'clear',
  notes: null,
}

const WARNING =
  'Label reading can be wrong. Check each ingredient against the package before saving.'
const MANUAL_MESSAGE = "Couldn't read the label. Try again or enter it manually."

async function mockScan(page: Page, body: unknown, status = 200) {
  await page.route('**/users/*/scan', (route) =>
    route.fulfill({
      status,
      contentType: 'application/json',
      body: JSON.stringify(body),
    }),
  )
}

async function chooseFile(page: Page, png: Buffer) {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
  await page.getByRole('button', { name: 'Scan', exact: true }).click()
  await expect(page.getByRole('heading', { name: 'Scan a product' })).toBeVisible()
  await page
    .locator('input[type="file"]:not([capture])')
    .setInputFiles({ name: 'label.png', mimeType: 'image/png', buffer: png })
}

async function myProducts(page: Page): Promise<{ name: string }[]> {
  const me = await (await page.request.get(`${API}/auth/me`)).json()
  const res = await page.request.get(`${API}/users/${me.id}/my-products`)
  return (await res.json()) as { name: string }[]
}

test('the camera input asks for the rear camera and a second input picks a photo', async ({
  page,
}) => {
  await page.goto('/')
  await page.getByRole('button', { name: 'Scan', exact: true }).click()
  await expect(
    page.locator('input[type="file"][accept="image/*"][capture="environment"]'),
  ).toHaveCount(1)
  await expect(
    page.locator('input[type="file"][accept="image/*"]:not([capture])'),
  ).toHaveCount(1)
  await expect(
    page.getByText("Demo only: don't enter real medical or personal information."),
  ).toBeVisible()
  await shot(page, 'scan-1-pick')
})

test('the photo is downscaled to a 1600px JPEG without EXIF before it is sent', async ({
  page,
}) => {
  let sent: Buffer | null = null
  let type = ''
  await page.route('**/users/*/scan', async (route) => {
    sent = route.request().postDataBuffer()
    type = route.request().headers()['content-type'] ?? ''
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(LABEL),
    })
  })
  await chooseFile(page, makePng(2400, 1800))
  await expect(page.getByRole('heading', { name: 'Check what we read' })).toBeVisible()
  expect(type).toBe('image/jpeg')
  expect(sent).not.toBeNull()
  const data = sent as unknown as Buffer
  expect([...data.subarray(0, 3)]).toEqual([0xff, 0xd8, 0xff])
  expect(data.includes(Buffer.from('Exif'))).toBe(false)
  const { width, height } = jpegSize(data)
  expect(Math.max(width, height)).toBe(1600)
})

test('review shows the warning and statuses, and saves nothing until Add is pressed', async ({
  page,
}) => {
  await mockScan(page, LABEL)
  const before = (await myProducts(page)).length
  await chooseFile(page, makePng(64, 48))
  await expect(page.getByText(WARNING)).toBeVisible()
  const rows = page.locator('.scan-row')
  await expect(rows).toHaveCount(3)
  await expect(rows.filter({ hasText: 'Water' })).toContainText('Not tracked')
  await expect(rows.filter({ hasText: 'Acetaminophen' })).toContainText('Matched')
  await expect(rows.filter({ hasText: 'Acetaminophen' })).toContainText('500 mg as printed')
  await expect(rows.filter({ hasText: 'Zorbium' })).toContainText('Enter manually')
  await shot(page, 'scan-2-review')
  expect((await myProducts(page)).length).toBe(before)

  await page.getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByRole('heading', { name: 'Pharmakon' })).toBeVisible()
  expect((await myProducts(page)).length).toBe(before)
})

test('Enter manually opens the ingredient prefilled and a fix is used', async ({ page }) => {
  await mockScan(page, LABEL)
  await chooseFile(page, makePng(64, 48))
  const row = page.locator('.scan-row').filter({ hasText: 'Zorbium' })
  await row.getByRole('button', { name: 'Enter manually' }).click()
  const field = row.getByLabel('Ingredient name')
  await expect(field).toHaveValue('Zorbium')
  await field.fill('Retinol')
  await field.blur()
  await expect(page.locator('textarea')).toHaveValue(/Retinol/)
})

test('Add to my products saves exactly the confirmed entry', async ({ page }) => {
  await mockScan(page, LABEL)
  const before = (await myProducts(page)).length
  await chooseFile(page, makePng(64, 48))
  await page.getByRole('button', { name: 'Add to my products' }).click()
  await expect(page.getByRole('heading', { name: 'Added' })).toBeVisible()
  const after = await myProducts(page)
  expect(after.length).toBe(before + 1)
  expect(after.some((p) => p.name === 'Barrier Cream')).toBe(true)
})

test('an unreadable label offers manual entry and cannot be saved empty', async ({
  page,
}) => {
  await mockScan(page, { ...LABEL, label_text_quality: 'unreadable', ingredients: [] })
  await chooseFile(page, makePng(64, 48))
  await expect(page.getByText("We couldn't read this label.")).toBeVisible()
  await expect(page.getByRole('button', { name: 'Add to my products' })).toBeDisabled()
})

test('a failed read shows the manual-entry message and stays on the picker', async ({
  page,
}) => {
  await mockScan(page, { error: { code: 'scan_unreadable', message: MANUAL_MESSAGE } }, 502)
  await chooseFile(page, makePng(64, 48))
  await expect(page.getByRole('alert')).toContainText(MANUAL_MESSAGE)
  await expect(page.getByRole('heading', { name: 'Scan a product' })).toBeVisible()
})
