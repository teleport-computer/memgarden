/**
 * Memory Garden 挂到 DeepSeek Harness —— 端到端验证用的最小实现。
 *
 * 只做三件事，都走 DSH 的正式扩展点，**不改 DSH 任何代码**：
 *   agent/pre-step      每轮自动带上相关记忆
 *   agent/turn-stopping 轮末自动落卡
 *   ctx.tools.register  注册 memory_search / memory_write
 *
 * 判断全部回到 Python 那边（memgarden serve），这里只翻译和接线。
 */
import { spawn } from 'node:child_process'
import {
  appendFileSync, existsSync, mkdirSync, readFileSync, renameSync, unlinkSync,
  writeFileSync,
} from 'node:fs'
import nodePath from 'node:path'

// SDK 会吞掉子进程的 stderr，所以自己再落一份文件 ——
// 否则「插件没跑」和「跑了但报错」区分不开，而这两件事的处置完全不同。
const LOG = process.env.MEMGARDEN_DEBUG_LOG

function log(message) {
  if (LOG) {
    try { appendFileSync(LOG, message) } catch { /* 观测失败不该影响主流程 */ }
  }
  process.stderr.write(message)
}

export const name = 'memgarden'
export const inject = ['tools', 'llm']

class Client {
  constructor(bin, storage) {
    this.bin = bin
    this.storage = storage
    this.pending = new Map()
    this.next = 1
    this.buf = ''
    this.restarts = 0
    this.closed = false
    // 🔴 重启耗尽后置成 Error，request() 据此立刻失败。
    // 不置的话（此前就是），后续请求会继续写一个已经退出的子进程的 stdin，
    // 然后等满 120s 超时 —— 每一轮对话都白等两分钟，而根因早就发生了。
    this.dead = null
    this.spawn()
  }

  spawn() {
    this.dead = null          // 起来了就不再是 dead
    // 🔴 **不给服务任何模型配置**。模型调用归 DSH —— 它持有 provider、
    // key、路由、用量统计、超时、取消、重试。让 Garden 另开一条 DSH 管不着的
    // 通道，后果很具体：用户按「停止」时那次落卡的调用停不下来，也不计入用量。
    this.child = spawn(this.bin, ['serve', '--storage', this.storage], {
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.child.on('error', (e) => {
      // spawn 本身失败（文件不存在、没有执行权限）。**不能让它冒出去** ——
      // 未处理的 error 事件会直接杀掉宿主进程，也就是「记忆服务装错了路径」
      // 导致整个 agent 起不来。
      log('[memgarden] 服务起不来: ' + e.message + '\n')
      this.failAll('service_unavailable', e.message)
    })
    this.child.stdout.setEncoding('utf8')
    this.child.stdout.on('data', (c) => this.onData(c))
    this.child.stderr.setEncoding('utf8')
    this.child.stderr.on('data', (c) => log('[memgarden:py] ' + c))
    this.child.on('exit', (code) => {
      log('[memgarden] 服务退出 code=' + code + '\n')
      // 在飞的请求必须得到**明确结果**。悬着不回的话，turn-stopping 会一直
      // 等到超时，用户那边表现为「这一轮结束得特别慢」，而且没有任何线索。
      this.failAll('service_exited', 'code=' + code)
      this.maybeRestart()
    })
  }

  /**
   * 有界重启。
   *
   * 不重启 = 服务崩一次之后这个进程再也没有记忆，而且悄无声息。
   * 无界重启 = 一个必然失败的配置（比如 storage 路径没权限）会变成
   * 无限重启风暴，把日志和 CPU 都吃光。所以给个上限。
   */
  maybeRestart() {
    if (this.closed) return
    if (this.restarts >= MAX_RESTARTS) {
      this.dead = new Error('已重启 ' + this.restarts + ' 次仍然退出')
      log('[memgarden] 已重启 ' + this.restarts + ' 次仍然退出，不再重试。\n' +
          '            记忆功能从现在起不可用，但对话不受影响。\n')
      return
    }
    this.restarts += 1
    const delay = Math.min(1000 * 2 ** (this.restarts - 1), 8000)
    log('[memgarden] ' + delay + 'ms 后第 ' + this.restarts + ' 次重启\n')
    setTimeout(() => { if (!this.closed) this.spawn() }, delay).unref?.()
  }

  failAll(code, message) {
    for (const [, pending] of this.pending) {
      clearTimeout(pending.timer)
      pending.resolve({ ok: false, error: { code, message } })
    }
    this.pending.clear()
    this.buf = ''
  }

  close() {
    this.closed = true
    try { this.child?.kill() } catch { /* 已经没了就算了 */ }
    this.failAll('service_closed', 'plugin disposed')
  }

  onData(chunk) {
    this.buf += chunk
    // 按行切 —— 一个 chunk 里有半行是常态，直接 JSON.parse(chunk) 量一大就随机失败
    let i
    while ((i = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, i).trim()
      this.buf = this.buf.slice(i + 1)
      if (!line) continue
      try {
        const res = JSON.parse(line)
        const pending = this.pending.get(String(res.id))
        if (pending) {
          this.pending.delete(String(res.id))
          clearTimeout(pending.timer)
          pending.resolve(res)
        }
      } catch {
        log('[memgarden] 非 JSON 输出: ' + line.slice(0, 160) + '\n')
      }
    }
  }

  request(method, params) {
    // 服务已经死了就直接失败，不要往一根断掉的管子里写（那会再抛一次 EPIPE）
    if (this.dead) return Promise.reject(Object.assign(
      new Error(method + ': 服务不可用（' + this.dead.message + '）'),
      { code: 'service_unavailable' },
    ))
    const id = String(this.next++)
    return new Promise((resolve) => {
      const pending = { resolve, timer: null }
      this.pending.set(id, pending)
      try {
        this.child.stdin.write(JSON.stringify({ id, method, params }) + '\n')
      } catch (e) {
        // 管道已经断了。**当场给结果**，别让调用方等超时 ——
        // 「写不进去」和「写进去了但没回」对调用方是两回事：
        // 前者可以安全重试，后者的结果是 unknown，要靠幂等键去对账。
        this.pending.delete(id)
        resolve({ ok: false,
                  error: { code: 'service_unavailable', message: e.message } })
        return
      }
      pending.timer = setTimeout(() => {
        if (this.pending.delete(id)) {
          resolve({ ok: false, error: { code: 'timeout', message: method } })
        }
      }, 120000)
    }).then((res) => {
      if (!res.ok) {
        const e = new Error(method + ': ' + (res.error && res.error.code))
        e.code = res.error && res.error.code
        throw e
      }
      return res.result
    })
  }
}

/**
 * 用 **DSH 自己的模型** 驱动一次落卡。
 *
 *     begin  → Garden 说「该问模型这句话」
 *     ctx.llm.stream(...)  ← DSH 调，用它的 provider / key / 重试 / 取消
 *     feed   → Garden 解析、判断要不要重问
 *     …直到 completed
 *
 * Garden 全程不碰 key，也不知道用的是哪个 provider。
 */
async function driveCapture(ctx, client, scope, locale, text, agent, turn, config) {
  // 🔴 幂等身份必须包含 tenant + owner + session + turn。
  //
  // 以前是 `tenant + ':dsh:' + turn` —— 两个会话都从 turn 1 开始时会**撞
  // 同一个键**，于是第二个会话的第一轮被当成第一个会话的重放，
  // 那一轮什么都不会写，而且回的是「成功」。
  const key = [scope.tenant_id, scope.memory_owner_id,
               sessionIdOf(agent) || 'nosession', 'turn', turn].join(':')
  return driveCaptureRaw(ctx, client, scope, locale, text, key, config)
}


/** 幂等键由调用方给的版本 —— 崩溃恢复要用**原来那个键**重放。 */
async function driveCaptureRaw(ctx, client, scope, locale, text, key, config) {
  let state = await client.request('capture.begin', {
    scope,
    // `text` 是 renderTurnWindow() 生成的完整本轮窗口，里面已经有
    // 「用户：/助手：/工具结果：」。再加一次前缀会让真正交给
    // Garden 的内容变成「用户：用户：…」，并且把后续的助手/工具
    // 行都伪装成第一条用户消息的一部分。
    window: text,
    locale,
    // 稳定幂等键：崩溃后重放同一轮不会写第二遍。**它不依赖会话还在** ——
    // 进程重启会丢掉在途会话，但重放同一轮仍然不会写出第二条记忆。
    idempotency_key: key,
  })

  let rounds = 0
  while (state.status === 'needs_model') {
    // 有上限：模型一直吐脏东西时不能无限重问下去，那会烧光额度。
    if (++rounds > 4) {
      await client.request('capture.cancel', { session_id: state.session_id })
      throw new Error('落卡重问超过 4 轮，放弃')
    }
    const reply = await callModel(ctx, config, state.next_prompt, 'capture')
    state = await client.request('capture.feed', {
      session_id: state.session_id,
      reply: reply.text,
      // 🔴 是否被截断只有拿到原始响应元数据的这一层看得见，内核看不到。
      //
      // 以前这里写的是 `reply.truncated`，而 reply 是**字符串** ——
      // 字符串没有这个属性，于是 truncated 恒为 false。后果：模型输出被截断
      // 时，内核以为拿到的是完整回复，把半个 JSON 当成「没什么可记」，
      // 而不是重问一次。表现是长对话偶尔莫名其妙什么都没记住。
      truncated: reply.truncated === true,
      finish_reason: reply.finishReason || '',
    })
  }
  return state.result
}

/** 用 DSH 的 provider 驱动一次整理，不让 modelless service 自己调模型。 */
async function driveMaintenance(ctx, client, scope, locale, config) {
  let state = await client.request('maintenance.begin', {
    scope,
    locale,
    ai_name: config.aiName || '',
    user_name: config.userName || '',
  })

  let rounds = 0
  while (state.status === 'needs_model') {
    if (++rounds > 4) {
      await client.request('maintenance.cancel', { session_id: state.session_id })
      throw new Error('整理重问超过 4 轮，放弃')
    }
    const reply = await callModel(ctx, config, state.next_prompt, 'maintenance')
    state = await client.request('maintenance.feed', {
      session_id: state.session_id,
      reply: reply.text,
      truncated: reply.truncated === true,
      finish_reason: reply.finishReason || '',
    })
  }
  return state.result
}

/** 用 DSH 的 llm 服务发一次一次性请求，把流拼成完整文本。 */
async function callModel(ctx, config, prompt, purpose = 'capture') {
  const purposeModel = purpose === 'maintenance' ? config.maintenanceModel : null
  const chunks = []
  const stream = ctx.llm.stream({
    provider: config.provider || 'deepseek-official',
    model: purposeModel || config.captureModel || config.model || 'deepseek-v4-flash',
    messages: [{ role: 'user', content: [{ type: 'text', text: prompt }] }],
    maxTokens: 4096,
    temperature: 0.2,
  })
  let finish
  for await (const chunk of stream) {
    if (!chunk) continue
    // 🔴 字段是 `text`，不是 `delta`。写成 chunk.delta 时它永远是 undefined，
    // 拼出来是空串 —— 而空串会让 Garden 解析失败、落卡产出 0 张卡，
    // **整条链路每一步都「成功」，只是什么都没记住**。
    // 只收文本增量：reasoning-delta 是模型的思考过程，不该进记忆。
    if (chunk.type === 'text-delta' && typeof chunk.text === 'string') {
      chunks.push(chunk.text)
    } else if (chunk.type === 'finish') {
      finish = chunk.reason
    }
  }
  if (finish && finish.kind === 'error') {
    throw new Error('模型调用失败: ' + JSON.stringify(finish.failure || finish))
  }
  const out = chunks.join('')
  const truncated = Boolean(finish && (finish.kind === 'length'
                                       || finish.reason === 'length'
                                       || finish.truncated === true))
  // 空白 stop 不是合法 noop，但也不该在 Adapter 这层直接抛错。
  // Capture/Maintenance 状态机会只对「no_json_object 且原文为空白」给出
  // 一次有界的格式重试；非空纯 prose 不会被泛化重试。第二答仍空白才返回
  // 显式 error，且不推进 Capture frontier / Maintenance ledger。在这里
  // throw 会绕过那个唯一语义 owner，让真实 DSH 的空 stop 没有重试机会。
  return { text: out, truncated, finishReason: String(finish?.kind || '') }
}

//: dispose 时最多等多久。不等 = 丢记忆；无限等 = 一个卡住的模型调用
//: 能让整个进程关不掉。
const DRAIN_TIMEOUT_MS = 15000

//: 子进程反复退出时最多重启几次。见 Client.maybeRestart。
const MAX_RESTARTS = 5

/** 在飞的落卡。dispose 时要等它们收尾。 */
const inflight = new Set()

function sessionIdOf(agent) {
  try { return String(agent?.session?.id || '') } catch { return '' }
}

function agentIdOf(agent) {
  try { return String(agent?.options?.name || agent?.name || 'dsh') }
  catch { return 'dsh' }
}

/**
 * 把这一轮渲染成落卡用的窗口。
 *
 * 只给用户那句问话是不够的 —— 这一轮真正值得记的东西，往往在助手的回答里
 * （「我帮你订了周四下午三点」）或者工具结果里。所以从 session 的消息里
 * 取出属于本轮的部分。
 *
 * 取不到 session 时退回只用 query：**降级要能看出来**，不能悄悄记一半。
 */
function messageCount(agent) {
  try {
    return (agent?.session?.surface?.messages
            || agent?.session?.messages || []).length
  } catch { return 0 }
}

// 这是「一次模型上下文插入」的界，不是记忆的总量上限。
// 不再用 slice(-40) / tool.slice(0, 500) 静默丢数据：先读完整 turn，
// 只在真正超过明确预算时截取，且把丢掉的字符数写进窗口。
const DEFAULT_CAPTURE_WINDOW_CHARS = 64000
const DEFAULT_CAPTURE_MESSAGE_CHARS = 16000

function positiveLimit(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? Math.floor(n) : fallback
}

function clipWithNotice(text, limit, label) {
  const chars = Array.from(String(text || ''))
  if (chars.length <= limit) return chars.join('')
  // marker 本身也占预算，所以「省略数」要按最终保留的头尾重算，
  // 不能简单写 chars.length - limit（那会少报 marker 所占的部分）。
  let omitted = chars.length - limit
  let notice = []
  for (let i = 0; i < 3; i += 1) {
    notice = Array.from(`\n[…${label}，省略 ${omitted} 个字符…]\n`)
    omitted = chars.length - Math.max(0, limit - notice.length)
  }
  notice = Array.from(`\n[…${label}，省略 ${omitted} 个字符…]\n`)
  // 即使调用方给了非常小的测试预算，也要保留可读的截断标记。
  if (limit <= notice.length + 2) return notice.slice(0, limit).join('')
  const room = limit - notice.length
  const head = Math.floor(room * 0.4)
  const tail = room - head
  return chars.slice(0, head).join('') + notice.join('')
       + chars.slice(chars.length - tail).join('')
}

function renderTurnWindow(agent, query, from = 0, config = {}) {
  const lines = []
  const perMessage = positiveLimit(config.captureMessageChars,
                                   DEFAULT_CAPTURE_MESSAGE_CHARS)
  const total = positiveLimit(config.captureWindowChars,
                              DEFAULT_CAPTURE_WINDOW_CHARS)
  try {
    const msgs = agent?.session?.surface?.messages
                 || agent?.session?.messages || []
    // 只取本轮开始之后的消息，但不再按「最后 40 条」静默丢掉前半轮。
    for (const m of msgs.slice(Math.max(0, from))) {
      const role = String(m?.role || '')
      const raw = textOf(m?.content)
      const text = clipWithNotice(raw, perMessage, '该条消息超过单条预算')
      if (!text.trim()) continue
      if (role === 'user') lines.push('用户：' + text)
      else if (role === 'assistant') lines.push('助手：' + text)
      else if (role === 'tool') lines.push('工具结果：' + text)
    }
  } catch (e) {
    log('[memgarden] 取本轮消息失败，退回只用用户问句: ' + e.message + '\n')
  }
  if (!lines.length && query) lines.push('用户：' + query)
  return clipWithNotice(lines.join('\n'), total, '本轮内容超过总预算')
}

function textOf(content) {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.map((p) => {
      if (!p) return ''
      if (typeof p === 'string') return p
      if (typeof p.text === 'string') return p.text
      if (typeof p.content === 'string') return p.content
      try { return JSON.stringify(p) } catch { return String(p) }
    }).join(' ')
  }
  if (content && typeof content === 'object') {
    try { return JSON.stringify(content) } catch { return String(content) }
  }
  return ''
}


/**
 * 落卡的持久待办本。
 *
 * ## 为什么必须落盘
 *
 * `turn-stopping` 返回 promise 只解决**正常退出**：DSH 会等落卡做完。
 * 但进程在 turn 中途被 kill、崩溃、机器断电时，那一轮的落卡就没了 ——
 * 没有报错，只是那条记忆不存在。而用户那边刚刚说完一件重要的事。
 *
 * 幂等键保证「重放不会写两遍」，但**得有人去重放**。这就是那个人。
 *
 * ## 为什么是 JSON 行而不是数据库
 *
 * 一个只需要「追加、读全部、删一条」的待办本，用文件就够了；引入第二个
 * 数据库意味着第二套一致性问题，而它和记忆本身的库还不是同一个。
 */
class Outbox {
  constructor(dir) {
    this.path = dir ? nodePath.join(dir, 'memgarden-outbox.jsonl') : ''
  }

  /** 记下「这一轮要落卡」。**在调模型之前写** —— 之后写就白写了。 */
  add(entry) {
    if (!this.path) return
    try {
      mkdirSync(nodePath.dirname(this.path), { recursive: true, mode: 0o700 })
      appendFileSync(this.path, JSON.stringify(entry) + '\n',
                     { encoding: 'utf8', mode: 0o600 })
    } catch (e) {
      // 待办本写不了不该挡住落卡本身 —— 那样是为了防丢反而先丢了。
      log('[memgarden] outbox 写入失败（本轮崩溃将无法恢复）: ' + e.message + '\n')
    }
  }

  /** 只有落卡完成（包括明确「无事可记」）才划掉。 */
  done(key) {
    if (!this.path) return
    const tmp = this.path + '.' + process.pid + '.' + Date.now() + '.tmp'
    try {
      const left = this.readAll().filter((e) => e.key !== key)
      // 不在原文件上 truncate + rewrite。那样进程恰好在两步之间
      // 崩溃时，待办本会变成空文件。先写同目录临时文件再 rename，
      // 读者只会看到旧版或新版。
      writeFileSync(tmp, left.map((e) => JSON.stringify(e)).join('\n')
                         + (left.length ? '\n' : ''),
                    { encoding: 'utf8', mode: 0o600 })
      renameSync(tmp, this.path)
    } catch (e) {
      try { unlinkSync(tmp) } catch { /* 没有临时文件 */ }
      log('[memgarden] outbox 清理失败: ' + e.message + '\n')
    }
  }

  readAll() {
    if (!this.path || !existsSync(this.path)) return []
    try {
      return readFileSync(this.path, 'utf8')
        .split('\n').filter(Boolean)
        .map((line) => { try { return JSON.parse(line) } catch { return null } })
        .filter(Boolean)
    } catch { return [] }
  }
}


export function apply(ctx, config) {
  log('[memgarden] apply 被调用 tenant=' + config.tenant + '\n')

  const locale = config.locale || 'zh-Hans'

  // 🔴 作用域来自可信配置，不来自模型的工具参数。
  //
  // ## owner 从哪来，以及为什么不能省
  //
  // `memoryOwner` 是**这座花园的稳定所有者**，由宿主的可信上下文给。
  // 以前这里把 agent_id 写死成字符串 'dsh'、也没有 owner，后果是同一个
  // tenant 下所有 agent 共用一座花园 —— 「agent-private」名不副实。
  //
  // 拿不到 owner 时**直接关掉记忆功能**，不塞一个默认值：塞了的话，
  // 所有没配 owner 的部署会静默共用一座花园，而这不会有任何报错。
  const memoryOwner = String(config.memoryOwner || config.owner || '').trim()
  if (!memoryOwner) {
    log('[memgarden] 没有配 memoryOwner —— 记忆功能不启用。\n' +
        '            这是有意的：随便给一个默认值会让同一个租户下的\n' +
        '            不同用户/agent 共用一座花园，而且不报错。\n')
    return
  }

  // ⚠️ 子进程在 owner 检查**之后**才起。
  // 反过来的话，一个没配 owner、记忆根本不会启用的部署，仍然会 spawn 一个
  // memgarden 进程、建出一个空库文件，然后因为我们直接 return 而**永远没人
  // 关掉它** —— 泄漏一个进程，还留下一个会让人以为「记忆在工作」的 db 文件。
  const client = new Client(config.bin, config.storage)
  const outbox = new Outbox(config.outboxDir || config.stateDir || '')

  const scopeFor = (agentId, sessionId) => ({
    tenant_id: config.tenant,
    memory_owner_id: memoryOwner,
    actor: {
      user_id: config.tenant,
      // agent / session 是**此刻在执行的是谁**，不是记忆的归属人。
      agent_id: agentId || 'dsh',
      session_id: sessionId || '',
    },
    allowed_mounts: ['agent-private'],
  })
  const scope = scopeFor('dsh', '')

  // 🔴 按 (session, turn) 存状态，不是一个模块级变量。
  //
  // 以前是 `let turnText = ''` —— 一个插件实例共用一个。两个会话同时跑时，
  // A 的 pre-step 会把 B 刚存的文本覆盖掉，于是 B 的落卡记的是 A 说的话。
  // 这类串台不会报错，只会让记忆里出现「用户从没说过的事」。
  const turns = new Map()
  const turnKey = (agent, turn) => sessionIdOf(agent) + '#' + turn

  const ready = client.request('manifest.get', {}).then((m) => {
    if (m.protocol_version !== '1') {
      throw new Error('protocol ' + m.protocol_version + ' 不兼容')
    }
    log('[memgarden] 握手成功 v' + m.component_version +
        ' protocol=' + m.protocol_version + '\n')
    for (const item of (m.storage?.degradations || [])) {
      log('[memgarden] 存储能力降级 ' + item.capability + ': ' +
          item.fallback + '（' + item.cost + '）\n')
    }
    for (const notice of (m.storage?.user_notices || [])) {
      log('[memgarden] 存储能力提示: ' + notice + '\n')
    }
    return m
  }).catch((e) => {
    log('[memgarden] 握手失败: ' + e.message + '\n')
    throw e
  })

  // ---- 崩溃恢复：把上次没做完的落卡补上 --------------------------------- //
  //
  // 幂等键保证「重放不会写两遍」，但得有人去重放。就是这里。
  // 只在启动时跑一次，串行做，做完就把待办划掉。
  void ready.then(async () => {
    const pending = outbox.readAll()
    if (!pending.length) return
    log('[memgarden] 上次有 ' + pending.length + ' 轮落卡没做完，正在补\n')
    for (const entry of pending) {
      try {
        const r = await driveCaptureRaw(ctx, client, entry.scope, entry.locale,
                                        entry.window, entry.key, config)
        log('[memgarden] 补落卡 ' + entry.key + ' written=' + r.written +
            ' error=' + (r.error || '-') + '\n')
        // RPC 成功只说明「服务给了回答」；receipt.error 说明这轮
        // 并没有落库。以前恢复路径不看这一层，会把失败任务划掉，
        // 而正常 turn 路径反而会留下，两条路径语义不一致。
        if (r && !r.error) outbox.done(entry.key)
        else log('[memgarden] 补落卡未落库，保留待办 ' + entry.key + '\n')
      } catch (e) {
        // 补不上就留着，下次启动再试。**不能划掉** —— 划掉等于放弃那条记忆。
        log('[memgarden] 补落卡失败 ' + entry.key + ': ' + e.message + '\n')
      }
    }
  }).catch(() => { /* 握手都没成功时不必补，服务本来就不可用 */ })

  // ---- 每轮自动召回 ---------------------------------------------------- //
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next()
    log('[memgarden] pre-step 命中 kind=' + decision.kind + '\n')
    if (decision.kind !== 'enter') return decision
    try {
      await ready
      const last = payload.messages && payload.messages[payload.messages.length - 1]
      let q = ''
      if (last) {
        if (typeof last.content === 'string') q = last.content
        else if (Array.isArray(last.content)) {
          q = last.content.map((p) => (p && p.text) || '').join(' ')
        }
      }
      if (!q.trim()) { log('[memgarden] 本轮没取到用户文本\n'); return decision }
      const key = turnKey(payload.agent, payload.turn)
      // 一个 turn 里会有多次 pre-step（工具循环）。记忆只在第一次注入；
      // 后面几次的最后一条通常是工具结果，用它再次检索不仅会重复注入，
      // 还会把工具输出误当成用户问题。
      if (turns.has(key)) return decision
      // 记下本轮从第几条消息开始 —— turn-stopping 只渲染这之后的。
      // ⚠️ 减一：pre-step 触发时，用户这一轮的话已经在消息里了。
      turns.set(key, { query: q, agent: payload.agent,
                       from: Math.max(0, messageCount(payload.agent) - 1) })
      const turnScope = scopeFor(agentIdOf(payload.agent),
                                 sessionIdOf(payload.agent))
      const result = await client.request('context.get',
                                          { scope: turnScope, query: q, limit: 5 })
      const blocks = (result.blocks || []).map((b) => b.text).filter(Boolean)
      log('[memgarden] 召回 ' + blocks.length + ' 条\n')
      if (!blocks.length) return decision
      return {
        ...decision,
        messages: [
          ...decision.messages,
          // 🔴 content 必须是**分片数组**，不是字符串。
          // 传字符串时 DSH 内部会 content.some(...) → TypeError，整轮直接失败，
          // 而且报错是 "content.some is not a function" —— 和「记忆注入」
          // 看不出任何关系。类型检查发现不了（我们只有自己抄的最小声明）。
          {
            role: 'user',
            content: [{
              type: 'text',
              text: '[记忆]\n' + blocks.map((b) => '- ' + b).join('\n'),
            }],
          },
        ],
      }
    } catch (e) {
      // 召回失败绝不能挡住这一轮对话 —— 没有记忆也要能聊
      log('[memgarden] context 失败: ' + e.message + '\n')
      return decision
    }
  })

  // ---- 注册模型工具 ---------------------------------------------------- //
  //
  // 自动召回不依赖这些工具（pre-step 每轮都注入），但「模型主动想查一下」
  // 这条路要靠它们。
  //
  // schema 从 Garden 的 tool.list 取，**不在这边手写第二份** —— 手写的那份
  // 会漂，而漂的表现是模型按旧 schema 传参、被拒，看起来像模型出错。
  void ready.then(async () => {
    const tools = await client.request('tool.list', {})
    for (const t of tools) {
      ctx.tools.register({
        // 加命名空间，免得和 DSH 自带的或别的插件撞名
        name: 'memgarden_' + t.name,
        description: t.description,
        parameters: t.parameters,
        output: {
          schema: { type: 'string' },
          render: (_args, value) => [{ type: 'text', text: String(value ?? '') }],
        },
        async execute(args) {
          // 🔴 作用域用插件配置里的 scope，**不读 args 里的任何身份字段**。
          // args 是模型生成的 —— 读它等于让模型自己决定能看谁的记忆。
          const out = await client.request('tool.invoke', {
            scope, name: t.name, arguments: args,
          })
          if (!out.ok) throw new Error(out.error || 'tool failed')
          return out.content || ''
        },
      })
    }
    log('[memgarden] 注册了 ' + tools.length + ' 个工具: ' +
        tools.map((x) => 'memgarden_' + x.name).join(', ') + '\n')
  }).catch((e) => log('[memgarden] 注册工具失败: ' + e.message + '\n'))

  // ---- 轮末自动落卡 ---------------------------------------------------- //
  ctx.on('agent/turn-stopping', (payload) => {
    const key = turnKey(payload.agent, payload.turn)
    const state = turns.get(key)
    turns.delete(key)          // 无论成败都清掉 —— 留着会越攒越多
    log('[memgarden] turn-stopping ' + key +
        ' 有状态=' + Boolean(state) + '\n')
    if (!state) return

    // 🔴 落卡的窗口是**这一轮的完整内容**，不只是 pre-step 存的那句 query。
    // 只记用户问句的话，「助手答应了什么」「工具查到了什么」全都进不了记忆 ——
    // 而那些往往才是这一轮真正值得记的东西。
    const window = renderTurnWindow(payload.agent, state.query, state.from, config)
    if (!window.trim()) return

    const turnScope = scopeFor(agentIdOf(payload.agent),
                               sessionIdOf(payload.agent))

    // 🔴 **返回这个 promise**，让 DSH 等落卡做完。
    //
    // 这个钩子的签名是 `Promise<void> | void` —— 返回 promise 时 DSH 会等。
    // 不返回的话（`void promise`），turn 立刻结束、进程可能随即退出，
    // 落卡在半路被杀掉：**没有报错，只是那条记忆没了**。
    // 实测过：host-driven 要两次往返 + 一次模型调用，比单次 capture.run 慢，
    // 于是这个竞态每次都稳定命中，花园里 0 张卡。
    //
    // 代价是 turn 的结束会等落卡（几秒）。生产上如果不能接受这个延迟，
    // 正确做法是后台跑 + 在 dispose 时 drain，**而不是** fire-and-forget ——
    // 后者在进程退出时必然丢数据。
    // 🔴 **在调模型之前**记进待办本。之后记就白记了 —— 崩溃恰好发生在
    // 模型调用中途时，待办本里什么都没有，那一轮就真的没了。
    const outboxKey = [turnScope.tenant_id, turnScope.memory_owner_id,
                       sessionIdOf(payload.agent) || 'nosession',
                       'turn', payload.turn].join(':')
    outbox.add({ key: outboxKey, scope: turnScope, locale, window,
                 session: sessionIdOf(payload.agent), turn: payload.turn,
                 at: new Date().toISOString() })

    const job = ready
      .then(() => driveCapture(ctx, client, turnScope, locale, window,
                               payload.agent, payload.turn, config))
      .then((r) => {
        log('[memgarden] 落卡 written=' + r.written +
            ' ids=' + JSON.stringify(r.record_ids) +
            ' reason=' + (r.reason || '-') +
            ' error=' + (r.error || '-') + '\n')
        // 「没什么可记」也是**做完了**，要划掉；只有真失败才留着重试。
        if (r.error) {
          const e = new Error('落卡未落库: ' + r.error)
          e.code = r.error
          throw e
        }
        outbox.done(outboxKey)
      })
      // 落卡完成之后才考虑整理 —— 整理要基于最新的花园状态，
      // 和落卡抢同一个 revision 只会白白触发一次 CAS 冲突重算。
      .then(() => maybeTidy(turnScope))
      .catch((e) => log('[memgarden] capture 失败: ' + e.message + '\n'))
      .finally(() => inflight.delete(job))
    inflight.add(job)
    return job
  })

  // ---- 整理调度 -------------------------------------------------------- //
  //
  // 落卡只管往里加，不整理的话花园会越长越乱：同一件事散在五张卡上、
  // 互相矛盾的两张都活着。整理是把它们收敛回去的那一步。
  //
  // 调度放在**轮末之后**，不放定时器：定时器会在用户正在说话时插进来，
  // 和前台读并发；而轮末这个点本来就是空的。
  //
  // 账本在 Garden 那边落库（和卡改动同一次提交），所以这里不需要记任何状态 ——
  // 进程重启、换机器都不会重复整理同一批。
  let tidying = false
  async function maybeTidy(turnScope) {
    if (tidying) return          // 同时只跑一个，别和自己抢
    tidying = true
    try {
      const check = await client.request('maintenance.check', { scope: turnScope })
      if (check.error) throw new Error('整理检查失败: ' + check.error)
      if (!check.needed) return
      log('[memgarden] 该整理了: ' + (check.reason || '-') + '\n')
      // service 是故意不带 model 起的，所以整理也必须像 capture 一样
      // begin/feed，模型调用交还 DSH。调 maintenance.run 会稳定得到
      // model_not_configured，之前的「自动整理」其实从未成功。
      const out = await driveMaintenance(ctx, client, turnScope, locale, config)
      log('[memgarden] 整理结果 written=' + out.written +
          ' reason=' + (out.reason || '-') + ' error=' + (out.error || '-') + '\n')
      if (out.error) throw new Error('整理未落库: ' + out.error)
    } catch (e) {
      // 整理失败**绝不能**影响对话。下一轮还会再试，而账本没推进，
      // 所以这批东西不会被漏掉。
      log('[memgarden] 整理失败: ' + e.message + '\n')
    } finally {
      tidying = false
    }
  }

  // hot reload / 关停时把在飞的落卡等完，但**有上限**。
  // 不等 = 丢记忆；无限等 = 一个卡住的模型调用能让整个进程关不掉。
  ctx.on('dispose', async () => {
    if (!inflight.size) return client.close()
    log('[memgarden] dispose：等 ' + inflight.size + ' 个落卡收尾\n')
    await Promise.race([
      Promise.allSettled([...inflight]),
      new Promise((r) => setTimeout(r, DRAIN_TIMEOUT_MS)),
    ])
    client.close()
  })
}
