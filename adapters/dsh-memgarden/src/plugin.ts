/**
 * DeepSeek Harness 插件 —— 对着**真实 API** 写的接线。
 *
 * 核对基线：`dsh-v0.1.2-alpha.4`，commit `4e84901e6471b79ec0338099867ebb4606d12bb5`
 * （和 sevenfloor 2026-09-02 文档里 pin 的那个一致，已从公开仓库拉下来核对）。
 *
 * ## 已核对属实的部分
 *
 *     包名        @deepseek-ai/dsh-*        （不是 @deepseek/harness —— 那是我先前猜错的）
 *     注入上下文  @deepseek-ai/cordis 的 Context
 *     工具注册    ctx.tools.register(defineTool({...}))
 *     每轮召回    'agent/pre-step'  waterfall，返回 PreStepDecision
 *                 { kind: 'enter', messages } —— 往 messages 里追加就是注入
 *     轮末落卡    'agent/turn-stopping'  payload: { agent, turn, signal }
 *
 * ## 仍未验证的部分
 *
 * 没有在真实 DSH 上跑起来过（要装整个 pnpm workspace + 模型 key）。所以：
 *
 *   - 从 Agent/Session 取 profileId / userId 的**具体字段名**还是推测的；
 *   - 端到端那 16 步（sevenfloor §8.2）一步都没跑；
 *   - 失败路径那 12 项（子进程不存在、握手不兼容、hot reload…）都没试过。
 *
 * 按他的标准，这**不算兼容完成**。这个文件的价值是：接线形状现在是照着真实
 * 签名写的，接手的人不用从零猜。
 */

import type { Context } from '@deepseek-ai/cordis';
import { DshMemGardenAdapter, type DshSession } from './adapter.js';

export const name = 'memgarden';

/** 这个插件要用到 DSH 的哪些服务。 */
export const inject = ['tools'];

export interface PluginConfig {
  /** 存储 DSN，例如 `sqlite:///…/memory.db`。 */
  storage: string;
  /** 花园用什么语言写卡。**必填** —— Garden 不猜。 */
  locale: string;
  /** 每轮带几张记忆。 */
  contextLimit?: number;
}

/**
 * DSH 的 Agent → Garden 的会话身份。
 *
 * ⚠️ 这里的字段名是**推测**：真实的 Agent 上叫什么还没核对过（要跑起来看）。
 * 接手时第一件事就是把这个函数对准，它是整条链路的身份来源。
 *
 * 🔴 但有一条是确定的：这些值必须来自 DSH 的可信 scope，**绝不能**从模型的
 * 工具参数读 —— 那是模型生成的文本，读它等于让模型自己决定能看谁的记忆。
 */
function sessionOf(agent: { id?: string; profileId?: string; userId?: string },
                   turn: number): DshSession {
  return {
    profileId: agent.profileId ?? 'default',
    agentId: agent.id ?? 'agent',
    userId: agent.userId,
    sessionId: `${agent.id ?? 'agent'}:${turn}`,
  };
}

export function apply(ctx: Context, config: PluginConfig): void {
  const adapter = new DshMemGardenAdapter({
    storage: config.storage,
    locale: config.locale,
    contextLimit: config.contextLimit,
  });

  // 启动即握手。版本不兼容立刻抛 —— 否则问题会推迟到第一条用户消息才暴露，
  // 那时用户已经在等回复了。
  const ready = adapter.start();

  // ---- 每轮自动召回 -------------------------------------------------- //
  //
  // 这是 waterfall：先 await next() 拿到机器本来要用的 messages，再往里追加。
  // **不能**自己造一份 messages 返回 —— 那会把别的插件加的东西丢掉。
  ctx.on('agent/pre-step', async (payload, next) => {
    const decision = await next();
    if (decision.kind !== 'enter') return decision;

    try {
      await ready;
      const latest = payload.messages.at(-1);
      const query = typeof latest?.content === 'string' ? latest.content : '';
      if (!query) return decision;

      const { blocks, degraded } = await adapter.contextForTurn(
        sessionOf(payload.agent as never, payload.turn), query,
      );
      if (degraded) {
        // 「召回超时」和「真的没有记忆」在用户那头一模一样。不留痕的话
        // 「它忘了我说过什么」这类反馈永远查不出是哪一种。
        ctx.logger?.('memgarden').warn?.('context degraded to empty (timeout)');
      }
      if (blocks.length === 0) return decision;

      return {
        ...decision,
        messages: [
          ...decision.messages,
          { role: 'user', content: `[记忆]\n${blocks.map((b) => `- ${b}`).join('\n')}` },
        ] as typeof decision.messages,
      };
    } catch (error) {
      // 召回失败**绝不能**挡住这一轮对话 —— 没有记忆也要能聊。
      ctx.logger?.('memgarden').error?.(error);
      return decision;
    }
  });

  // ---- 轮末落卡 ------------------------------------------------------ //
  //
  // 发出去就返回，不等它完成：落卡要调模型、几秒起步，让用户的回复等它
  // 等于把记忆的成本转嫁到每一次对话延迟上。
  ctx.on('agent/turn-stopping', (payload) => {
    void ready.then(() => {
      const session = sessionOf(payload.agent as never, payload.turn);
      // ⚠️ 这一轮的可 capture 文本从哪取还没核对（Agent 上的消息访问方式）。
      // 接手时要对准 —— 取错了表现是「什么都没记住」，且不报错。
      adapter.captureTurn(session, '', String(payload.turn));
    });
  });

  // ---- 模型工具 ------------------------------------------------------ //
  //
  // 工具 schema 从 Garden 取，**不在这边手写第二份** —— 手写的会漂。
  void ready.then(async () => {
    const { defineTool } = await import('@deepseek-ai/dsh-tools');
    for (const definition of await adapter.toolDefinitions()) {
      const def = definition as { name: string; description: string; parameters: unknown };
      ctx.tools.register(defineTool({
        // 加命名空间，免得和 DSH 自带的或别的插件撞名。
        name: `memgarden_${def.name}`,
        description: def.description,
        parameters: def.parameters as never,
        async execute(args: Record<string, unknown>, exec: { agent?: unknown; turn?: number }) {
          // 🔴 作用域从执行上下文推，不从 args 读。
          const session = sessionOf((exec.agent ?? {}) as never, exec.turn ?? 0);
          return adapter.invokeTool(session, def.name, args);
        },
      } as never));
    }
  });

  ctx.on('dispose', () => {
    // 关之前把在飞的写入等完，否则刚落的那张卡会丢。
    void adapter.stop();
  });
}
