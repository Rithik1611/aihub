// Sends one message in the DesignCenter chat panel and returns the finished answer, the pictures that loaded and any approval card.
//   QA_MSG="..." node <browser-automation>/browser.mjs --session qa --script ui_chat.mjs [--screenshot out.png]
//   QA_ACTION=decline  -> if an approval card appears, click its Decline/Reject button (never Approve).
export default async function run(page, ui) {
  const msg = process.env.QA_MSG
  const action = process.env.QA_ACTION ?? ''
  const MAX_MS = Number(process.env.QA_MAX_MS ?? 240000)
  let snap = await ui.snapshot()
  if (!/textbox "Chat message input"/.test(snap)) {
    const open = snap.match(/@(e\d+) button "Open chat"/)?.[1]
    if (open) { await ui.click('@' + open); await page.waitForTimeout(1500); snap = await ui.snapshot() }
  }
  const newChat = snap.match(/@(e\d+) button "Start a new chat"/)?.[1]
  if (newChat && process.env.QA_NEW_CHAT === '1') { await ui.click('@' + newChat); await page.waitForTimeout(800); snap = await ui.snapshot() }
  const box = snap.match(/@(e\d+) textbox "Chat message input"/)?.[1]
  if (!box) return { error: 'chat input not found', snap: snap.slice(0, 500) }
  const before = (await page.evaluate(() => document.body.innerText)).length
  await ui.fill('@' + box, msg)
  await page.keyboard.press('Enter')
  const started = Date.now()
  // finished = the page text stops changing (ignoring the live "Logged in since" clock) for ~6 s, after something new appeared
  let last = '', stable = 0, card = null
  while (Date.now() - started < MAX_MS && (stable < 4 || (await page.evaluate(() => document.body.innerText)).length === before)) {
    await page.waitForTimeout(1500)
    const t = (await page.evaluate(() => document.body.innerText)).replace(/Logged in since: [0-9:]+/, '')
    stable = t === last ? stable + 1 : 0
    last = t
    const s = await ui.snapshot()
    card = s.match(/@(e\d+) button "(Decline|Reject|Cancel changes|Deny)"/i) || card
    if (card && !action) break // an approval card is waiting: report it, do not touch it
  }
  let declined = false
  if (card && action === 'decline') { await ui.click('@' + card[1]); declined = true; await page.waitForTimeout(2500) }
  const text = last.replace(/\s+/g, ' ')
  const at = text.lastIndexOf(msg.slice(0, 40))
  const images = await page.evaluate(() => [...document.querySelectorAll('img')]
    .map(i => ({ src: i.currentSrc.slice(0, 80), shown: i.complete && i.naturalWidth > 0, alt: i.alt }))
    .filter(i => /gstatic|drawingurl|chat\/images/.test(i.src)))
  return { seconds: Math.round((Date.now() - started) / 1000), approvalCard: card ? card[2] : null, declined, answer: text.slice(at + msg.length).slice(0, Number(process.env.QA_CHARS ?? 700)), images }
}
