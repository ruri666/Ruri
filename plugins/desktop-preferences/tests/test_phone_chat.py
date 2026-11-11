"""Regression checks for Android collection scopes and shared settings."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from phone_chat import parse_android_chat, capture_android_chat
from chat_settings import read_settings, update_settings
from evidence import load_evidence

XML = '''<hierarchy><node package="com.tencent.mm" class="android.widget.FrameLayout" bounds="[0,0][1080,2000]">
<node class="android.view.ViewGroup" package="com.tencent.mm" bounds="[0,200][1080,400]">
  <node class="android.widget.ImageView" package="com.tencent.mm" content-desc="Friend头像" bounds="[10,210][100,300]"/>
  <node class="android.widget.TextView" package="com.tencent.mm" text="Discussing rhythm games" bounds="[120,210][700,300]"/>
</node>
<node class="android.view.ViewGroup" package="com.tencent.mm" bounds="[0,500][1080,700]">
  <node class="android.widget.TextView" package="com.tencent.mm" text="My private statement" bounds="[400,500][950,600]"/>
  <node class="android.widget.ImageView" package="com.tencent.mm" content-desc="Me头像" bounds="[980,500][1070,600]"/>
</node></node></hierarchy>'''

TOPICS = '''<hierarchy><node package="com.tencent.mobileqq" class="androidx.recyclerview.widget.RecyclerView" content-desc="会话列表" bounds="[0,0][1080,2000]">
<node class="android.view.ViewGroup" package="com.tencent.mobileqq">
  <node class="android.widget.TextView" package="com.tencent.mobileqq" text="Arcaea group"/>
  <node class="android.widget.TextView" package="com.tencent.mobileqq" text="My private preview must stay out"/>
</node><node class="android.view.ViewGroup" package="com.tencent.mobileqq">
  <node class="android.widget.TextView" package="com.tencent.mobileqq" text="Music production group"/>
</node></node></hierarchy>'''


class PhoneChatTests(unittest.TestCase):
    def test_default_reads_conversation_titles_without_previews(self):
        result = parse_android_chat(TOPICS, 'qq')
        self.assertEqual([item['name'] for item in result['items']], ['Arcaea group', 'Music production group'])
        self.assertNotIn('private', json.dumps(result))

    def test_messages_exclude_own_bubbles_until_enabled(self):
        result = parse_android_chat(XML, 'wechat', screen='messages')
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['speaker'], 'other')
        self.assertNotIn('private', json.dumps(result))
        deep = parse_android_chat(XML, 'wechat', screen='messages', deep_personal_reading=True)
        self.assertEqual([item['speaker'] for item in deep['items']], ['other', 'self'])
        self.assertEqual(deep['items'][1]['kind'], 'user-message')

    def test_wrong_app_and_ambiguous_layout_do_not_return_text(self):
        self.assertEqual(parse_android_chat(XML, 'qq')['status'], 'needs_open_client')
        self.assertEqual(parse_android_chat(XML.replace('头像', 'image'), 'wechat', screen='messages')['items'], [])
        with self.assertRaises(ValueError):
            parse_android_chat(XML, 'wechat', deep_personal_reading='true')

    def test_limits_bound_both_titles_and_messages(self):
        result = parse_android_chat(TOPICS, 'qq', limit=1, max_total_chars=20)
        self.assertEqual(len(result['items']), 1)
        self.assertLessEqual(len(result['items'][0]['name']), 20)
        result = parse_android_chat(XML, 'wechat', screen='messages', deep_personal_reading=True, max_total_chars=20)
        self.assertLessEqual(sum(len(item['excerpt']) for item in result['items']), 20)

    def test_capture_uses_selected_device_and_evidence_schema(self):
        def adb(command, **kwargs):
            if 'get-state' in command:
                output = 'device\n'
            elif 'cat' in command:
                output = XML
            else:
                output = ''
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr='')
        with patch('phone_chat.subprocess.run', side_effect=adb) as run:
            report = capture_android_chat('adb', 'selected-phone', 'wechat', screen='messages')
        self.assertEqual(report['acquisition'], 'live-client-uiautomator')
        self.assertEqual(len(report['observations']), 1)
        self.assertFalse(report['deep_personal_reading'])
        self.assertTrue(all(call.args[0][:3] == ['adb', '-s', 'selected-phone'] for call in run.call_args_list))
        self.assertEqual(run.call_args.args[0][3:5], ['shell', 'rm'])
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'evidence.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            self.assertEqual(len(load_evidence(path)), 1)

    def test_shared_settings_are_boolean_and_default_off(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'settings.json'
            self.assertFalse(read_settings(path)['deep_personal_reading'])
            update_settings(True, path)
            self.assertTrue(read_settings(path)['deep_personal_reading'])
            update_settings(False, path)
            self.assertFalse(read_settings(path)['deep_personal_reading'])
            path.write_text('{"schema":"preference-settings/v1","deep_personal_reading":"false"}', encoding='utf-8')
            with self.assertRaises(ValueError):
                read_settings(path)

class AutomaticScreenTests(unittest.TestCase):
    def test_unknown_list_cannot_turn_own_preview_into_a_topic(self):
        ambiguous = TOPICS.replace('content-desc="会话列表"', '')
        result = parse_android_chat(ambiguous, 'qq')
        self.assertEqual(result['status'], 'unsupported_layout')
        self.assertEqual(result['items'], [])


if __name__ == '__main__':
    unittest.main()
