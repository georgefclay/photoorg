// Fast 64-bit perceptual hash (dHash) — 8×9 grayscale, take horizontal
// gradient signs, pack into a 16-hex string. Same shape as the existing
// `photos.phash` / `photo_backs.sha256` textual columns, so pHash-near
// dedupe on `photos.phash` is a simple Hamming distance comparison.
//
// This is deliberately small — the desktop already uses a
// 256-bit multi-band index for its dedupe scan; on the web we only
// need to spot exact-or-very-near duplicates coming in over upload. A
// 64-bit dHash with distance ≤ 10 is a reasonable "same photo" gate.
const sharp = require('sharp');

async function dhash64Hex(buffer) {
  // 9×8 grayscale — 9 across so 8 horizontal comparisons per row.
  const raw = await sharp(buffer, { failOn: 'none' })
    .rotate()
    .resize(9, 8, { fit: 'fill' })
    .greyscale()
    .raw()
    .toBuffer();
  let bits = 0n;
  let idx = 0;
  for (let y = 0; y < 8; y++) {
    for (let x = 0; x < 8; x++) {
      const left  = raw[y * 9 + x];
      const right = raw[y * 9 + x + 1];
      const bit = left > right ? 1n : 0n;
      bits = (bits << 1n) | bit;
      idx += 1;
    }
  }
  return bits.toString(16).padStart(16, '0');
}

function hamming64Hex(a, b) {
  if (!a || !b) return null;
  const bigA = BigInt('0x' + a);
  const bigB = BigInt('0x' + b);
  let x = bigA ^ bigB;
  let n = 0;
  while (x) { x &= (x - 1n); n += 1; }
  return n;
}

module.exports = { dhash64Hex, hamming64Hex };
