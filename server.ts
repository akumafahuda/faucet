import { Hono } from 'hono'
import { serve, type ServerType } from '@hono/node-server'
import { z } from 'zod'
import type { Config } from './config'
import type { Solver } from './solver'

const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms))

const Schema = z.object({
  url: z.string().url(),
  sitekey: z.string().min(1).optional(),
  proxy: z.string().url().optional(),
})

type Job = {
  status: 'pending' | 'done' | 'error'
  createdAt: number
  timer: ReturnType<typeof setTimeout>
  token?: string
  error?: string
  time?: number
}

class Queue {
  #store = new Map<string, Job>()
  #count = 1
  #ttl: number
  #max: number

  constructor(ttl: number, max: number) { this.#ttl = ttl; this.#max = max }

  create(): string {
    if (this.#store.size >= this.#max) {
      const first = this.#store.keys().next().value
      if (first) this.remove(first)
    }
    const id = String(this.#count++)
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
  const q = new Queue(config.jobTTL, config.maxJobs)
  const app = new Hono()

  app.get('/health', c => c.json({ ok: true, ready: solver.ready, jobs: q.size, pending: q.pending }))

  app.post('/solve', async c => {
    const body = await c.req.json().catch(() => ({}))
    const parsed = Schema.safeParse(body)
    if (!parsed.success) return c.json({ error: 'Invalid body' }, 400)
    const { url, sitekey, proxy } = parsed.data
    const id = q.create()
    solver.solve(url, sitekey ?? null, proxy ?? null).then(res => {
      q.set(id, res.ok ? { status: 'done', token: res.token, time: res.time } : { status: 'error', error: res.error, time: res.time })
    })
    return c.json({ id }, 202)
  })

  app.get('/solve/result/:id', c => {
    const job = q.get(c.req.param('id'))
    if (!job) return c.json({ error: 'Not found' }, 404)
    if (job.status === 'pending') return c.json({ status: 'pending' }, 202)
    const { timer, ...rest } = job
    q.remove(c.req.param('id'))
    return c.json(rest)
  })

  let server: ServerType | null = null

  return {
    app,
    start: async () => {
      await solver.init()
      server = serve({ port: config.port, fetch: app.fetch })
      console.log(`[server] Running on port ${config.port}`)
      return { port: config.port, fetch: app.fetch }
    },
    stop: async () => {
      await q.drain()
      if (server) {
        await new Promise<void>((res, rej) => { server!.close((err) => (err ? rej(err) : res())) })
        server = null
      }
      await solver.stop()
    },
  }
}
