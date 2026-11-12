import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { parseChatTree, sampleChat, listChatSources, readChatSettings } from '../client-chat.mjs';

const wechatTree = `Window: 微信
0 窗口 微信
\t1 工具栏 导航
\t\t2 按钮 Owner
\t\t3 按钮 聊天
\t4 列表 会话
\t\t5 列表项目 (selectable) A sidebar message is not evidence
\t6 列表 消息
\t\t7 列表项目 (selectable) Other person's interest
\t\t\t8 按钮 Other
\t\t9 列表项目 (selectable) I enjoy music production
\t\t\t10 文本 I enjoy music production
\t\t\t11 按钮 Owner
\t\t12 列表项目 (selectable) Quoted reply 引用 的消息 : I enjoy games
\t\t\t13 按钮 Owner
\t\t14 列表项目 (selectable) I want to explore VLA
\t\t\t15 按钮 Owner
\t\t16 列表项目 (selectable) [图片]
\t\t\t17 按钮 Owner
\t\t18 列表项目 (selectable) Unknown author
\t\t\t19 文本 Owner
\t20 编辑 Compose
Document text: Owner Other person's interest`;

const qqTree = `0 窗格 QQ
\t1 按钮 Owner的头像
\t2 应用程序 会话列表 Secondary Actions: Raise
\t\t3 组 Arcaea group
\t4 应用程序 消息列表 Secondary Actions: Raise
\t\t5 列表项目 (selectable) I enjoy rhythm games
\t\t\t6 按钮 Owner的头像
\t\t7 列表项目 (selectable) Other person's statement
\t\t\t8 按钮 Other的头像`;

test('WeChat deep mode distinguishes own speech, other speech and topics', () => {
  const result = parseChatTree(wechatTree, { app: 'wechat', deepPersonalReading: true });
  assert.equal(result.status, 'sampled');
  assert.deepEqual(result.messages.map(message => message.excerpt), ["Other person's interest", 'I enjoy music production', 'I want to explore VLA']);
  assert.equal(result.unidentifiedRows, 1);
  assert.equal(result.accountOwner, 'Owner');
});

test('QQ account and message avatar identify own text', () => {
  assert.deepEqual(parseChatTree(qqTree, { app: 'qq', deepPersonalReading: true }).messages.filter(message => message.speaker === 'self').map(message => message.excerpt), ['I enjoy rhythm games']);
});

test('Limits apply to newest own messages and codepoint counts', () => {
  const result = parseChatTree(wechatTree.replace('列表 会话', '列表 其他'), { app: 'wechat', deepPersonalReading: true, limit: 1, excerptChars: 20, maxTotalChars: 20 });
  assert.equal(result.messages.length, 1);
  assert.equal(result.sampledChars, 20);
  assert.equal(result.messages[0].row, 14);
  assert.throws(() => parseChatTree('', { app: 'wechat', limit: 21 }));
  assert.throws(() => parseChatTree('', { app: 'wechat', excerptChars: 1000 }));
});

test('Unrecognized author or layout never becomes own speech', () => {
  assert.ok(parseChatTree(wechatTree, { app: 'wechat', ownerAlias: 'Unknown' }).messages.every(message => message.speaker === 'other'));
  assert.equal(parseChatTree('0 编辑 扫码登录', { app: 'qq' }).status, 'needs_login');
  assert.ok(parseChatTree(qqTree.replace('列表项目', '不支持的项目'), { app: 'qq' }).messages.every(message => message.speaker === 'other'));
});

test('Normal sample entrypoint uses returned window and writes Python-compatible evidence', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'live-chat-test-'));
  try {
    const window = { id: 23, app: 'process:C:\\Apps\\WeChat.exe', title: '微信' };
    const calls = [];
    const sky = {
      list_windows: async () => [window],
      get_window: async input => { assert.equal(input.id, 23); calls.push('get'); return window; },
      get_window_state: async input => { assert.equal(input.include_screenshot, false); calls.push('capture'); return { window, accessibility: { tree: wechatTree } }; },
    };
    const output = join(directory, 'evidence.json');
    const result = await sampleChat(sky, { app: 'wechat', output, settingsPath: join(directory, 'settings.json') });
    assert.equal(result.observations.length, 2);
    assert.equal(result.acquisition, 'live-client-accessibility');
    const source = await readFile(output, 'utf8');
    assert.ok(!source.includes('I enjoy music production'));
    assert.ok(!source.includes('I want to explore VLA'));
    assert.equal(result.deep_personal_reading, false);
    assert.ok(!Object.hasOwn(result, 'account_owner'));
    assert.ok(result.observations.some(item => item.kind === 'conversation-topic'));
    assert.ok(result.observations.some(item => item.kind === 'conversation-context'));
    assert.deepEqual(calls, ['get', 'capture']);
    const validation = spawnSync('python', ['-X', 'utf8', '-c',
      "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from evidence import load_evidence; assert len(load_evidence(Path(sys.argv[2]))) == 2", new URL('..', import.meta.url).pathname.replace(/^\/(\w:)/, '$1'), output], { encoding: 'utf8' });
    assert.equal(validation.status, 0, validation.stderr);
    await assert.rejects(sampleChat(sky, { app: 'wechat', output, settingsPath: join(directory, 'settings.json') }), /EEXIST/);
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
});

test('Missing/ambiguous clients and user stop do not trigger UI actions', async () => {
  const absent = await sampleChat({ list_windows: async () => [] }, { app: 'qq' });
  assert.equal(absent.status, 'needs_open_client');
  const window = { id: 1, app: 'process:C:\\Apps\\QQ.exe', title: 'QQ' };
  const ambiguous = await sampleChat({ list_windows: async () => [window, { ...window, id: 2 }] }, { app: 'qq' });
  assert.equal(ambiguous.status, 'ambiguous_window');
  await assert.rejects(sampleChat({ list_windows: async () => { throw new Error('Computer Use was stopped by the user'); } }, { app: 'qq' }), /stopped by the user/);
  await assert.rejects(listChatSources({ list_windows: async () => [{ app: 'process:C:\\Windows\\LockApp.exe' }] }), /desktop_locked/);
});

test('Minimized restoration is explicit and bounded to one attempt', async () => {
  const window = { id: 1, app: 'process:C:\\Apps\\WeChat.exe' };
  let reads = 0;
  let restores = 0;
  const sky = {
    list_windows: async () => [window], get_window: async () => window,
    activate_window: async () => { restores += 1; },
    get_window_state: async () => { reads += 1; if (reads === 1) throw new Error('window is minimized'); return { window, accessibility: { tree: wechatTree } }; },
  };
  assert.equal((await sampleChat(sky, { app: 'wechat', restore: true })).status, 'sampled');
  assert.equal(reads, 2);
  assert.equal(restores, 1);
});

test('QQ live author/content groups cannot inherit an author across cards or timestamps', () => {
  const tree = `0 窗格 QQ
\t1 按钮 Owner的头像
\t2 应用程序 会话列表 Secondary Actions: Raise
\t3 应用程序 消息列表 Secondary Actions: Raise
\t\t4 组
\t\t\t5 文本 19:05
\t\t6 组 Other
\t\t7 组
\t\t\t8 文本 Other speech
\t\t9 组 Owner
\t\t10 组
\t\t\t11 文本 我想了解VLA，也喜欢音乐🎵
\t\t12 组 Owner
\t\t13 组 卡片 ID: ark-msg-content-container_123
\t\t\t14 图像 card
\t\t15 组
\t\t\t16 文本 Must not inherit Owner
\t\t17 组 Other
\t\t18 组
\t\t\t19 组 引用 Owner
\t\t\t\t20 文本 A quoted preference`;
  const result = parseChatTree(tree, { app: 'qq', deepPersonalReading: true });
  assert.equal(result.status, 'sampled');
  assert.deepEqual(result.messages.filter(message => message.speaker === 'self').map(message => message.excerpt), ['我想了解VLA，也喜欢音乐🎵']);
});

test('A message avatar cannot substitute for a missing account badge', () => {
  const noAccount = qqTree.replace('\t1 按钮 Owner的头像\n', '');
  assert.equal(parseChatTree(noAccount, { app: 'qq' }).status, 'unsupported_layout');
  assert.equal(parseChatTree(noAccount, { app: 'qq' }).messages.length, 0);
});


test('Context-only is the default and enabling personal reading is explicit', async () => {
  const context = parseChatTree(wechatTree, { app: 'wechat' });
  assert.equal(context.messages.length, 1);
  assert.equal(context.messages[0].speaker, 'other');
  assert.equal(context.topics.length, 1);
  assert.ok(!JSON.stringify(context.messages).includes('Owner'));
  const deep = parseChatTree(wechatTree, { app: 'wechat', deepPersonalReading: true });
  assert.equal(deep.messages.filter(message => message.speaker === 'self').length, 2);
  assert.throws(() => parseChatTree('', { app: 'qq', deepPersonalReading: 'true' }));
  const limited = parseChatTree(wechatTree, { app: 'wechat', limit: 1, maxTotalChars: 20 });
  assert.equal(limited.topics.length + limited.messages.length, 1);
  assert.ok(limited.sampledChars <= 20);
  const directory = await mkdtemp(join(tmpdir(), 'chat-settings-test-'));
  try {
    const path = join(directory, 'settings.json');
    assert.equal((await readChatSettings(path)).deep_personal_reading, false);
    const { writeFile } = await import('node:fs/promises');
    await writeFile(path, JSON.stringify({schema:'preference-settings/v1',deep_personal_reading:true}));
    assert.equal((await readChatSettings(path)).deep_personal_reading, true);
    await writeFile(path, JSON.stringify({schema:'preference-settings/v1',deep_personal_reading:'false'}));
    await assert.rejects(readChatSettings(path), /Invalid/);
  } finally { await rm(directory, { recursive:true, force:true }); }
});

test('Occluded unsupported pane is refreshed once after explicit restoration', async () => {
  const window = { id: 1, app: 'process:C:\\Apps\\WeChat.exe' };
  let reads = 0, activations = 0;
  const sky = { list_windows: async () => [window], get_window: async () => window,
    activate_window: async () => { activations += 1; },
    get_window_state: async () => { reads += 1; return {window,accessibility:{tree: reads===1?'0 窗口 微信\n\t1 窗格 (disabled)':wechatTree}}; } };
  assert.equal((await sampleChat(sky,{app:'wechat',restore:true})).status,'sampled');
  assert.equal(reads,2);
  assert.equal(activations,1);
});
