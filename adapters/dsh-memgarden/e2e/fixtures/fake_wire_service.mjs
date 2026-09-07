#!/usr/bin/env node
/**
 * Adapter 离线验收用的最小 wire service。
 *
 * 它不复制 Garden 的任何判断，只记下 Adapter 真正发了哪些 RPC，
 * 再给出能推动 begin/feed 状态机的固定回答。这样 CI 不需要 DSH
 * 和真模型，也能防止 Adapter 又退回 maintenance.run 或误删 outbox。
 */
import { appendFileSync } from 'node:fs'
import readline from 'node:readline'

const requestLog = process.env.MEMGARDEN_FAKE_REQUEST_LOG || ''
const captureError = process.env.MEMGARDEN_FAKE_CAPTURE_ERROR || ''

function record(value) {
  if (requestLog) appendFileSync(requestLog, JSON.stringify(value) + '\n')
}

function reply(id, result) {
  process.stdout.write(JSON.stringify({ id, ok: true, result }) + '\n')
}

const rl = readline.createInterface({ input: process.stdin })
rl.on('line', (line) => {
  const request = JSON.parse(line)
  const { id, method, params = {} } = request
  record({ method, params })

  if (method === 'manifest.get') {
    reply(id, { protocol_version: '1', component_version: 'test' })
  } else if (method === 'tool.list') {
    reply(id, [])
  } else if (method === 'context.get') {
    reply(id, { blocks: [] })
  } else if (method === 'capture.begin') {
    reply(id, { session_id: 'capture-1', status: 'needs_model',
                next_prompt: 'capture prompt' })
  } else if (method === 'capture.feed') {
    reply(id, { status: 'completed', result: {
      written: captureError ? 0 : 1,
      record_ids: captureError ? [] : ['memory-1'],
      error: captureError,
    } })
  } else if (method === 'capture.cancel') {
    reply(id, { cancelled: true })
  } else if (method === 'maintenance.check') {
    reply(id, { needed: true, reason: 'offline-test' })
  } else if (method === 'maintenance.begin') {
    reply(id, { session_id: 'maintenance-1', status: 'needs_model',
                next_prompt: 'maintenance prompt' })
  } else if (method === 'maintenance.feed') {
    reply(id, { status: 'completed', result: {
      written: 1, record_ids: ['dream-1'], error: '',
    } })
  } else if (method === 'maintenance.cancel') {
    reply(id, { cancelled: true })
  } else {
    process.stdout.write(JSON.stringify({ id, ok: false,
      error: { code: 'unknown_method', message: method } }) + '\n')
  }
})
