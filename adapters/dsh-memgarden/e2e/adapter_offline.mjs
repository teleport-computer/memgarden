/**
 * 不用真 DSH / 真模型的 Adapter 回归。
 *
 * 不是直接对 memgarden serve 发 RPC：这里真正 import plugin.mjs，
 * 调 apply()，再触发 DSH 的 pre-step / turn-stopping / dispose hook。
 */
import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { spawnSync } from 'node:child_process'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '../../..')
const PLUGIN = path.join(ROOT, 'src/memgarden/adapters/dsh/plugin.mjs')
const SERVICE = path.join(HERE, 'fixtures/fake_wire_service.mjs')

function waitFor(check, timeoutMs = 5000) {
  const started = Date.now()
  return new Promise((resolve, reject) => {
    const poll = () => {
      try {
        if (check()) return resolve()
      } catch { /* 文件可能尚未创建 */ }
      if (Date.now() - started >= timeoutMs) {
        return reject(new Error('等待 Adapter 动作超时'))
      }
      setTimeout(poll, 20)
    }
    poll()
  })
}

function calls(logPath) {
  try {
    return readFileSync(logPath, 'utf8').split('\n').filter(Boolean).map(JSON.parse)
  } catch { return [] }
}

function fakeContext() {
  const hooks = new Map()
  return {
    hooks,
    tools: { register() {} },
    llm: {
      async *stream() {
        // 同一个回答同时是合法的 capture noop 和 maintenance noop；
        // 这里验状态机接线，不测模型质量。
        yield { type: 'text-delta', text: '{"cards":[],"consolidations":[]}' }
        yield { type: 'finish', reason: { kind: 'stop' } }
      },
    },
    on(name, fn) { hooks.set(name, fn) },
  }
}

function scriptedContext(replies) {
  const hooks = new Map()
  let index = 0
  return {
    hooks,
    get calls() { return index },
    tools: { register() {} },
    llm: {
      async *stream() {
        assert.ok(index < replies.length, 'Adapter 发起了未预期的额外模型调用')
        const reply = replies[index++]
        if (typeof reply.text === 'string' && reply.text) {
          yield { type: 'text-delta', text: reply.text }
        }
        yield { type: 'finish', reason: reply.finish || { kind: 'stop' } }
      },
    },
    on(name, fn) { hooks.set(name, fn) },
  }
}

function serviceRpc(bin, storage, requests) {
  const proc = spawnSync(bin, ['serve', '--storage', storage], {
    input: requests.map((x, i) => JSON.stringify({ id: String(i), ...x })).join('\n') + '\n',
    encoding: 'utf8', timeout: 15000,
  })
  assert.equal(proc.status, 0, proc.stderr)
  const replies = proc.stdout.split('\n').filter(Boolean).map(JSON.parse)
  assert.equal(replies.length, requests.length, proc.stderr || proc.stdout)
  assert.ok(replies.every((x) => x.ok), JSON.stringify(replies))
  return replies.map((x) => x.result)
}

function realScope(suffix) {
  return {
    tenant_id: `tenant-${suffix}`, memory_owner_id: `owner-${suffix}`,
    actor: { user_id: `tenant-${suffix}`, agent_id: 'seed' },
    allowed_mounts: ['agent-private'],
  }
}

function seedMaintenanceThreshold(bin, storage, scope, prefix) {
  const requests = Array.from({ length: 10 }, (_, i) => ({
    method: 'records.write', params: {
      scope, text: `离线格式重试验收事实 ${i}`, bucket: 'general',
      idempotency_key: `${prefix}-${i}`,
    },
  }))
  const results = serviceRpc(bin, storage, requests)
  assert.ok(results.every((x) => !x?.error), JSON.stringify(results))
}

async function runActualTurn({ bin, storage, stateDir, debugLog, scope, ctx }) {
  process.env.MEMGARDEN_DEBUG_LOG = debugLog
  const { apply } = await loadPlugin()
  apply(ctx, {
    bin, storage, tenant: scope.tenant_id,
    memoryOwner: scope.memory_owner_id, stateDir,
  })
  await waitFor(() => {
    try { return readFileSync(debugLog, 'utf8').includes('握手成功') }
    catch { return false }
  })
  const messages = [{ role: 'user', content: '这一轮用来触发格式重试和整理' }]
  const agent = {
    name: 'agent-real',
    session: { id: `session-${scope.memory_owner_id}`, messages },
  }
  await ctx.hooks.get('agent/pre-step')(
    { agent, turn: 1, messages }, async () => ({ kind: 'enter', messages: [] }))
  messages.push({ role: 'assistant', content: '好的' })
  await ctx.hooks.get('agent/turn-stopping')({ agent, turn: 1 })
  await ctx.hooks.get('dispose')()
  delete process.env.MEMGARDEN_DEBUG_LOG
}

async function loadPlugin() {
  // 两个场景各用一份模块状态，避免 inflight 等模块级状态串台。
  return import(pathToFileURL(PLUGIN).href + '?offline=' + Math.random())
}

async function successfulTurnGoesThroughAdapter() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-ok-'))
  const logPath = path.join(dir, 'requests.jsonl')
  const stateDir = path.join(dir, 'state')
  process.env.MEMGARDEN_FAKE_REQUEST_LOG = logPath
  delete process.env.MEMGARDEN_FAKE_CAPTURE_ERROR

  const ctx = fakeContext()
  const { apply } = await loadPlugin()
  apply(ctx, {
    bin: SERVICE, storage: 'ignored', tenant: 'tenant-1',
    memoryOwner: 'owner-1', stateDir,
    // 故意给小预算，让截断语义也穿过真 Adapter 被验到。
    captureWindowChars: 1200, captureMessageChars: 300,
  })

  await waitFor(() => calls(logPath).some((x) => x.method === 'manifest.get'))
  const messages = [{ role: 'user', content: '我想记住这件事' }]
  const agent = { name: 'agent-1', session: { id: 'session-1', messages } }
  const pre = ctx.hooks.get('agent/pre-step')
  const stop = ctx.hooks.get('agent/turn-stopping')
  const dispose = ctx.hooks.get('dispose')
  assert.ok(pre && stop && dispose, 'Adapter 应注册三个关键 hook')

  await pre({ agent, turn: 1, messages }, async () => ({ kind: 'enter', messages: [] }))
  const firstContextCalls = calls(logPath).filter(
    (x) => x.method === 'context.get').length
  messages.push({ role: 'tool', content: '工具循环中的中间结果' })
  await pre({ agent, turn: 1, messages }, async () => ({ kind: 'enter', messages: [] }))
  assert.equal(calls(logPath).filter((x) => x.method === 'context.get').length,
               firstContextCalls,
               '同一个 turn 的工具循环不应重复召回/注入记忆')
  // 超过旧实现的 40 条，并带一条超长工具结果。
  for (let i = 0; i < 45; i += 1) {
    messages.push({ role: i % 2 ? 'assistant' : 'tool',
                    content: `message-${i}-` + 'x'.repeat(i === 44 ? 900 : 20) })
  }
  messages.push({ role: 'assistant', content: '最后的助手答复' })
  await stop({ agent, turn: 1 })

  const sent = calls(logPath)
  const methods = sent.map((x) => x.method)
  assert.ok(methods.includes('capture.begin'))
  assert.ok(methods.includes('capture.feed'))
  assert.ok(methods.includes('maintenance.begin'))
  assert.ok(methods.includes('maintenance.feed'))
  assert.ok(!methods.includes('maintenance.run'),
            'modelless service 不应再被要求自己调模型')

  const window = sent.find((x) => x.method === 'capture.begin').params.window
  assert.ok(!window.startsWith('用户：用户：'), '不应重复加用户前缀')
  assert.ok(window.includes('省略'), '超预算时必须明确标记，不得静默 slice')
  assert.ok(window.includes('最后的助手答复'), '应保留 turn 尾部')
  const outbox = path.join(stateDir, 'memgarden-outbox.jsonl')
  assert.equal(readFileSync(outbox, 'utf8'), '', '成功落卡后应清空 outbox')

  await dispose()
  rmSync(dir, { recursive: true, force: true })
}

async function recoveryKeepsFailedReceipt() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-recovery-'))
  const logPath = path.join(dir, 'requests.jsonl')
  const stateDir = path.join(dir, 'state')
  const outbox = path.join(stateDir, 'memgarden-outbox.jsonl')
  mkdirSync(stateDir, { recursive: true })
  writeFileSync(outbox, JSON.stringify({
    key: 'tenant-1:owner-1:session-1:turn:9',
    scope: {
      tenant_id: 'tenant-1', memory_owner_id: 'owner-1',
      actor: { user_id: 'tenant-1', agent_id: 'agent-1', session_id: 'session-1' },
      allowed_mounts: ['agent-private'],
    },
    locale: 'zh-Hans', window: '用户：需要恢复的一轮',
  }) + '\n')
  process.env.MEMGARDEN_FAKE_REQUEST_LOG = logPath
  process.env.MEMGARDEN_FAKE_CAPTURE_ERROR = 'revision_conflict'

  const ctx = fakeContext()
  const { apply } = await loadPlugin()
  apply(ctx, {
    bin: SERVICE, storage: 'ignored', tenant: 'tenant-1',
    memoryOwner: 'owner-1', stateDir,
  })
  await waitFor(() => calls(logPath).some((x) => x.method === 'capture.feed'))
  await new Promise((resolve) => setTimeout(resolve, 50))
  const remaining = readFileSync(outbox, 'utf8')
  assert.ok(remaining.includes('turn:9'),
            'RPC ok 但 receipt.error 时不能把恢复待办划掉')

  await ctx.hooks.get('dispose')()
  delete process.env.MEMGARDEN_FAKE_CAPTURE_ERROR
  rmSync(dir, { recursive: true, force: true })
}

async function actualServiceAlsoClosesTheHostDrivenLoop() {
  const bin = process.env.MEMGARDEN_BIN
  assert.ok(bin, 'Python wrapper 必须把当前环境的 memgarden 传进来')
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-real-service-'))
  const db = path.join(dir, 'garden.db')
  const storage = 'sqlite:///' + db
  const stateDir = path.join(dir, 'state')
  const debugLog = path.join(dir, 'adapter.log')
  const scope = {
    tenant_id: 'tenant-real', memory_owner_id: 'owner-real',
    actor: { user_id: 'tenant-real', agent_id: 'seed' },
    allowed_mounts: ['agent-private'],
  }
  const seed = Array.from({ length: 10 }, (_, i) => ({
    id: String(i), method: 'records.write', params: {
      scope, text: `离线真服务验收事实 ${i}`, bucket: 'general',
      idempotency_key: `offline-real-${i}`,
    },
  }))
  const seeded = spawnSync(bin, ['serve', '--storage', storage], {
    input: seed.map((x) => JSON.stringify(x)).join('\n') + '\n',
    encoding: 'utf8', timeout: 15000,
  })
  assert.equal(seeded.status, 0, seeded.stderr)
  const replies = seeded.stdout.split('\n').filter(Boolean).map(JSON.parse)
  assert.equal(replies.length, 10)
  assert.ok(replies.every((x) => x.ok && !x.result?.error))

  delete process.env.MEMGARDEN_FAKE_REQUEST_LOG
  delete process.env.MEMGARDEN_FAKE_CAPTURE_ERROR
  process.env.MEMGARDEN_DEBUG_LOG = debugLog
  const ctx = fakeContext()
  const { apply } = await loadPlugin()
  apply(ctx, {
    bin, storage, tenant: 'tenant-real', memoryOwner: 'owner-real', stateDir,
  })
  await waitFor(() => {
    try { return readFileSync(debugLog, 'utf8').includes('握手成功') }
    catch { return false }
  })
  const messages = [{ role: 'user', content: '这一轮用来触发整理' }]
  const agent = { name: 'agent-real', session: { id: 'session-real', messages } }
  await ctx.hooks.get('agent/pre-step')(
    { agent, turn: 1, messages }, async () => ({ kind: 'enter', messages: [] }))
  messages.push({ role: 'assistant', content: '好的' })
  await ctx.hooks.get('agent/turn-stopping')({ agent, turn: 1 })

  const observed = readFileSync(debugLog, 'utf8')
  assert.ok(observed.includes('整理结果'),
            '真 memgarden serve 也应走完 host-driven maintenance')
  assert.ok(!observed.includes('model_not_configured'))
  await ctx.hooks.get('dispose')()
  delete process.env.MEMGARDEN_DEBUG_LOG
  rmSync(dir, { recursive: true, force: true })
}

async function emptyStopUsesCoreFormatRetryForCaptureAndMaintenance() {
  const bin = process.env.MEMGARDEN_BIN
  assert.ok(bin, 'Python wrapper 必须把当前环境的 memgarden 传进来')
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-empty-retry-'))
  const storage = 'sqlite:///' + path.join(dir, 'garden.db')
  const scope = realScope('empty-retry')
  const debugLog = path.join(dir, 'adapter.log')
  seedMaintenanceThreshold(bin, storage, scope, 'empty-retry')

  delete process.env.MEMGARDEN_FAKE_REQUEST_LOG
  delete process.env.MEMGARDEN_FAKE_CAPTURE_ERROR
  const ctx = scriptedContext([
    // Capture 首答空 stop；内核应返回 format retry prompt，而不是 Adapter 抛错。
    { text: '' },
    { text: '{"cards":[]}' },
    // Maintenance 同样必须由同一个内核状态机拥有格式重试语义。
    { text: '' },
    { text: '{"consolidations":[],"questions_to_ask":[]}' },
  ])
  await runActualTurn({
    bin, storage, stateDir: path.join(dir, 'state'), debugLog, scope, ctx,
  })

  const observed = readFileSync(debugLog, 'utf8')
  assert.equal(ctx.calls, 4, 'Capture 和 Maintenance 应各做一次有界格式重试')
  assert.ok(observed.includes('落卡 written=false'))
  assert.ok(observed.includes('error=-'), observed)
  assert.ok(observed.includes('整理结果 written=true'), observed)
  assert.ok(!observed.includes('capture 失败'), observed)
  assert.ok(!observed.includes('整理失败'), observed)
  const [check] = serviceRpc(bin, storage, [
    { method: 'maintenance.check', params: { scope } },
  ])
  assert.equal(check.needed, false,
               '合法第二答完成后 Maintenance 账本应推进')
  assert.equal(readFileSync(path.join(dir, 'state', 'memgarden-outbox.jsonl'), 'utf8'), '',
               '合法 Capture 第二答完成后 outbox 应清空')
  rmSync(dir, { recursive: true, force: true })
}

async function repeatedEmptyStopFailsWithoutAdvancingMaintenanceLedger() {
  const bin = process.env.MEMGARDEN_BIN
  assert.ok(bin, 'Python wrapper 必须把当前环境的 memgarden 传进来')
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-empty-fail-'))
  const storage = 'sqlite:///' + path.join(dir, 'garden.db')
  const scope = realScope('empty-fail')
  const debugLog = path.join(dir, 'adapter.log')
  seedMaintenanceThreshold(bin, storage, scope, 'empty-fail')

  const ctx = scriptedContext([
    { text: '{"cards":[]}' },
    { text: '' },
    { text: '' },
  ])
  await runActualTurn({
    bin, storage, stateDir: path.join(dir, 'state'), debugLog, scope, ctx,
  })

  const observed = readFileSync(debugLog, 'utf8')
  assert.equal(ctx.calls, 3, '第二次空 stop 后应停止，不在 Adapter 里另造重试策略')
  assert.ok(observed.includes('整理结果 written=false'), observed)
  assert.ok(observed.includes('error=no_json_object'), observed)
  assert.ok(observed.includes('整理失败: 整理未落库: no_json_object'), observed)
  const [check] = serviceRpc(bin, storage, [
    { method: 'maintenance.check', params: { scope } },
  ])
  assert.equal(check.needed, true,
               '连续空 stop 失败后不得推进 Maintenance 账本或假装成功')
  rmSync(dir, { recursive: true, force: true })
}

async function repeatedEmptyCaptureKeepsOutboxAndWritesNoCard() {
  const bin = process.env.MEMGARDEN_BIN
  assert.ok(bin, 'Python wrapper 必须把当前环境的 memgarden 传进来')
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-empty-capture-'))
  const storage = 'sqlite:///' + path.join(dir, 'garden.db')
  const stateDir = path.join(dir, 'state')
  const scope = realScope('empty-capture')
  const debugLog = path.join(dir, 'adapter.log')
  const ctx = scriptedContext([{ text: '' }, { text: '' }])

  await runActualTurn({ bin, storage, stateDir, debugLog, scope, ctx })

  const observed = readFileSync(debugLog, 'utf8')
  assert.equal(ctx.calls, 2, 'Capture 连续空 stop 只消耗内核既有的一次重试预算')
  assert.ok(observed.includes('落卡 written=false'), observed)
  assert.ok(observed.includes('error=no_json_object'), observed)
  assert.ok(observed.includes('capture 失败: 落卡未落库: no_json_object'), observed)
  assert.ok(!observed.includes('该整理了'), 'Capture 失败后不得继续 Maintenance')
  assert.ok(readFileSync(path.join(stateDir, 'memgarden-outbox.jsonl'), 'utf8')
              .includes('tenant-empty-capture:owner-empty-capture:'),
            'Capture 连续空 stop 后 outbox 必须保留供恢复')
  const [exported] = serviceRpc(bin, storage, [
    { method: 'records.export', params: { scope } },
  ])
  assert.deepEqual(exported.items?.records, [],
                   'Capture 连续空 stop 不得写卡或假成功')
  rmSync(dir, { recursive: true, force: true })
}

async function finishErrorStillPropagatesWithoutFeedingOrClearingOutbox() {
  const dir = mkdtempSync(path.join(os.tmpdir(), 'memgarden-adapter-finish-error-'))
  const logPath = path.join(dir, 'requests.jsonl')
  const debugLog = path.join(dir, 'adapter.log')
  const stateDir = path.join(dir, 'state')
  process.env.MEMGARDEN_FAKE_REQUEST_LOG = logPath
  process.env.MEMGARDEN_DEBUG_LOG = debugLog
  delete process.env.MEMGARDEN_FAKE_CAPTURE_ERROR
  const ctx = scriptedContext([{
    text: '',
    finish: { kind: 'error', failure: { code: 'offline_provider_failure' } },
  }])
  const { apply } = await loadPlugin()
  apply(ctx, {
    bin: SERVICE, storage: 'ignored', tenant: 'tenant-error',
    memoryOwner: 'owner-error', stateDir,
  })
  await waitFor(() => calls(logPath).some((x) => x.method === 'manifest.get'))
  const messages = [{ role: 'user', content: '这次 provider 明确失败' }]
  const agent = { name: 'agent-error', session: { id: 'session-error', messages } }
  await ctx.hooks.get('agent/pre-step')(
    { agent, turn: 1, messages }, async () => ({ kind: 'enter', messages: [] }))
  messages.push({ role: 'assistant', content: '不会完成' })
  await ctx.hooks.get('agent/turn-stopping')({ agent, turn: 1 })

  const methods = calls(logPath).map((x) => x.method)
  assert.ok(methods.includes('capture.begin'))
  assert.ok(!methods.includes('capture.feed'), 'finish.kind=error 不得伪装成空回复喂给内核')
  assert.ok(!methods.includes('maintenance.begin'), 'Capture 失败后不得继续 Maintenance')
  assert.ok(readFileSync(debugLog, 'utf8').includes('capture 失败: 模型调用失败'))
  assert.ok(readFileSync(path.join(stateDir, 'memgarden-outbox.jsonl'), 'utf8')
              .includes('tenant-error:owner-error:session-error:turn:1'),
            'provider 明确失败时 outbox 必须保留供恢复')

  await ctx.hooks.get('dispose')()
  delete process.env.MEMGARDEN_FAKE_REQUEST_LOG
  delete process.env.MEMGARDEN_DEBUG_LOG
  rmSync(dir, { recursive: true, force: true })
}

await successfulTurnGoesThroughAdapter()
await recoveryKeepsFailedReceipt()
await actualServiceAlsoClosesTheHostDrivenLoop()
await emptyStopUsesCoreFormatRetryForCaptureAndMaintenance()
await repeatedEmptyStopFailsWithoutAdvancingMaintenanceLedger()
await repeatedEmptyCaptureKeepsOutboxAndWritesNoCard()
await finishErrorStillPropagatesWithoutFeedingOrClearingOutbox()
console.log('DSH Adapter offline regression: PASS')
