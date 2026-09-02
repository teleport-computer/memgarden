/**
 * DSH 的最小类型声明。
 *
 * ⚠️ **这不是真实类型**，是从 dsh-v0.1.2-alpha.4 的源码里抄出来的最小子集，
 * 只为了让这个包能独立做类型检查。
 *
 * 真正接入时删掉这个文件，改成依赖 workspace 里的真包 —— 那些包没有发到
 * npm（`@deepseek-ai/dsh-*` 是 monorepo 内部包），所以要么把 Adapter 放进
 * 他们的 workspace，要么等他们发布。
 *
 * 留着这份声明的风险很实在：它和真类型漂了不会报错，只会在真正接入的那天
 * 一次性炸出来。所以**接入的第一步就是删掉它**。
 */
declare module '@deepseek-ai/cordis' {
  export interface Context {
    on(event: string, listener: (...args: any[]) => any): void;
    tools: { register(tool: unknown): void };
    logger?: (name: string) => {
      warn?: (...args: unknown[]) => void;
      error?: (...args: unknown[]) => void;
    };
  }
}

declare module '@deepseek-ai/dsh-tools' {
  export function defineTool(spec: unknown): unknown;
}
