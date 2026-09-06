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
import { appendFileSync } from 'node:fs'

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
    this.spawn()
  }

  spawn() {
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
    for (const [, r] of this.pending) r({ ok: false, error: { code, message } })
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
        const r = this.pending.get(String(res.id))
        if (r) { this.pending.delete(String(res.id)); r(res) }
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
      this.pending.set(id, resolve)
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
      setTimeout(() => {
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
  let state = await client.request('capture.begin', {
    scope,
    window: '用户：' + text,
    locale,
    // 稳定幂等键：崩溃后重放同一轮不会写第二遍。**它不依赖会话还在** ——
    // 进程重启会丢掉在途会话，但重放同一轮仍然不会写出第二条记忆。
    // 🔴 幂等身份必须包含 tenant + owner + session + turn。
    //
    // 以前是 `tenant + ':dsh:' + turn` —— 两个会话都从 turn 1 开始时会**撞
    // 同一个键**，于是第二个会话的第一轮被当成第一个会话的重放，
    // 那一轮什么都不会写，而且回的是「成功」。
    idempotency_key: [scope.tenant_id, scope.memory_owner_id,
                      sessionIdOf(agent) || 'nosession',
                      'turn', turn].join(':'),
  })

  let rounds = 0
  while (state.status === 'needs_model') {
    // 有上限：模型一直吐脏东西时不能无限重问下去，那会烧光额度。
    if (++rounds > 4) {
      await client.request('capture.cancel', { session_id: state.session_id })
      throw new Error('落卡重问超过 4 轮，放弃')
    }
    const reply = await callModel(ctx, config, state.next_prompt)
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

/** 用 DSH 的 llm 服务发一次一次性请求，把流拼成完整文本。 */
async function callModel(ctx, config, prompt) {
  const chunks = []
  const stream = ctx.llm.stream({
    provider: config.provider || 'deepseek-official',
    model: config.captureModel || config.model || 'deepseek-v4-flash',
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
  if (!out.trim()) {
    // 空回复要当失败报出来。当成正常结果喂回去的话，Garden 会解析失败，
    // 而调用方看到的是「没什么值得记」—— 和真的没内容分不开。
    throw new Error('模型返回空（finish=' + JSON.stringify(finish) + '）')
  }
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
function renderTurnWindow(agent, query) {
  const lines = []
  try {
    const msgs = agent?.session?.surface?.messages
                 || agent?.session?.messages || []
    for (const m of msgs.slice(-12)) {
      const role = String(m?.role || '')
      const text = textOf(m?.content)
      if (!text.trim()) continue
      if (role === 'user') lines.push('用户：' + text)
      else if (role === 'assistant') lines.push('助手：' + text)
      else if (role === 'tool') lines.push('工具结果：' + text.slice(0, 500))
    }
  } catch (e) {
    log('[memgarden] 取本轮消息失败，退回只用用户问句: ' + e.message + '\n')
  }
  if (!lines.length && query) lines.push('用户：' + query)
  return lines.join('\n')
}

function textOf(content) {
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.map((p) => (p && p.text) || '').join(' ')
  }
  return ''
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
    return m
  }).catch((e) => {
    log('[memgarden] 握手失败: ' + e.message + '\n')
    throw e
  })

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
      // 只在这一轮**第一次** pre-step 时记下用户输入。一个 turn 里会有
      // 多次 pre-step（工具循环），后面几次的最后一条消息是工具结果，
      // 拿它当「用户说的话」会把工具输出记成用户的原话。
      if (!turns.has(key)) turns.set(key, { query: q, agent: payload.agent })
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
    const window = renderTurnWindow(payload.agent, state.query)
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
    const job = ready
      .then(() => driveCapture(ctx, client, turnScope, locale, window,
                               payload.agent, payload.turn, config))
      .then((r) => log('[memgarden] 落卡 written=' + r.written +
                       ' ids=' + JSON.stringify(r.record_ids) +
                       ' reason=' + (r.reason || '-') +
                       ' error=' + (r.error || '-') + '\n'))
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
      if (!check.needed) return
      log('[memgarden] 该整理了: ' + (check.reason || '-') + '\n')
      const out = await client.request('maintenance.run', {
        scope: turnScope, locale,
        ai_name: config.aiName || '', user_name: config.userName || '',
      })
      log('[memgarden] 整理结果 written=' + out.written +
          ' reason=' + (out.reason || '-') + ' error=' + (out.error || '-') + '\n')
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
