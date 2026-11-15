// Declines the pending approval card in the DesignCenter chat: selects the "Decline" option, VERIFIES it is the only selected option, then presses Send.
// Never selects Approve; it stops with an error if anything other than Decline ends up selected.
export default async function run(page, ui) {
  await page.getByText('Decline', { exact: true }).last().click()
  await page.waitForTimeout(600)
  const checked = await page.evaluate(() => [...document.querySelectorAll('input[type=radio]')]
    .filter(i => i.checked).map(i => (i.closest('label')?.innerText || i.getAttribute('aria-label') || i.value || '').trim()))
  if (checked.length !== 1 || !/^decline$/i.test(checked[0])) return { error: 'NOT sending: the selected option is not exactly Decline', checked }
  const snap = await ui.snapshot()
  const line = snap.split('\n').find(l => /button "Send"(\s|$)/.test(l) && !/Send message/.test(l))
  if (!line || /\[disabled\]/.test(line)) return { error: 'Send button not available', line }
  await ui.click('@' + line.match(/@(e\d+)/)[1])
  await page.waitForTimeout(6000)
  const text = (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, ' ')
  const at = text.lastIndexOf('Approve 1 change')
  return { selected: checked[0], after: text.slice(Math.max(0, at), at + 700) }
}
