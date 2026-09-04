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
    // 🔴 **不给服务任何模型配置**。模型调用归 DSH —— 它持有 provider、
    // key、路由、用量统计、超时、取消、重试。让 Garden 另开一条 DSH 管不着的
    // 通道，后果很具体：用户按「停止」时那次落卡的调用停不下来，也不计入用量。
    this.child = spawn(bin, ['serve', '--storage', storage], {
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.dead = null
    this.pending = new Map()
    // 服务死后仍可能有一次写入落在管子上 —— EPIPE 同样会升级成 unhandled
    this.child.stdin.on('error', (err) => {
      log('[memgarden] 写入服务失败: ' + err.message + '\n')
    })
    this.next = 1
    this.buf = ''
    this.child.stdout.setEncoding('utf8')
    this.child.stdout.on('data', (c) => this.onData(c))
    this.child.stderr.setEncoding('utf8')
    this.child.stderr.on('data', (c) => log('[memgarden:py] ' + c))
    // 🔴 spawn 的 'error' 事件没人监听时，Node 会把它升级成 unhandled error
    // **直接杀掉整个进程** —— memgarden 没装、路径写错、没有执行权限，
    // 后果都是「用户的 agent 起不来」，而报错和记忆看不出关系。
    // 记忆是增强，不是依赖：挂了就退化成没有记忆，不能拖垮宿主。
    this.child.on('error', (err) => {
      log('[memgarden] 服务起不来: ' + err.message + '\n')
      this.dead = err
      this.failAll('service_unavailable', err.message)
    })
    this.child.on('exit', (code) => {
      log('[memgarden] 服务退出 code=' + code + '\n')
      this.dead = this.dead || new Error('服务已退出 code=' + code)
      this.failAll('service_exited', 'code=' + code)
    })
  }

  /** 把所有在途请求就地失败掉，避免调用方一直挂到超时。 */
  failAll(code, message) {
    for (const [, r] of this.pending) r({ ok: false, error: { code, message } })
    this.pending.clear()
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
      this.child.stdin.write(JSON.stringify({ id, method, params }) + '\n')
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
async function driveCapture(ctx, client, scope, locale, text, turn, config) {
  let state = await client.request('capture.begin', {
    scope,
    window: '用户：' + text,
    locale,
    // 稳定幂等键：崩溃后重放同一轮不会写第二遍。**它不依赖会话还在** ——
    // 进程重启会丢掉在途会话，但重放同一轮仍然不会写出第二条记忆。
    idempotency_key: scope.tenant_id + ':dsh:' + turn,
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
      reply,
      // 是否被截断只有拿到原始响应元数据的这一层看得见，内核看不到
      truncated: reply.truncated === true,
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
  if (!out.trim()) {
    // 空回复要当失败报出来。当成正常结果喂回去的话，Garden 会解析失败，
    // 而调用方看到的是「没什么值得记」—— 和真的没内容分不开。
    throw new Error('模型返回空（finish=' + JSON.stringify(finish) + '）')
  }
  return out
}

export function apply(ctx, config) {
  log('[memgarden] apply 被调用 tenant=' + config.tenant + '\n')

  const client = new Client(config.bin, config.storage)
  // 🔴 作用域来自可信配置，不来自模型的工具参数
  const scope = {
    tenant_id: config.tenant,
    actor: { user_id: config.tenant, agent_id: 'dsh' },
    allowed_mounts: ['agent-private'],
  }
  const locale = config.locale || 'zh-Hans'
  let turnText = ''

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
      turnText = q
      const result = await client.request('context.get', { scope, query: q, limit: 5 })
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
    log('[memgarden] turn-stopping turn=' + payload.turn +
        ' 文本长度=' + turnText.length + '\n')
    const text = turnText
    if (!text.trim()) return

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
    return ready
      .then(() => driveCapture(ctx, client, scope, locale, text, payload.turn, config))
      .then((r) => log('[memgarden] 落卡 written=' + r.written +
                       ' ids=' + JSON.stringify(r.record_ids) +
                       ' reason=' + (r.reason || '-') +
                       ' error=' + (r.error || '-') + '\n'))
      .catch((e) => log('[memgarden] capture 失败: ' + e.message + '\n'))
  })
}
