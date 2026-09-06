#!/usr/bin/env node
/**
 * 把这个 Adapter 装进一个 DSH profile —— **一条命令，不用手工连 symlink**。
 *
 *     npx dsh-memgarden-install --profile sdk-minimal \
 *         --tenant acme --owner user-42 --bin /usr/local/bin/memgarden
 *
 * 以前 README 只说「把插件挂进 cordis.patch.yml」，真正能跑的步骤藏在 E2E
 * 脚本里（建目录、连 symlink、拼 YAML）。陌生工程师照 README 做不出来，
 * 得先读一遍我们的测试代码 —— 那说明这个 Adapter 还不算能被别人装。
 */
import { existsSync, mkdirSync, readFileSync, symlinkSync, unlinkSync, writeFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))

function arg(name, fallback = '') {
  const i = process.argv.indexOf('--' + name)
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback
}

const home = resolve(arg('dsh-home', process.env.DSH_HOME || ''))
const profile = arg('profile', 'sdk-minimal')
const tenant = arg('tenant')
const owner = arg('owner')
const bin = arg('bin', 'memgarden')
const storage = arg('storage', join(home, 'memgarden.db'))
const locale = arg('locale', 'zh-Hans')

const problems = []
if (!home) problems.push('--dsh-home（或环境变量 DSH_HOME）')
if (!tenant) problems.push('--tenant')
// owner 没有默认值是**有意的**：随便给一个会让同一个租户下的不同用户
// 共用一座花园，而且不报错。见 plugin.mjs 里 memoryOwner 那段。
if (!owner) problems.push('--owner（这座花园的稳定所有者，不能省）')
if (problems.length) {
  console.error('缺少参数：' + problems.join('、'))
  console.error('\n用法：\n  npx dsh-memgarden-install --dsh-home <目录> \\\n'
                + '      --profile sdk-minimal --tenant <租户> --owner <归属人> \\\n'
                + '      [--bin memgarden] [--storage <db 路径>] [--locale zh-Hans]')
  process.exit(2)
}

const profileDir = join(home, 'profiles', profile)
if (!existsSync(profileDir)) {
  console.error(`找不到 profile 目录：${profileDir}\n`
                + `先初始化一次：DSH_HOME=${home} npx dsh --profile ${profile} --dump-default-config`)
  process.exit(2)
}

const link = join(profileDir, 'node_modules', 'dsh-memgarden')
mkdirSync(dirname(link), { recursive: true })
if (existsSync(link)) unlinkSync(link)
symlinkSync(HERE, link, 'dir')

const patchPath = join(profileDir, 'cordis.patch.yml')
const entry = [
  '- insert:',
  '    - id: memgarden',
  "      name: 'dsh-memgarden'",
  '      inject: [tools, llm]',
  '      config:',
  `        bin: '${bin}'`,
  `        storage: 'sqlite:///${storage}'`,
  `        tenant: '${tenant}'`,
  `        memoryOwner: '${owner}'`,
  `        locale: '${locale}'`,
  '',
].join('\n')

const existing = existsSync(patchPath) ? readFileSync(patchPath, 'utf8') : ''
if (existing.includes('id: memgarden')) {
  console.log(`ℹ️  ${patchPath} 里已经有 memgarden 了，没有重复写入。`)
} else {
  // 🔴 dsh 生成的默认 patch 文件里有一个**空数组字面量** `[]`。
  // 直接往后追加 `- insert:` 会得到「一个文档里既有 flow 序列又有 block
  // 序列」的非法 YAML，dsh 启动时直接解析失败：
  //     failed to parse overlay ...: end of the stream or a document
  //     separator is expected (5:1)
  // 而这个报错完全看不出跟「装记忆插件」有关。所以要先把那个占位删掉。
  const cleaned = existing
    .split('\n')
    .filter((line) => line.trim() !== '[]')
    .join('\n')
    .replace(/\n+$/, '')
  writeFileSync(patchPath, (cleaned ? cleaned + '\n' : '') + entry)
  console.log(`✅ 已写入 ${patchPath}`)
}
console.log(`✅ 已链接 ${link} → ${HERE}`)
console.log(`\n下一步：确认 ${bin} 在 PATH 上（pip install memgarden），然后照常起 dsh。`)
