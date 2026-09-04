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
export const inject = ['tools']

class Client {
  constructor(bin, storage, model) {
    // 模型命令由宿主给 —— key 归宿主，Garden 不碰。
    // ⚠️ 这仍不是最终形态：sevenfloor §5.4 要的是 host-driven session
    //（DSH 用自己的 provider 调模型），那需要 capture.begin/feed。
    const args = ['serve', '--storage', storage]
    if (model) args.push('--model', model)
    this.child = spawn(bin, args, {
      stdio: ['pipe', 'pipe', 'pipe'],
    })
    this.pending = new Map()
    this.next = 1
    this.buf = ''
    this.child.stdout.setEncoding('utf8')
    this.child.stdout.on('data', (c) => this.onData(c))
    this.child.stderr.setEncoding('utf8')
    this.child.stderr.on('data', (c) => log('[memgarden:py] ' + c))
    this.child.on('exit', (code) => {
      log('[memgarden] 服务退出 code=' + code + '\n')
      for (const [, r] of this.pending) {
        r({ ok: false, error: { code: 'service_exited', message: 'code=' + code } })
      }
      this.pending.clear()
    })
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

export function apply(ctx, config) {
  log('[memgarden] apply 被调用 tenant=' + config.tenant + '\n')

  const client = new Client(config.bin, config.storage, config.model)
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

  // ---- 轮末自动落卡 ---------------------------------------------------- //
  ctx.on('agent/turn-stopping', (payload) => {
    log('[memgarden] turn-stopping turn=' + payload.turn +
        ' 文本长度=' + turnText.length + '\n')
    const text = turnText
    if (!text.trim()) return
    void ready
      .then(() => client.request('capture.run', {
        scope,
        window: '用户：' + text,
        locale,
        // 稳定幂等键：崩溃后重放同一轮不会写第二遍
        idempotency_key: scope.tenant_id + ':dsh:' + payload.turn,
      }))
      .then((r) => log('[memgarden] 落卡 written=' + r.written +
                       ' ids=' + JSON.stringify(r.record_ids) +
                       ' ' + (r.reason || '') + '\n'))
      .catch((e) => log('[memgarden] capture 失败: ' + e.message + '\n'))
  })
}
