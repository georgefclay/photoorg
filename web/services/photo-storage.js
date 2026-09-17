// Filesystem helpers for the web-side working/thumbs/backs/faces/uploads
// directories under PHOTO_DIR. Sync endpoints and image-serving route
// both go through here.

const fs = require('fs/promises');
const fsSync = require('fs');
const path = require('path');
const sharp = require('sharp');

const SUBDIRS = ['working', 'thumbs', 'backs', 'faces', 'uploads'];

function root() {
  const dir = process.env.PHOTO_DIR;
  if (!dir) throw new Error('PHOTO_DIR not configured');
  return dir;
}

async function ensureDirs() {
  const r = root();
  await fs.mkdir(r, { recursive: true });
  for (const d of SUBDIRS) await fs.mkdir(path.join(r, d), { recursive: true });
}

function paddedId(n) { return String(n).padStart(8, '0'); }

function workingBasename(photoId, sha256, mime) {
  const ext = extForMime(mime) || 'jpg';
  const sha8 = String(sha256).slice(0, 8);
  return `${paddedId(photoId)}_${sha8}.${ext}`;
}

function backBasename(backId, sha256, mime) {
  const ext = extForMime(mime) || 'jpg';
  const sha8 = String(sha256 || '').slice(0, 8) || 'back';
  return `back_${paddedId(backId)}_${sha8}.${ext}`;
}

function extForMime(mime) {
  const m = (mime || '').toLowerCase();
  if (m === 'image/jpeg') return 'jpg';
  if (m === 'image/png')  return 'png';
  if (m === 'image/tiff') return 'tif';
  if (m === 'image/heic') return 'heic';
  if (m === 'image/webp') return 'webp';
  return null;
}

async function writeWorking(photoId, sha256, mime, buffer) {
  const base = workingBasename(photoId, sha256, mime);
  const dest = path.join(root(), 'working', base);
  await fs.writeFile(dest, buffer);
  return { basename: base, absolute: dest };
}

async function writeBack(backId, sha256, mime, buffer) {
  const base = backBasename(backId, sha256, mime);
  const dest = path.join(root(), 'backs', base);
  await fs.writeFile(dest, buffer);
  return { basename: base, absolute: dest };
}

async function writeFaceCrop(faceId, buffer) {
  const dest = path.join(root(), 'faces', `${faceId}.jpg`);
  await fs.writeFile(dest, buffer);
  return { basename: `${faceId}.jpg`, absolute: dest };
}

async function writeUploadFile(contributionId, fileId, ext, buffer) {
  const dir = path.join(root(), 'uploads', String(contributionId));
  await fs.mkdir(dir, { recursive: true });
  const base = `${fileId}.${ext}`;
  const dest = path.join(dir, base);
  await fs.writeFile(dest, buffer);
  return { basename: base, absolute: dest, relative: path.join('uploads', String(contributionId), base) };
}

async function generateThumbFromWorking(photoId, workingBuffer) {
  const dest = path.join(root(), 'thumbs', `${paddedId(photoId)}.jpg`);
  await sharp(workingBuffer, { failOn: 'none' })
    .rotate() // respect EXIF orientation
    .resize({ width: 320, withoutEnlargement: true })
    .jpeg({ quality: 85 })
    .toFile(dest);
  return { absolute: dest };
}

// Where a back's JPEG lives: backs/back_<id:08d>_<sha8>.jpg. Derived from
// id + sha256 so a later metadata push (which carries the desktop's own
// working_path) can't point the web at a file that doesn't exist.
function backPath(backId, sha256) {
  return path.join(root(), 'backs', backBasename(backId, sha256, 'image/jpeg'));
}

function fileExists(absPath) {
  try { return fsSync.statSync(absPath).isFile(); } catch { return false; }
}

module.exports = {
  root,
  ensureDirs,
  paddedId,
  extForMime,
  workingBasename,
  backBasename,
  writeWorking,
  writeBack,
  writeFaceCrop,
  writeUploadFile,
  generateThumbFromWorking,
  backPath,
  fileExists,
};
