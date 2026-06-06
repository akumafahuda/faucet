import { z } from 'zod'
import { parseArgs } from 'util'
import { config as dotenvConfig } from 'dotenv'

dotenvConfig()

export const Config = z.object({
  port: z.coerce.number().default(3000),
  timeout: z.coerce.number().default(60_000),
  headless: z.coerce.boolean().default(false),
  jobTTL: z.coerce.number().default(300_000),
  maxJobs: z.coerce.number().default(1000),
})

export type Config = z.infer<typeof Config>

function env(name: string): string | undefined {
  return process.env[name]
}

function coerceEnv(value: unknown, envValue: string | undefined): unknown {
  if (value !== undefined) return value
  if (envValue === undefined) return undefined
  if (envValue === 'true') return true
  if (envValue === 'false') return false
  if (/^\d+$/.test(envValue)) return Number(envValue)
  return envValue
}

export function loadConfig(argv: string[]): Config {
  const { values } = parseArgs({
    args: argv.slice(2),
    options: {
      port: { type: 'string' },
      timeout: { type: 'string' },
      headless: { type: 'string' },
      'job-ttl': { type: 'string' },
    },
    strict: false,
  })

  const args: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(values)) {
    if (v === undefined) continue
    const key = k.replace(/-([a-z])/g, (_, c) => c.toUpperCase())
    if (v === 'true') args[key] = true
    else if (v === 'false') args[key] = false
    else if (/^\d+$/.test(v)) args[key] = Number(v)
    else args[key] = v
  }

  return Config.parse({
    port: coerceEnv(args.port, env('PORT')),
    timeout: coerceEnv(args.timeout, env('TIMEOUT')),
    headless: coerceEnv(args.headless, env('HEADLESS')),
    jobTTL: coerceEnv(args.jobTTL, env('JOB_TTL')),
  })
}
