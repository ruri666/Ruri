---
name: mobile-preferences
description: Collect bounded Android QQ/WeChat UI context and application evidence, with optional own-message reading, or analyze explicitly selected Android/iPhone exports.
---

Read ../desktop-preferences/SKILL.md for source-backed interpretation and lifecycle persistence. The plugin runs in desktop Codex and collects phone evidence via available device tooling. The shared deep_personal_reading setting defaults to false, is configured with preferences.py settings --deep-personal-reading on|off, and is also used by the desktop reader. Read ../live-chat-preferences/SKILL.md for the distinction between indirect social context and own statements.

## Android direct client reading

Use an explicitly selected, already-authorized online ADB device. The user enables USB debugging and acknowledges the computer on the phone. Do not automate permission settings, install tools or authorize devices without the user. Keep the phone unlocked and ask the user to open the relevant QQ/WeChat screen.

`python -X utf8 preferences.py phone-chat --adb ADB_PATH --device SERIAL --app wechat --screen conversations --output NEW_PRIVATE_JSON`

For QQ use --app qq. The default conversations screen reads only the first title in each visible list row and excludes previews. This requires a recognizable ListView/RecyclerView. To sample a currently open actual conversation, specify --screen messages. The caller must confirm the phone is on that screen before selecting that mode. Other-message bubbles are recognized by explicitly labelled avatars on the left side. Own-message bubbles on the right are included only if the shared option is on, or the user explicitly selects --deep-personal-reading on for that call. The method is a layout convention, not unique-account authentication. Unknown or ambiguous layouts produce no message excerpts.

Default budgets are10 observations,120 characters each and1600 total; maximum20,160 and3200. The sampler calls get-state, then uiautomator dump, reads one temporary XML hierarchy and removes only its own generated file. It performs no taps, swipes, text entry, app launches or history expansion. Outputs use preference-evidence/v1 and go through the same --evidence preview/save pipeline. Raw XML is not retained.

## Other phone sources

Android applications can be sampled with `observe android-apps --platform android --device SERIAL --adb ADB_PATH --limit 20 --output NEW_JSON`. Package names may be ambiguous.

Selected Android or iPhone app inventories, readable browser history and chat exports remain supported through observe apps/history/chat --platform android|ios --input PATH --output NEW_JSON. Chat imports also respect the shared deeper-reading setting and cap samples at20 messages and160 characters each. Normalized chats require exact --user-id; QQChatExporter exports can use their own account metadata.

## iPhone boundary

The current plugin has no iPhone live-UI collector. Use explicitly selected readable exports for iPhone. A USB connection alone does not provide access to its QQ/WeChat sandboxes. Direct iPhone debugging needs a separately available authorized iOS automation host; report this requirement and do not claim a successful live-phone test from fixtures or export tests.


## Automatic complete-run route (0.8.0)

For a user request to discover and remember preferences, invoke the primary desktop-preferences workflow with these sources and the user's chosen depth. It owns collection paths, model synthesis, canonical conversion, preview, commit and readback. Do not ask the user to provide JSON, inspect hashes or approve each inferred preference. The source/depth request authorizes inferred-memory persistence. Keep the scope, hardware/login handoffs and budget rules above. Standalone collection is only for a user who explicitly asks for a collection-only diagnostic.
