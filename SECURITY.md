# Security / 安全与隐私

Memory Garden processes sensitive user memories. It is a plaintext library, not an authentication service or encrypted vault.

## Reporting a vulnerability

Do not publish exploit details, credentials, or user data in a public Issue or PR. If GitHub offers **Security → Report a vulnerability**, use that private channel. If unavailable, open an Issue requesting a private reporting channel **without technical details or sensitive attachments**, and wait for a maintainer to establish one before sending the report. No response-time SLA is promised by this repository.

报告时准备版本/commit、受影响入口、预期与实际行为和合成数据最小复现。未获维护者确认前，不在公开位置发布可被利用的细节。泄露凭据应在提供方撤销或轮换；删掉消息或 Git 文件不能使旧凭据重新安全。

## Integrator responsibilities

- Construct tenant, owner and allowed mounts from trusted authentication state, not model-generated arguments. Low-level Store/scoring functions do not authenticate callers.
- Treat recalled text, imports and model output as untrusted data. Memories must not override system instructions or tool authorization.
- Keep credentials out of prompts, cards, logs and fixtures. Hosts own model/network scope, budgets, timeouts and cancellation.
- Protect plaintext databases, backups and outboxes using deployment access controls. Outboxes contain conversation material; retrieval traces can contain titles/matched words and need redaction outside your trust boundary.
- Coordinate deletion across host sources, cards, indexes, caches and backups; deleting one card does not erase all derived copies.
- Pass only currently authorized, active candidates to low-level retrieval; keep vectors and model/projection versions consistent with those cards.

There is currently no separate long-term-support/security backport matrix. Reports should identify the exact release; maintainers determine affected versions and remediation. See [release evidence](docs/STATUS.md) and [integration boundaries](docs/INTEGRATION-AND-DATA.md).
