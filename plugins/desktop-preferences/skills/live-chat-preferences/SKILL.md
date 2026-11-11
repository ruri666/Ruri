---
name: live-chat-preferences
description: Infer interests from QQ/WeChat conversation topics and other speakers, with optional deeper reading of the user's own messages, through the running Windows clients.
---

Use client-chat.mjs at this plugin's root through the host computer-use sky client in node_repl. Read the installed computer-use skill before desktop actions. The source is the running client UI. Follow ../desktop-preferences/SKILL.md for source-backed preference review and lifecycle persistence.

## Reading scope

The shared setting deep_personal_reading defaults to false. Read or change it with `python -X utf8 preferences.py settings` and `settings --deep-personal-reading on|off`. The default private file is ~/.artificial-life/preference-settings.json. Desktop and Android readers share it. Set --settings/ settingsPath only for an explicitly selected alternate config.

When false, collect up to five conversation titles plus a small amount of identifiable other speech. Exclude the user's own messages and omit their account nickname from the evidence report. The account badge is inspected locally only to exclude own speech. When true, also include own text. Change this setting or pass deepPersonalReading:true only when the user chooses to enable deeper personal reading. Do not enable it automatically when context evidence seems insufficient.

Group names, conversation names and other people's statements are indirect context. They can suggest shared interests and candidate topics. They do not establish that the user shares a participant's opinion or has the identity, occupation or personal circumstances mentioned in a group name. Conversation titles may represent private chats, services or groups. Preserve uncertainty and seek corroboration across independent sources. Save inferred candidates unless the user has confirmed the preference itself.

## Collection

1. Explain the chosen scope. Selected conversation titles and short excerpts enter the configured Codex model. Use a new private evidence file outside the repository.
2. Initialize sky using the host computer-use skill. Import client-chat.mjs by its absolute installed path. Call listChatSources(sky), then inspect the returned app keys and window IDs before selecting qq/wechat. Ambiguous windows require choosing the intended returned window.
3. Call sampleChat(sky,{app:'wechat',limit:10,excerptChars:120,maxTotalChars:1600,restore:true,output:NEW_FILE}). For QQ use app:'qq'. The default respects the stored setting. Titles and messages share the count and character budgets. For both apps, default to 10 observations each, 20 total. No automatic history scrolling occurs.
4. Inspect status and observations. sampled means usable context or messages. no_matching_messages means no eligible messages; unsupported_layout means the parser cannot reliably identify the displayed structure. needs_open_client, needs_login and ambiguous_window require resolving that condition. Login is a user handoff. A user-stopped request ends desktop actions immediately.
5. For another conversation, navigate through the host's documented computer-use workflow, then verify the actual selected title. Conversation lists reorder and stale indexes may select another chat. The sampler itself does not type, send, search or switch conversations. A shallow trial uses one loaded-pane snapshot per app.
6. The account badge and author labels support display-name matching. A different own group nickname can be supplied as ownerAlias only when identified by the user. Both account nickname and the supplied alias are excluded when deeper reading is off. Duplicate nicknames can remain ambiguous and require clarification. Unknown senders and recognized quoted/forwarded/non-text content are skipped.
7. Review candidates using the existing preview/save --connect ... --evidence REPORT route. conversation-topic is a title observation, conversation-context is other speech, and user-message is own speech available only after opting in. Never describe the first two kinds as the user's direct statement. Core memory contains concise preferences and source references, while excerpts stay in local evidence. Check the target persona before committing.

## Executable entrypoints

```javascript
globalThis.chatReader=await import('file:///C:/actual/plugin/client-chat.mjs');
nodeRepl.write(JSON.stringify(await chatReader.listChatSources(sky)));
```

Inspect the returned sources before the next call.

```javascript
globalThis.sample=await chatReader.sampleChat(sky,{
  app:'wechat',limit:10,excerptChars:120,maxTotalChars:1600,
  restore:true,output:'C:/private-evidence/wechat-new.json'
});
nodeRepl.write(JSON.stringify(sample));
```

Limits are at most20 observations,160 characters per message and3200 characters total. Up to five titles can be included within those limits. Output is created exclusively. It contains selected evidence, capture time and UI positions, not the raw UI tree or screenshots. UI Automation can expose buffered rows beyond visible lines. Current support covers WeChat message rows and QQ avatar-labelled or author-group/content-group layouts. Unknown versions return an unsupported status. Capture time is not an inferred historical message timestamp.


## Automatic complete-run route (0.8.0)

For a user request to discover and remember preferences, invoke the primary desktop-preferences workflow with these sources and the user's chosen depth. It owns collection paths, model synthesis, canonical conversion, preview, commit and readback. Do not ask the user to provide JSON, inspect hashes or approve each inferred preference. The source/depth request authorizes inferred-memory persistence. Keep the scope, hardware/login handoffs and budget rules above. Standalone collection is only for a user who explicitly asks for a collection-only diagnostic.
