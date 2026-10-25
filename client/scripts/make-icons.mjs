import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'

const outDir = path.resolve(import.meta.dirname, '../public')
const TEAL = [11, 122, 107]
const WHITE = [255, 255, 255]

function crc32(buffer) {
  let crc = 0xffffffff
  for (const byte of buffer) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) {
      crc = crc & 1 ? (crc >>> 1) ^ 0xedb88320 : crc >>> 1
    }
  }
  return (crc ^ 0xffffffff) >>> 0
}

function chunk(kind, payload) {
  const body = Buffer.concat([Buffer.from(kind), payload])
  const length = Buffer.alloc(4)
  length.writeUInt32BE(payload.length)
  const check = Buffer.alloc(4)
  check.writeUInt32BE(crc32(body))
  return Buffer.concat([length, body, check])
}

function coverage(distance, radius) {
  return Math.min(1, Math.max(0, radius - distance + 0.5))
}

function render(size, scale) {
  const px = Buffer.alloc(size * size * 3)
  const mid = size / 2
  const unit = size * scale
  const ringOuter = unit * 0.3
  const ringInner = unit * 0.19
  const dotRadius = unit * 0.075
  const dotX = mid + unit * 0.3
  const dotY = mid - unit * 0.3
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const dx = x + 0.5 - mid
      const dy = y + 0.5 - mid
      const d = Math.hypot(dx, dy)
      const ring = Math.min(coverage(d, ringOuter), 1 - coverage(d, ringInner))
      const dot = coverage(Math.hypot(x + 0.5 - dotX, y + 0.5 - dotY), dotRadius)
      const white = Math.max(ring, dot)
      const at = (y * size + x) * 3
      for (let c = 0; c < 3; c += 1) {
        px[at + c] = Math.round(TEAL[c] * (1 - white) + WHITE[c] * white)
      }
    }
  }
  return px
}

function encode(size, pixels) {
  const header = Buffer.alloc(13)
  header.writeUInt32BE(size, 0)
  header.writeUInt32BE(size, 4)
  header[8] = 8
  header[9] = 2
  const rows = []
  for (let y = 0; y < size; y += 1) {
    rows.push(Buffer.from([0]), pixels.subarray(y * size * 3, (y + 1) * size * 3))
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', zlib.deflateSync(Buffer.concat(rows), { level: 9 })),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

const files = [
  ['icon-192.png', 192, 1],
  ['icon-512.png', 512, 1],
  ['icon-maskable-192.png', 192, 0.8],
  ['icon-maskable-512.png', 512, 0.8],
  ['apple-touch-icon.png', 180, 1],
]

for (const [name, size, scale] of files) {
  fs.writeFileSync(path.join(outDir, name), encode(size, render(size, scale)))
}
