import { connect } from 'puppeteer-real-browser'
import type { Config } from './config'

const BROWSER_TIMEOUT = 120_000
const PROTOCOL_TIMEOUT = 300_000
const PAGE_TIMEOUT = 30_000
const NAV_TIMEOUT = 30_000
const MIN_TOKEN = 10

function escapeHtml(str: string): string {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;')
}

function tpl(sitekey: string): string {
  const s = escapeHtml(sitekey)
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><title>T</title></head><body><div class="cf"></div><script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_onload" defer></script><script>window._onload=()=>{turnstile.render('.cf',{sitekey:'${s}',callback:t=>{const e=document.createElement('input');e.type='hidden';e.name='cf-response';e.value=t;document.body.appendChild(e)}})}</script></body></html>`
}

export class Solver {
  #browser: Awaited<ReturnType<typeof connect>>['browser'] | null = null
  #config: Config

  constructor(config: Config) {
    this.#config = config
  }

  get ready(): boolean { return !!this.#browser?.connected }

  async init(): Promise<void> {
    if (this.ready) return
    const { width = 1280, height = 720 } = this.#config.viewport ?? {}
    const args = [`--window-size=${width},${height}`, '--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']

    const { browser } = await connect({
      headless: this.#config.headless,
      turnstile: true,
      connectOption: { defaultViewport: { width, height }, timeout: BROWSER_TIMEOUT, protocolTimeout: PROTOCOL_TIMEOUT, args },
      disableXvfb: false,
    })
    this.#browser = browser
  }

  async stop(): Promise<void> {
    if (this.#browser) { try { await this.#browser.close() } catch {} this.#browser = null }
  }

  async solve(url: string, sitekey: string | null): Promise<{ ok: boolean; token?: string; error?: string; time: number }> {
    const t0 = Date.now()
    const elapsed = () => +((Date.now() - t0) / 1000).toFixed(3)
    if (!this.#browser) throw new Error('Browser not initialized')

    let page: Awaited<ReturnType<typeof this.#browser.newPage>> | undefined
    try {
      page = await this.#browser.newPage()
      page.setDefaultTimeout(PAGE_TIMEOUT)
      page.setDefaultNavigationTimeout(NAV_TIMEOUT)

      if (sitekey) {
        const html = tpl(sitekey)
        const base = url.endsWith('/') ? url : url + '/'
        await page.setRequestInterception(true)
        page.on('request', async req => {
          try {
            if ((req.url() === url || req.url() === base) && req.resourceType() === 'document') {
              await req.respond({ status: 200, contentType: 'text/html', body: html })
            } else await req.continue()
          } catch {}
        })
      } else {
        await page.evaluateOnNewDocument(() => {
          void (async () => {
            let token: string | undefined
            while (!token) { try { token = window.turnstile?.getResponse() } catch {} await new Promise(r => setTimeout(r, 500)) }
            const el = document.createElement('input'); el.type = 'hidden'; el.name = 'cf-response'; el.value = token; document.body.appendChild(el)
          })()
        })
      }

      await page.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })
      await page.waitForSelector('[name="cf-response"]', { timeout: this.#config.timeout })
      const token = await page.evaluate(() => document.querySelector<HTMLInputElement>('[name="cf-response"]')?.value ?? null)
      if (!token || token.length < MIN_TOKEN) throw new Error('Invalid token')
      return { ok: true, token, time: elapsed() }
    } catch (err) {
      return { ok: false, error: (err as Error).message, time: elapsed() }
    } finally {
      if (page) { try { await page.close() } catch {} }
    }
  }
}
