import { connect } from 'puppeteer-real-browser'
import type { Config } from './config'

const B_TIMEOUT = 120_000
const P_TIMEOUT = 300_000
const NAV_TIMEOUT = 30_000
const MIN_LEN = 10
const MAX_CONCURRENT_BROWSERS = 12

function cleanHtml(str: string): string {
  return str.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#039;')
}

function tpl(sitekey: string): string {
  const s = cleanHtml(sitekey)
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"></head><body><div class="cf"></div><script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=_ol" defer></script><script>window._ol=()=>{turnstile.render('.cf',{sitekey:'${s}',callback:t=>{const e=document.createElement('input');e.type='hidden';e.name='cf-response';e.value=t;document.body.appendChild(e)}})}</script></body></html>`
}

export class Solver {
  #c: Config
  #active = 0
  #queue: (() => void)[] = []

  constructor(config: Config) { this.#c = config }

  get ready(): boolean { return true }
  async init(): Promise<void> {}
  async stop(): Promise<void> {}

  async #acquire(): Promise<void> {
    if (this.#active < MAX_CONCURRENT_BROWSERS) { this.#active++; return }
    return new Promise<void>(r => this.#queue.push(r))
  }

  #release(): void {
    this.#active--
    const next = this.#queue.shift()
    if (next) { this.#active++; next() }
  }

  async solve(url: string, sitekey: string | null, proxy: string | null): Promise<{ ok: boolean; token?: string; error?: string; time: number }> {
    await this.#acquire()
    const t0 = Date.now()
    const diff = () => +((Date.now() - t0) / 1000).toFixed(2)
    
    let b: any
    let p: any
    try {
      const args = [
        '--no-sandbox', 
        '--disable-setuid-sandbox', 
        '--disable-dev-shm-usage', 
        '--disable-gpu',
        '--disable-renderer-backgrounding',
        '--disable-background-timer-throttling'
      ]
      if (proxy) args.push(`--proxy-server=${proxy}`)

      const res = await connect({
        headless: this.#c.headless,
        turnstile: true,
        connectOption: { defaultViewport: { width: 1280, height: 720 }, timeout: B_TIMEOUT, protocolTimeout: P_TIMEOUT, args },
        disableXvfb: true,
      })
      b = res.browser
      p = await b.newPage()
      p.setDefaultTimeout(P_TIMEOUT)
      p.setDefaultNavigationTimeout(NAV_TIMEOUT)

      if (sitekey) {
        const html = tpl(sitekey)
        const base = url.endsWith('/') ? url : url + '/'
        await p.setRequestInterception(true)
        p.on('request', async (req: any) => {
          try {
            const type = req.resourceType()
            if (['image', 'media', 'font'].includes(type)) {
              await req.abort()
            } else if ((req.url() === url || req.url() === base) && type === 'document') {
              await req.respond({ status: 200, contentType: 'text/html', body: html })
            } else {
              await req.continue()
            }
          } catch {
            try { await req.continue() } catch {}
          }
        })
      }

      await p.goto(url, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })
      await p.waitForSelector('[name="cf-response"]', { timeout: this.#c.timeout })
      const token = await p.evaluate(() => document.querySelector<HTMLInputElement>('[name="cf-response"]')?.value ?? null)
      
      if (!token || token.length < MIN_LEN) throw new Error('Invalid token')
      return { ok: true, token, time: diff() }
    } catch (err: any) {
      return { ok: false, error: err.message, time: diff() }
    } finally {
      if (p) { try { await p.close() } catch {} }
      if (b) { try { await b.close() } catch {} }
      this.#release()
    }
  }
}
