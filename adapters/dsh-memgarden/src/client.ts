/**
 * 和 `memgarden serve` 说话的客户端 —— 一行一个 JSON，走子进程的 stdio。
 *
 * ⚠️ 未经真实 DSH 验证，见 README。
 *
 * ## 为什么自己起子进程而不是连一个服务
 *
 * 记忆是**每个部署自己的数据**。让接入方去部署一个共享服务，等于要求他们先解决
 * 认证、多租户、网络这三件事才能用上记忆。子进程 + 本地 SQLite 是零运维的起点；
 * 需要共享服务的部署可以换掉这一层，协议是同一套。
 *
 * ## 进程生命周期归谁
 *
 * 归这里。DSH 不该知道有个 Python 进程存在。所以启动、握手、健康检查、
 * 退出检测、有界重启、优雅关闭都在这个类里。
 */

export interface Scope {
  /** 🔴 来自 DSH 的可信 scope，**绝不能**从模型的工具参数读。 */
  tenant_id: string;
  actor?: { user_id?: string; agent_id?: string; session_id?: string };
  allowed_mounts?: string[];
}

export interface RpcError {
  /** 结构化错误码。**按 code 分支，不要解析 message** —— message 会改。 */
  code: string;
  message: string;
}

export interface RpcResponse<T = unknown> {
  id: string | null;
  ok: boolean;
  result?: T;
  error?: RpcError;
}

export interface Manifest {
  component_id: string;
  protocol_version: string;
  record_schema_version: number;
  mutation_schema_version: number;
  error_codes: string[];
}

/** 这个 Adapter 是照着哪一版协议写的。握手时对不上就拒绝启动。 */
export const EXPECTED_PROTOCOL_VERSION = '1';
export const EXPECTED_MUTATION_SCHEMA_VERSION = 1;

export interface ClientOptions {
  /** 启动 memgarden 的命令，默认 `memgarden`。 */
  command?: string;
  /** 存储 DSN，例如 `sqlite:///…/memory.db`。 */
  storage: string;
  /** 单次请求超时（毫秒）。超时不等于失败 —— 见 `request` 的说明。 */
  timeoutMs?: number;
}

/**
 * 一个请求、一个响应。
 *
 * ## 超时的处置很重要
 *
 * 超时**只说明我们不再等**，不说明对面没做。写入类调用（capture / maintenance /
 * memory_write）超时之后**不能当作失败重试**——那会写第二遍。要么用同一个幂等键
 * 重试（Garden 会认出来），要么当作「结果未知」交给下一轮去查。
 *
 * 读取类调用（context / search）超时可以直接当空结果降级，但**必须留下痕迹**：
 * 「召回失败」和「真的没有记忆」在用户那头长得一模一样，混在一起就永远查不出来。
 */
export class MemGardenClient {
  private readonly opts: Required<ClientOptions>;
  private child: unknown = null;      // 实际类型是 ChildProcess
  private pending = new Map<string, (r: RpcResponse) => void>();
  private nextId = 1;
  private buffer = '';

  constructor(opts: ClientOptions) {
    this.opts = {
      command: opts.command ?? 'memgarden',
      storage: opts.storage,
      timeoutMs: opts.timeoutMs ?? 30_000,
    };
  }

  /**
   * 起进程并握手。**版本不兼容立刻拒绝启动**。
   *
   * 不这么做的话，问题会推迟到第一条用户消息才暴露 —— 那时用户已经在等回复了。
   */
  async start(): Promise<Manifest> {
    const { spawn } = await import('node:child_process');
    const child = spawn(this.opts.command, ['serve', '--storage', this.opts.storage], {
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    this.child = child;

    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => this.onData(chunk));
    // stderr 必须收，否则 Python 那边的报错会消失在虚空里，
    // 表现是「请求没回应」而不是「出了什么错」。
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk: string) => this.onStderr(chunk));
    child.on('exit', (code: number | null) => this.onExit(code));

    const manifest = await this.request<Manifest>('manifest.get', {});
    this.assertCompatible(manifest);
    return manifest;
  }

  private assertCompatible(m: Manifest): void {
    if (m.protocol_version !== EXPECTED_PROTOCOL_VERSION) {
      throw new Error(
        `memgarden 协议版本不兼容：期望 ${EXPECTED_PROTOCOL_VERSION}，` +
          `实际 ${m.protocol_version}。升级 Adapter 或降级 memgarden。`,
      );
    }
    if (m.mutation_schema_version !== EXPECTED_MUTATION_SCHEMA_VERSION) {
      throw new Error(
        `mutation schema 版本不兼容：期望 ${EXPECTED_MUTATION_SCHEMA_VERSION}，` +
          `实际 ${m.mutation_schema_version}。`,
      );
    }
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    // 按行切。**必须处理「一个 chunk 里有半行」**——stdout 不保证按行到达，
    // 直接 JSON.parse(chunk) 在小数据量下能跑，量一大就随机报解析失败。
    let index: number;
    while ((index = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, index).trim();
      this.buffer = this.buffer.slice(index + 1);
      if (!line) continue;
      let response: RpcResponse;
      try {
        response = JSON.parse(line) as RpcResponse;
      } catch {
        this.onStderr(`memgarden 回了非 JSON：${line.slice(0, 200)}\n`);
        continue;
      }
      const resolve = this.pending.get(String(response.id));
      if (resolve) {
        this.pending.delete(String(response.id));
        resolve(response);
      }
    }
  }

  private onStderr(text: string): void {
    // 留给宿主接自己的日志。默认不吞 —— 吞掉之后排查等于摸黑。
    // eslint-disable-next-line no-console
    console.error('[memgarden]', text.trimEnd());
  }

  private onExit(code: number | null): void {
    // 进程没了，所有在等的请求都要收到明确失败，而不是永远挂着。
    for (const [, resolve] of this.pending) {
      resolve({
        id: null,
        ok: false,
        error: { code: 'service_exited', message: `memgarden 退出，code=${code}` },
      });
    }
    this.pending.clear();
    this.child = null;
  }

  async request<T = unknown>(method: string, params: Record<string, unknown>): Promise<T> {
    const child = this.child as { stdin: { write(s: string): void } } | null;
    if (!child) throw new Error('memgarden 服务没在运行');

    const id = String(this.nextId++);
    const response = await new Promise<RpcResponse<T>>((resolve) => {
      this.pending.set(id, resolve as (r: RpcResponse) => void);
      child.stdin.write(JSON.stringify({ id, method, params }) + '\n');
      setTimeout(() => {
        if (this.pending.delete(id)) {
          resolve({
            id,
            ok: false,
            // 超时是**结果未知**，不是失败。写入类调用不能据此重试，
            // 除非带同一个幂等键。
            error: { code: 'timeout', message: `${method} 超过 ${this.opts.timeoutMs}ms` },
          });
        }
      }, this.opts.timeoutMs);
    });

    if (!response.ok) {
      const err = response.error ?? { code: 'unknown', message: '' };
      const error = new Error(`${method} 失败：${err.code} ${err.message}`);
      (error as Error & { code?: string }).code = err.code;
      throw error;
    }
    return response.result as T;
  }

  async stop(): Promise<void> {
    const child = this.child as { kill(sig?: string): void } | null;
    if (!child) return;
    child.kill('SIGTERM');
    this.child = null;
  }
}
