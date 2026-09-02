/**
 * 把 Memory Garden 接到 DeepSeek Harness 的生命周期上。
 *
 * ⚠️ 未经真实 DSH 验证 —— hook 名和 API 形状照着公开文档写，实际接的时候
 * 大概率要改这一层（**只该改这一层**）。见 README。
 *
 * ## 这个文件做什么、不做什么
 *
 *     做      DSH 的 scope → Garden 的 Scope；生命周期 hook → Garden 调用；
 *             Garden 的工具定义 → DSH 的 Tool Registry
 *     不做    任何提示词、解析、挑卡、整理的判断 —— 那些一律回到 Python
 *
 * 复制一份判断逻辑过来最省事，也最致命：两边会慢慢漂，而漂了不报错，
 * 只会表现为「同样的对话，DSH 上记的东西和别处不一样」。
 */

import { MemGardenClient, Scope } from './client.js';

/** DSH 那边的会话上下文。字段名待真实接入时对齐。 */
export interface DshSession {
  profileId: string;
  agentId: string;
  userId?: string;
  sessionId: string;
}

export interface AdapterOptions {
  storage: string;
  /** 花园用什么语言写卡。**必填** —— Garden 不猜，猜错就是整个花园换语言。 */
  locale: string;
  /** 每轮带几张记忆。太多会挤掉真正的对话内容。 */
  contextLimit?: number;
  /** 召回超时（毫秒）。超时降级为空，但会记一条 trace。 */
  contextTimeoutMs?: number;
}

/**
 * DSH 的身份 → Garden 的作用域。
 *
 * 🔴 这些值**全部**来自 DSH 的可信 scope。一个都不能从模型的工具参数读 ——
 * 工具参数是模型生成的文本，读它等于让模型自己决定能看谁的记忆。
 */
export function scopeOf(session: DshSession): Scope {
  return {
    // 一个部署一个租户。多部署共用一个库时靠这个隔离。
    tenant_id: session.profileId,
    actor: {
      user_id: session.userId ?? '',
      agent_id: session.agentId,
      session_id: session.sessionId,
    },
    // 第一阶段只有 agent-private。shared 类 mount 的语义还没定，
    // **不能**先放开再补规则 —— 那期间写进去的数据没法追溯该归谁。
    allowed_mounts: ['agent-private'],
  };
}

export class DshMemGardenAdapter {
  private readonly client: MemGardenClient;
  private readonly opts: Required<AdapterOptions>;
  /** 每个 mount 上的写入串行化 —— 见 `captureTurn`。 */
  private writeChain = new Map<string, Promise<unknown>>();

  constructor(opts: AdapterOptions) {
    this.opts = {
      contextLimit: 8,
      contextTimeoutMs: 1_500,
      ...opts,
    };
    this.client = new MemGardenClient({ storage: opts.storage });
  }

  async start(): Promise<void> {
    await this.client.start();   // 版本不兼容会在这里抛
  }

  async stop(): Promise<void> {
    // 关之前把在飞的写入等完 —— 否则刚落的那张卡会丢。
    await Promise.allSettled([...this.writeChain.values()]);
    await this.client.stop();
  }

  // ---------------------------------------------------------------- 每轮召回

  /**
   * 每次请求模型之前调。**自动带上相关记忆，不依赖模型记得调工具。**
   *
   * 召回失败 ≠ 没有记忆。超时降级成空，但要留痕：这两种情况在用户那头
   * 长得一模一样（「它忘了我说过什么」），混在一起就永远查不出是哪一种。
   */
  async contextForTurn(
    session: DshSession,
    userText: string,
  ): Promise<{ blocks: string[]; degraded: boolean }> {
    try {
      const result = await this.client.request<{ blocks: { text: string }[] }>(
        'context.get',
        {
          scope: scopeOf(session),
          query: userText,
          limit: this.opts.contextLimit,
        },
      );
      return { blocks: result.blocks.map((b) => b.text).filter(Boolean), degraded: false };
    } catch (error) {
      const code = (error as Error & { code?: string }).code;
      if (code === 'timeout') {
        // 明确标成降级 —— 调用方应当把它写进可重放的 session log。
        return { blocks: [], degraded: true };
      }
      throw error;
    }
  }

  // ---------------------------------------------------------------- 轮末落卡

  /**
   * 一轮稳定结束之后调。
   *
   * ## 不等它完成
   *
   * 落卡要调模型，几秒起步。让用户的回复等它，等于把记忆的成本转嫁到每一次
   * 对话延迟上。所以这里发出去就返回。
   *
   * ## 但同一个 agent 的写入要串行
   *
   * 两轮几乎同时结束时，并发落卡会各自读到旧的花园快照，然后写出重复的卡
   * （两边都觉得「这件事还没记过」）。串行化的代价是第二轮多等一会儿，
   * 比重复记忆便宜。
   *
   * ## 幂等键要稳定
   *
   * 用 `(租户, agent, session, turn)` —— 崩溃后重放同一个 turn 不会写第二遍。
   * 用时间戳或随机数的话，每次重试都是一条新记忆。
   */
  captureTurn(session: DshSession, window: string, turnId: string): void {
    const scope = scopeOf(session);
    const key = `${scope.tenant_id}:${session.agentId}`;
    const previous = this.writeChain.get(key) ?? Promise.resolve();

    const next = previous
      .catch(() => undefined)   // 前一轮失败不该卡住后面所有轮
      .then(() =>
        this.client.request('capture.run', {
          scope,
          window,
          locale: this.opts.locale,
          idempotency_key: `${scope.tenant_id}:${session.agentId}:${session.sessionId}:${turnId}`,
        }),
      )
      .catch((error) => {
        // 落卡失败**不能**影响对话。但要能被看见 —— 静默失败的表现是
        // 「它什么都不记得了」，而没有任何线索指向这里。
        // eslint-disable-next-line no-console
        console.error('[memgarden] capture 失败', error);
      });

    this.writeChain.set(key, next);
  }

  // ---------------------------------------------------------------- 模型工具

  /**
   * Garden 的工具定义 → DSH 的 Tool Registry。
   *
   * schema 从 Garden 取，**不在这边手写第二份** —— 手写的那份会漂。
   */
  async toolDefinitions(): Promise<unknown[]> {
    return this.client.request<unknown[]>('tool.list', {});
  }

  /**
   * 执行工具。
   *
   * 🔴 作用域用 `session` 推出来的，**不用模型传进来的参数**。
   * 模型可以在 arguments 里写任何东西，包括别人的 tenant。
   */
  async invokeTool(
    session: DshSession,
    name: string,
    args: Record<string, unknown>,
  ): Promise<unknown> {
    return this.client.request('tool.invoke', {
      scope: scopeOf(session),
      name,
      arguments: args,
    });
  }

  // ---------------------------------------------------------------- 整理

  /**
   * 定时问一句要不要整理。**先 check 再 run** —— check 不调模型。
   *
   * 「什么时候触发」归 DSH 的 scheduler，「要不要整理、怎么整理」归 Garden。
   * 两者混在一起的话，换一个调度器就得重写整理逻辑。
   */
  async maybeTidy(session: DshSession): Promise<'skipped' | 'done' | 'failed'> {
    const scope = scopeOf(session);
    const check = await this.client.request<{ needed: boolean }>('maintenance.check', {
      scope,
    });
    if (!check.needed) return 'skipped';
    try {
      await this.client.request('maintenance.run', { scope, locale: this.opts.locale });
      return 'done';
    } catch {
      return 'failed';
    }
  }
}
