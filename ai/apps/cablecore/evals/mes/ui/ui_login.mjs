// Signs in to DesignCenter in a browser-automation session. Credentials come from aihub-main/.env.eval (EVAL_USER / EVAL_PASS), never from arguments.
//   node <browser-automation>/browser.mjs http://localhost:3000/ --session qa --script ui_login.mjs
import { readFileSync } from 'node:fs'

const ENV_FILE = process.env.EVAL_ENV_FILE ?? 'C:/WorkSpace/DesignCenter/Repository/aihub-main/aihub-main/.env.eval'

export default async function run(page, ui) {
  const env = Object.fromEntries(
    readFileSync(ENV_FILE, 'utf8').split(/\r?\n/).filter(l => l.includes('=')).map(l => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]))
  const snap = await ui.snapshot()
  const ref = re => snap.match(re)?.[1]
  const user = ref(/@(e\d+) textbox "Username/), pass = ref(/@(e\d+) textbox "Password/), btn = ref(/@(e\d+) button "Login"/)
  if (!user || !pass || !btn) return { error: 'login form not found (already signed in?)', snapshot: snap.slice(0, 300) }
  await ui.fill('@' + user, env.EVAL_USER)
  await ui.fill('@' + pass, env.EVAL_PASS)
  await ui.click('@' + btn)
  await page.waitForFunction(() => /Good (morning|afternoon|evening)/.test(document.body.innerText) || /invalid|incorrect|locked|failed/i.test(document.body.innerText), null, { timeout: 40000 }).catch(() => {})
  const text = (await page.evaluate(() => document.body.innerText)).replace(/\s+/g, ' ')
  return { signedIn: /Good (morning|afternoon|evening)/.test(text), url: page.url() }
}
