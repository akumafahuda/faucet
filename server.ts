import { Hono } from 'hono'
import { logger } from 'hono/logger'
import { serve, type ServerType } from '@hono/node-server'
import { randomUUID } from 'crypto'
import { z } from 'zod'
import type { Config } from './config'
import type { Solver } from './solver'

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

const RequestSchema = z.object({
  url: z.string().url(),
  sitekey: z.string().min(1).optional(),
})

type Job = {
  status: 'pending' | 'done' | 'error'
  createdAt: number
  timer: ReturnType<typeof setTimeout>
  token?: string
  error?: string
  time?: number
}

class Jobs {
  #store = new Map<string, Job>()
  #counter = 1
  #ttl: number
  #max: number

  constructor(ttl: number, max: number) { this.#ttl = ttl; this.#max = max }

  create(): string {
    if (this.#store.size >= this.#max) {
      const first = this.#store.keys().next().value
      if (first) this.remove(first)
    }
    const id = String(this.#counter++)
    const timer = setTimeout(() => { if (this.#store.get(id)?.status === 'pending') this.#store.delete(id) }, this.#ttl)
    this.#store.set(id, { status: 'pending', createdAt: Date.now(), timer })
    return id
  }

  set(id: string, data: Partial<Omit<Job, 'timer'>>): void {
    const job = this.#store.get(id)
    if (!job) return
    const next = { ...job, ...data } as Job
    next.timer = job.timer
    if (data.status === 'done' || data.status === 'error') clearTimeout(job.timer)
    this.#store.set(id, next)
  }

  get(id: string): Job | undefined { return this.#store.get(id) }
  remove(id: string): void { const j = this.#store.get(id); if (j) { clearTimeout(j.timer); this.#store.delete(id) } }
  get pending(): number { return [...this.#store.values()].filter(j => j.status === 'pending').length }
  get size(): number { return this.#store.size }

  async drain(maxWait = 30_000): Promise<void> {
    const start = Date.now()
    while (this.pending > 0 && Date.now() - start < maxWait) await sleep(500)
  }
}

export function createApp(config: Config, solver: Solver) {
  const jobs = new Jobs(config.jobTTL, config.maxJobs)
  const app = new Hono()

  // Di server.ts, tambahkan CORS middleware sebelum route definitions
  app.use('*', async (c, next) => {
    c.header('Access-Control-Allow-Origin', '*')
    c.header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
    c.header('Access-Control-Allow-Headers', 'Content-Type')
    await next()
  })


  app.use('*', async (c, next) => { c.set('reqId', randomUUID().slice(0, 8)); await next() })
  app.use('*', logger())

  app.get('/health', c => c.json({ ok: true, ready: solver.ready, jobs: jobs.size, pending: jobs.pending }))

  app.post('/solve', async c => {
    const body = await c.req.json().catch(() => ({}))
    const parsed = RequestSchema.safeParse(body)
    if (!parsed.success) return c.json({ error: 'Invalid body', issues: parsed.error.issues }, 400)
    const { url, sitekey } = parsed.data
    const id = jobs.create()
    solver.solve(url, sitekey ?? null).then(res => {
      jobs.set(id, res.ok ? { status: 'done', token: res.token, time: res.time } : { status: 'error', error: res.error, time: res.time })
    })
    return c.json({ id }, 202)
  })

  app.get('/solve/result/:id', c => {
    const job = jobs.get(c.req.param('id'))
    if (!job) return c.json({ error: 'Not found' }, 404)
    if (job.status === 'pending') return c.json({ status: 'pending' }, 202)
    const { timer, ...rest } = job
    jobs.remove(c.req.param('id'))
    return c.json(rest)
  })

  const submitCaptcha = async (body: Record<string, unknown>) => {
    if (body.methods !== 'turnstile') {
      return { status: 0, request: 'ERROR_NO_SUCH_METHOD' } as const
    }
    if (!body.domain || !body.sitekey) {
      return { status: 0, request: 'ERROR_BAD_PARAMETERS' } as const
    }
    const id = jobs.create()
    solver.solve(body.domain as string, body.sitekey as string).then(res => {
      jobs.set(id, res.ok ? { status: 'done', token: res.token, time: res.time } : { status: 'error', error: res.error, time: res.time })
    })
    return { status: 1, request: id } as const
  }

  // ── Helper: poll result logic ──
  const pollResult = (id: string | undefined) => {
    if (!id) return { status: 0, request: 'ERROR_BAD_PARAMETERS' }
    const job = jobs.get(id)
    if (!job) return { status: 0, request: 'ERROR_CAPTCHA_UNSOLVABLE' }
    if (job.status === 'pending') return { status: 0, request: 'CAPCHA_NOT_READY' }
    if (job.status === 'error') {
      jobs.remove(id)
      return { status: 0, request: 'ERROR_CAPTCHA_UNSOLVABLE' }
    }
    const { timer, token } = job
    jobs.remove(id)
    return { status: 1, request: token }
  }

  app.post('/in.php', async c => {
    const body = await c.req.json().catch(() => ({}))
    c.header('Cache-Control', 'no-store, no-cache, must-revalidate')
    return c.json(await submitCaptcha(body))
  })
  app.get('/res.php', c => {
    c.header('Cache-Control', 'no-store, no-cache, must-revalidate')
    return c.json(pollResult(c.req.query('id'))) 
  })
  
  app.post('/api/in.php', async c => {
    const body = await c.req.json().catch(() => ({}))
    c.header('Cache-Control', 'no-store, no-cache, must-revalidate')
    return c.json(await submitCaptcha(body))
  })
  app.get('/api/res.php', c => {
    c.header('Cache-Control', 'no-store, no-cache, must-revalidate')
    return c.json(pollResult(c.req.query('id')))
  })

  let honoServer: ServerType | null = null

  return {
    app,
    start: async () => {
      await solver.init()
      honoServer = serve({ port: config.port, fetch: app.fetch })
      console.log(`[server] http://localhost:${config.port}`)
      return { port: config.port, fetch: app.fetch }
    },
    stop: async () => {
      console.log(`[server] draining ${jobs.pending} jobs…`)
      await jobs.drain()
      if (honoServer) {
        await new Promise<void>((resolve, reject) => {
          honoServer!.close((err) => (err ? reject(err) : resolve()))
        })
        honoServer = null
      }
      await solver.stop()
    },
  }
}
