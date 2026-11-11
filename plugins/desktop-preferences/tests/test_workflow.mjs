import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { runPreferences, finishWorkflow } from '../workflow.mjs';

const src = fileURLToPath(new URL('../../../src', import.meta.url));
const tree = `0 窗格 QQ
\t1 按钮 Account的头像
\t2 应用程序 会话列表 Secondary Actions: Raise
\t\t3 组
\t\t\t4 文本 Music production group
\t3 应用程序 消息列表 Secondary Actions: Raise
\t\t5 组 Other
\t\t6 组
\t\t\t7 文本 Discussing music synthesis
\t\t8 组 Account
\t\t9 组
\t\t\t10 文本 This is private own text`;

test('One plugin invocation collects, synthesizes, commits canonical memory and verifies it', async () => {
  const temporary = await mkdtemp(join(tmpdir(), 'preference-full-run-'));
  const savedEnv = { ARTIFICIAL_LIFE_ROOT: process.env.ARTIFICIAL_LIFE_ROOT,
    ARTIFICIAL_LIFE_CONNECT: process.env.ARTIFICIAL_LIFE_CONNECT, PREFERENCE_RUNS_ROOT: process.env.PREFERENCE_RUNS_ROOT };
  try {
    const workspace = join(temporary, 'life');
    const initialized = spawnSync('python', ['-X', 'utf8', '-c',
      "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from artificial_life.plugin import load_agent; load_agent(Path(sys.argv[2]),harness=True)", src, workspace], { encoding: 'utf8' });
    assert.equal(initialized.status, 0, initialized.stderr);
    process.env.ARTIFICIAL_LIFE_ROOT = workspace;
    process.env.ARTIFICIAL_LIFE_CONNECT = 'http://127.0.0.1:1';
    process.env.PREFERENCE_RUNS_ROOT = join(temporary, 'runs');
    const window = { id: 1, app: 'process:C:\\Apps\\QQ.exe', title: 'QQ' };
    let captures = 0, generations = 0;
    const sky = { list_windows: async () => [window], get_window: async () => window,
      get_window_state: async () => { captures += 1; return { window, accessibility: { tree } }; } };
    const text = '用户可能对音乐制作相关话题感兴趣。';
    const result = await runPreferences({ sources: ['qq'], depth: 'context' }, { sky,
      synthesize: async prepared => {
        generations += 1;
        assert.ok(!JSON.stringify(prepared.observations).includes('private own text'));
        return [{ text, sources: prepared.observations.map(item => item.id), category: '音乐',
          detail: '依据会话标题和他人讨论推测，具体喜好仍需用户反馈。', confidence: 'high' }];
      } });
    assert.equal(result.status, 'complete');
    assert.equal(result.readback_verified, true);
    assert.equal(captures, 1);
    assert.equal(generations, 1);
    const memory = await readFile(result.memory_file, 'utf8');
    assert.ok(memory.includes(`[推测，待确认] ${text}`));
    const bundle = JSON.parse(await readFile(join(workspace, result.source_ref), 'utf8'));
    assert.equal(bundle.entries[0].status, 'inferred');
    assert.equal(bundle.entries[0].confidence, 'low');
    assert.ok(bundle.entries[0].sources.every(source => source.includes('evidence-kind=')));
    const replay = await finishWorkflow(result.run, []);
    assert.deepEqual(replay, result);
    assert.equal(await readFile(result.memory_file, 'utf8'), memory);
  } finally {
    for (const [key, value] of Object.entries(savedEnv)) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    await rm(temporary, { recursive: true, force: true });
  }
});
