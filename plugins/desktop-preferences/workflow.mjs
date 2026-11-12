/** Execute the complete preference workflow with the host's model and computer-use runtime. */
import { spawn } from 'node:child_process';
import { readFile, writeFile, rename } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { listChatSources, sampleChat } from './client-chat.mjs';

const cli = fileURLToPath(new URL('./preferences.py', import.meta.url));

function invokePython(python, args, payload) {
  return new Promise((resolve, reject) => {
    const child = spawn(python, ['-X', 'utf8', cli, ...args], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '';
    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', chunk => {
      stdout += chunk;
      if (stdout.length > 4_000_000) { child.kill(); reject(new Error('Workflow output exceeded its bound')); }
    });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    child.on('error', reject);
    child.on('close', code => {
      if (code !== 0) return reject(new Error(stderr || `Workflow exited with code ${code}`));
      try { resolve(JSON.parse(stdout)); } catch (error) { reject(error); }
    });
    child.stdin.on('error', error => { if (error.code !== 'EPIPE') reject(error); });
    child.stdin.end(payload === undefined ? '' : JSON.stringify(payload));
  });
}

/** Internal collector bridge for hosts where the model completes the synthesis phase itself. */
export async function collectWorkflowChats(sky, directory, { retryUnavailable = false } = {}) {
  const run = JSON.parse(await readFile(join(directory, 'run.json'), 'utf8'));
  if (run.schema !== 'preference-run/v1') throw new Error('Invalid workflow manifest');
  const results = [];
  for (const app of ['qq', 'wechat']) {
    if (!Object.hasOwn(run.sources, app)) continue;
    const output = join(directory, 'evidence', `${app}.json`);
    try {
      const previous = JSON.parse(await readFile(output, 'utf8'));
      if (!retryUnavailable || ['sampled', 'empty'].includes(previous.status) || ['complete', 'committing'].includes(run.status)) {
        results.push({ app, status: previous.status, reused: true });
        continue;
      }
      // Preserve the failed receipt; successful evidence is never recaptured here.
      await rename(output, join(directory, 'evidence', `${app}.failed-${randomUUID()}.json`));
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
    try {
      if (!sky) throw new Error('Host computer-use capability is unavailable');
      let sources = await listChatSources(sky);
      if (!sources.some(source => source.app === app)) {
        const pattern = app === 'qq' ? /(?:^|[\\/])QQ\.exe$/i : /(?:^|[\\/])(?:WeChat|Weixin)\.exe$/i;
        const installed = (await sky.list_apps()).filter(candidate => pattern.test(candidate.id));
        if (installed.length === 1) {
          await sky.launch_app({ app: installed[0].id });
          sources = await listChatSources(sky);
        }
      }
      const report = await sampleChat(sky, { app, deepPersonalReading: run.depth === 'personal',
        limit: 10, excerptChars: 120, maxTotalChars: 1600, restore: true, output });
      if (!report.schema) await writeFile(output, JSON.stringify({ schema: 'preference-source-error/v1', status: report.status, reason: report.status }), { flag: 'wx' });
      results.push({ app, status: report.status, count: report.observations?.length ?? 0 });
    } catch (error) {
      if (/stopped by the user|user stopped|turn ended|desktop_locked/i.test(String(error))) throw error;
      await writeFile(output, JSON.stringify({ schema: 'preference-source-error/v1', status: 'unavailable', reason: String(error).slice(0, 300) }), { flag: 'wx' });
      results.push({ app, status: 'unavailable' });
    }
  }
  return results;
}

export async function startWorkflow({ sources, depth }, { sky, python = 'python' } = {}) {
  const begin = await invokePython(python, ['workflow', 'begin', '--sources', ...sources, ...(depth === undefined ? [] : ['--depth', depth])]);
  await collectWorkflowChats(sky, begin.run);
  return prepareWorkflow(begin.run, { python });
}

export function prepareWorkflow(run, { python = 'python' } = {}) {
  return invokePython(python, ['workflow', 'prepare', '--run', run]);
}

export function finishWorkflow(run, entries, { python = 'python' } = {}) {
  return invokePython(python, ['workflow', 'finish', '--run', run], entries);
}

/** Only sources and depth are user choices; the host supplies its existing synthesis capability. */
export async function runPreferences(options, host) {
  if (typeof host?.synthesize !== 'function') throw new Error('The host model must supply the synthesis capability');
  const prepared = await startWorkflow(options, host);
  const entries = prepared.observations.length ? await host.synthesize(prepared) : [];
  return finishWorkflow(prepared.run, entries, host);
}
