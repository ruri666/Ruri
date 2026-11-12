/** Read one screen of QQ/WeChat through the host's computer-use client. */
import { createHash } from 'node:crypto';
import { readFile, writeFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

const CLIENTS = {
  qq: /(?:^|[\\/])QQ\.exe$/i,
  wechat: /(?:^|[\\/])(?:WeChat|Weixin)\.exe$/i,
};

function clean(value, limit = 500) {
  return Array.from(String(value ?? '').replace(/[\p{C}\p{Z}]/gu, ' ')).slice(0, limit).join('').trim();
}

// Match Python json.dumps(..., ensure_ascii=False, sort_keys=True), used by evidence.py.
function canonical(value) {
  if (Array.isArray(value)) return `[${value.map(canonical).join(', ')}]`;
  if (value !== null && typeof value === 'object') {
    return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}: ${canonical(value[key])}`).join(', ')}}`;
  }
  return JSON.stringify(value);
}

function record(fields) {
  const id = createHash('sha256').update(canonical(fields), 'utf8').digest('hex').slice(0, 24);
  return { id: `evidence-${id}`, ...fields };
}

function nodesFromTree(tree) {
  const text = String(tree ?? '').split('Document text:')[0];
  return text.split('\n').flatMap(line => {
    const match = /^(\s*)(\d+)\s+(.+)$/.exec(line);
    return match ? [{ depth: match[1].length, index: Number(match[2]), text: match[3].trim() }] : [];
  });
}

function descendants(nodes, position) {
  let end = position + 1;
  while (end < nodes.length && nodes[end].depth > nodes[position].depth) end += 1;
  return nodes.slice(position + 1, end);
}

function controlName(text, type) {
  if (!text.startsWith(`${type} `)) return null;
  return text.slice(type.length + 1).replace(/^(?:\([^)]*\)\s*)+/, '').split(' Value:')[0].trim();
}

function ownerFromTree(nodes, app) {
  if (app === 'qq') {
    // The account badge occurs before the conversation/message lists.
    const firstList = nodes.findIndex(node => /会话列表|消息列表/.test(node.text));
    const badge = nodes.slice(0, firstList < 0 ? nodes.length : firstList)
      .map(node => /^按钮 (.+)的头像$/.exec(node.text)).find(Boolean);
    return badge?.[1] ?? null;
  }
  const navigation = nodes.findIndex(node => node.text === '工具栏 导航');
  if (navigation < 0) return null;
  const buttons = descendants(nodes, navigation).filter(node => node.text.startsWith('按钮 '));
  const name = buttons.length ? controlName(buttons[0].text, '按钮') : null;
  return name && !['聊天', '通讯录', '收藏', '设置及其他'].includes(name) ? name : null;
}

function authorFromRow(children, app) {
  const candidates = children.flatMap(node => {
    const button = controlName(node.text, '按钮');
    const image = controlName(node.text, '图像');
    const value = button ?? (app === 'qq' ? image : null);
    if (!value || /^(?:查看|更多|引用|播放|下载|转发|撤回|菜单|重试)/.test(value)) return [];
    return [{ depth: node.depth, name: value.replace(/的头像$/, '') }];
  });
  if (!candidates.length) return null;
  const shallowest = Math.min(...candidates.map(candidate => candidate.depth));
  const names = [...new Set(candidates.filter(candidate => candidate.depth === shallowest).map(candidate => candidate.name))];
  return names.length === 1 ? names[0] : null;
}

function qqGroupRows(children) {
  if (!children.length) return [];
  const depth = Math.min(...children.map(node => node.depth));
  const direct = children.filter(node => node.depth === depth);
  const rows = [];
  let author = null;
  for (const node of direct) {
    const position = children.indexOf(node);
    const body = descendants(children, position);
    const name = controlName(node.text, '组');
    if (name && !body.length && !/\bID:|卡片|引用|转发/.test(name)) {
      author = name;
      continue;
    }
    if (node.text === '组' && author && body.length === 1 && body[0].depth === depth + 1 && body[0].text.startsWith('文本 ')) {
      const value = controlName(body[0].text, '文本');
      if (value && !/^\d{1,2}:\d{2}$/.test(value)) rows.push({ row: node.index, author, value });
    }
    // An author applies only to the immediately following message group.
    author = null;
  }
  return rows;
}

function conversationFromTree(nodes, listPosition, app) {
  const preceding = nodes.slice(0, listPosition);
  if (app === 'qq') {
    const toolbar = preceding.findLastIndex(node => node.text === '工具栏 更多');
    if (toolbar < 0) return null;
    const header = preceding.slice(0, toolbar).findLast(node => node.text.startsWith('按钮 '));
    return header ? controlName(header.text, '按钮') : null;
  }
  const sidebar = preceding.findIndex(node => node.text === '列表 会话');
  if (sidebar < 0) return null;
  const headerStart = sidebar + descendants(nodes, sidebar).length + 1;
  const header = preceding.slice(headerStart).findLast(node => node.text.startsWith('文本 ') && !/^文本 \(\d+\)$/.test(node.text));
  return header ? controlName(header.text, '文本') : null;
}

function topicsFromTree(nodes, app, limit, maxTotalChars) {
  const position = nodes.findIndex(node => app === 'wechat' ? node.text === '列表 会话' : /^应用程序 会话列表(?:\s|$)/.test(node.text));
  if (position < 0) return [];
  const children = descendants(nodes, position);
  if (!children.length) return [];
  const depth = Math.min(...children.map(node => node.depth));
  const rows = children.filter(node => node.depth === depth);
  const topics = [];
  let remaining = maxTotalChars;
  for (const row of rows) {
    let name;
    if (app === 'wechat') name = controlName(row.text, '列表项目');
    else {
      const firstText = descendants(nodes, nodes.indexOf(row)).find(node => node.text.startsWith('文本 '));
      name = firstText ? controlName(firstText.text, '文本') : null;
    }
    if (!name || topics.some(topic => topic.name === name)) continue;
    const title = clean(name, Math.min(120, remaining));
    if (!title) break;
    topics.push({ row: row.index, name: title });
    remaining -= Array.from(title).length;
    if (topics.length === Math.min(5, limit) || remaining === 0) break;
  }
  return topics;
}

export async function readChatSettings(path = join(homedir(), '.artificial-life', 'preference-settings.json')) {
  let value;
  try { value = JSON.parse(await readFile(path, 'utf8')); }
  catch (error) { if (error.code === 'ENOENT') return { deep_personal_reading: false }; throw error; }
  if (value.schema !== 'preference-settings/v1' || typeof value.deep_personal_reading !== 'boolean') throw new Error('Invalid preference settings');
  return value;
}

/** Pure parser. Unknown layouts and ambiguous authors fail closed. */
export function parseChatTree(tree, { app, ownerAlias, deepPersonalReading = false, limit = 10, excerptChars = 120, maxTotalChars = 1600 } = {}) {
  if (!CLIENTS[app]) throw new Error('app must be qq or wechat');
  if (!Number.isInteger(limit) || limit < 1 || limit > 20) throw new Error('limit must be 1..20');
  if (!Number.isInteger(excerptChars) || excerptChars < 20 || excerptChars > 160) throw new Error('excerptChars must be 20..160');
  if (!Number.isInteger(maxTotalChars) || maxTotalChars < 20 || maxTotalChars > 3200) throw new Error('maxTotalChars must be 20..3200');
  if (typeof deepPersonalReading !== 'boolean') throw new Error('deepPersonalReading must be boolean');
  const nodes = nodesFromTree(tree);
  const topics = topicsFromTree(nodes, app, limit, maxTotalChars);
  const topicChars = topics.reduce((sum, topic) => sum + Array.from(topic.name).length, 0);
  const accountOwner = ownerFromTree(nodes, app);
  if (!accountOwner) {
    return { status: topics.length ? 'sampled' : /二维码|扫码登录|登录QQ|登录微信/.test(String(tree)) ? 'needs_login' : 'unsupported_layout', topics, sampledChars: topicChars, messages: [], visibleRows: 0 };
  }
  if (ownerAlias !== undefined && (typeof ownerAlias !== 'string' || !ownerAlias.trim())) throw new Error('ownerAlias must be a user-supplied nonempty display name');
  const owner = ownerAlias?.trim() ?? accountOwner;
  const ownNames = new Set([accountOwner, owner]);
  const lists = nodes.map((node, position) => ({ node, position }))
    .filter(({ node }) => app === 'wechat' ? node.text === '列表 消息' : /^应用程序 消息列表(?:\s|$)/.test(node.text));
  if (lists.length !== 1) return { status: topics.length ? 'sampled' : 'unsupported_layout', accountOwner, topics, sampledChars: topicChars, messages: [], visibleRows: 0 };
  const conversation = conversationFromTree(nodes, lists[0].position, app);
  const children = descendants(nodes, lists[0].position);
  const listItems = children.filter(node => /^列表项目(?:\s|$)/.test(node.text));
  const rowDepth = listItems.length ? Math.min(...listItems.map(node => node.depth)) : null;
  const rows = listItems.filter(node => node.depth === rowDepth);
  const candidates = rows.map(row => ({ row: row.index,
    author: authorFromRow(descendants(nodes, nodes.indexOf(row)), app), value: controlName(row.text, '列表项目') }));
  if (!rows.length && app === 'qq') candidates.push(...qqGroupRows(children));
  if (!candidates.length) return { status: topics.length ? 'sampled' : 'unsupported_layout', accountOwner, topics, sampledChars: topicChars, messages: [], visibleRows: 0 };
  const messages = [];
  let totalChars = topicChars;
  let unidentifiedRows = 0;
  for (const row of candidates.toReversed()) {
    if (messages.length + topics.length >= limit) break;
    const { author, value } = row;
    if (!author) { unidentifiedRows += 1; continue; }
    const own = ownNames.has(author);
    if (!deepPersonalReading && own) continue;
    if (!value || /引用|转发|\[图片\]|\[动画表情\]|\[语音\]|\[视频\]|\[文件\]/.test(value)) continue;
    const remaining = maxTotalChars - totalChars;
    if (remaining <= 0) break;
    const excerpt = clean(value, Math.min(excerptChars, remaining));
    if (!excerpt || /^\d{1,2}:\d{2}$/.test(excerpt)) continue;
    messages.push({ row: row.row, ...(own ? { author } : {}), speaker: own ? 'self' : 'other', excerpt });
    totalChars += Array.from(excerpt).length;
  }
  return { status: messages.length || topics.length ? 'sampled' : 'no_matching_messages', accountOwner, owner, conversation, topics,
    visibleRows: candidates.length, unidentifiedRows, sampledChars: totalChars, messages: messages.reverse() };
}

export async function listChatSources(sky) {
  if (typeof sky?.list_windows !== 'function') throw new Error('The host computer-use sky client is required');
  const windows = await sky.list_windows();
  if (windows.some(window => /LockApp\.exe$/i.test(window.app))) throw new Error('desktop_locked');
  return windows.flatMap(window => Object.entries(CLIENTS)
    .filter(([, pattern]) => pattern.test(window.app))
    .map(([app]) => ({ app, windowId: window.id, title: window.title ?? '', window })));
}

/** Normal entry point. Reads an existing client; output is already compatible with --evidence. */
export async function sampleChat(sky, options) {
  const { app, windowId, restore = false, output, settingsPath, ...parserOptions } = options ?? {};
  if (!CLIENTS[app]) throw new Error('app must be qq or wechat');
  if (typeof restore !== 'boolean') throw new Error('restore must be a boolean');
  const settings = await readChatSettings(settingsPath);
  parserOptions.deepPersonalReading ??= settings.deep_personal_reading;
  // Validate budgets before any desktop access.
  parseChatTree('', { app, ...parserOptions });
  const sources = (await listChatSources(sky)).filter(source => source.app === app && (windowId === undefined || source.windowId === windowId));
  if (sources.length !== 1) return { status: sources.length ? 'ambiguous_window' : 'needs_open_client', app,
    windows: sources.map(({ window, ...source }) => source) };
  let window = await sky.get_window({ id: sources[0].window.id, app: sources[0].window.app });
  let state;
  let activated = false;
  try {
    state = await sky.get_window_state({ window, include_screenshot: false, include_text: true });
  } catch (error) {
    if (!restore || !/window is minimized/i.test(String(error))) throw error;
    await sky.activate_window({ window });
    activated = true;
    window = await sky.get_window({ id: window.id, app: window.app });
    state = await sky.get_window_state({ window, include_screenshot: false, include_text: true });
  }
  let parsed = parseChatTree(state.accessibility?.tree, { app, ...parserOptions });
  if (restore && !activated && parsed.status === 'unsupported_layout') {
    // Some QQ versions expose only disabled panes while occluded.
    await sky.activate_window({ window: state.window });
    window = await sky.get_window({ id: state.window.id, app: state.window.app });
    state = await sky.get_window_state({ window, include_screenshot: false, include_text: true });
    parsed = parseChatTree(state.accessibility?.tree, { app, ...parserOptions });
  }
  const observedAt = new Date().toISOString();
  const observations = (parsed.topics ?? []).map(topic => record({ kind: 'conversation-topic', name: topic.name,
    source: clean(`live-ui ${app} window=${state.window.id} topic-row=${topic.row}`, 1000), observed_at: observedAt }));
  observations.push(...parsed.messages.map(message => record({ kind: message.speaker === 'self' ? 'user-message' : 'conversation-context', name: message.speaker === 'self' ? '本人发言' : '对话中的他人发言',
    source: clean(`live-ui ${app} window=${state.window.id} row=${message.row}`, 1000),
    excerpt: message.excerpt, speaker: message.speaker, ...(message.author ? { sender: clean(message.author) } : {}), observed_at: observedAt,
    ...(parsed.conversation ? { conversation: clean(parsed.conversation) } : {}) })));
  const report = { schema: 'preference-evidence/v1', platform: 'desktop', observed_at: observedAt,
    acquisition: 'live-client-accessibility', identity_basis: 'account-badge-display-name', app, status: parsed.status,
    ...(parserOptions.deepPersonalReading ? { account_owner: parsed.accountOwner ?? null } : {}),
    deep_personal_reading: parserOptions.deepPersonalReading, conversation: parsed.conversation ?? null, visible_rows: parsed.visibleRows,
    sampled_characters: parsed.sampledChars ?? 0, observations,
    coverage: 'One loaded pane plus up to five conversation titles. Social context is indirect evidence. Own text is included only with deep_personal_reading enabled.' };
  if (output) await writeFile(output, JSON.stringify(report, null, 2) + '\n', { encoding: 'utf8', flag: 'wx' });
  return { ...report, ...(output ? { path: output } : {}) };
}
