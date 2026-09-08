const fs = require('fs');
const path = require('path');
const ejs = require('ejs');
const postmark = require('postmark');

const VIEWS = path.join(__dirname, '..', 'views', 'email');
const TMP_DIR = path.join(__dirname, '..', 'tmp', 'mail');
const SUBJECT_PREFIX = '[Photo Archive] ';

// In-memory sink. Every send appends here regardless of mode; tests read
// this, and it's cheap enough that leaving it always-on is fine.
const outbox = [];
function clearOutbox() { outbox.length = 0; }

let postmarkClient = null;
function client() {
  if (!process.env.POSTMARK_API_KEY) return null;
  if (!postmarkClient) {
    postmarkClient = new postmark.ServerClient(process.env.POSTMARK_API_KEY);
  }
  return postmarkClient;
}

async function renderTemplate(name, vars) {
  const textPath = path.join(VIEWS, `${name}.text.ejs`);
  const htmlPath = path.join(VIEWS, `${name}.html.ejs`);
  const [text, html] = await Promise.all([
    ejs.renderFile(textPath, vars, { async: true }),
    ejs.renderFile(htmlPath, vars, { async: true }),
  ]);
  return { text, html };
}

function ensureTmpDir() {
  fs.mkdirSync(TMP_DIR, { recursive: true });
}

function writeDevFile({ to, subject, text }) {
  ensureTmpDir();
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const file = path.join(TMP_DIR, `${stamp}.txt`);
  const body = [
    `To: ${to}`,
    `From: ${process.env.POSTMARK_FROM_EMAIL || '(unset)'}`,
    `Subject: ${subject}`,
    '',
    text,
  ].join('\n');
  fs.writeFileSync(file, body, 'utf8');
  return file;
}

// send({ to, subject, template, vars }). Subject is prefixed with [Photo Archive].
async function send({ to, subject, template, vars }) {
  const { text, html } = await renderTemplate(template, vars);
  const fullSubject = SUBJECT_PREFIX + subject;
  const message = { to, subject: fullSubject, template, vars, text, html, sentAt: new Date() };
  outbox.push(message);

  const pm = client();
  if (pm) {
    await pm.sendEmail({
      From: process.env.POSTMARK_FROM_EMAIL,
      To: to,
      Subject: fullSubject,
      TextBody: text,
      HtmlBody: html,
      MessageStream: 'outbound',
    });
    return message;
  }

  // No Postmark key. In dev, log + write file. In tests, stay quiet.
  if (process.env.NODE_ENV !== 'test') {
    console.log(`[mail] To: ${to}\n[mail] Subject: ${fullSubject}\n[mail] ${text.split('\n').join('\n[mail] ')}`);
    const file = writeDevFile({ to, subject: fullSubject, text });
    console.log(`[mail] wrote ${file}`);
  }
  return message;
}

module.exports = { send, outbox, clearOutbox };
