/**
 * ⚠️ 未经真实 DeepSeek Harness 验证 —— 见 README。
 *
 * 按 sevenfloor 2026-09-02 §8.2 的标准，「兼容完成」需要固定版本 + 可重复演示 +
 * 端到端证据。这个包现在**一样都没有**，它只是把接线形状定下来。
 */
export { MemGardenClient, EXPECTED_PROTOCOL_VERSION } from './client.js';
export type { Scope, RpcError, RpcResponse, Manifest, ClientOptions } from './client.js';
export { DshMemGardenAdapter, scopeOf } from './adapter.js';
export type { DshSession, AdapterOptions } from './adapter.js';
