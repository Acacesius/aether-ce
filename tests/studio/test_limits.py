"""CLI_EXECUTOR: usage-limit detection and reset-time parsing."""
from datetime import datetime, timedelta, timezone
import json
import unittest

from aether.studio import cli_events, limits

from tests.studio import support

NOW = datetime(2026, 3, 10, 12, 0, 0, tzinfo=timezone.utc)   # 12:00 UTC


class DetectTests(unittest.TestCase):
    def hit(self, text):
        result = limits.detect(text, NOW)
        self.assertIsNotNone(result, text)
        return result

    def test_claude_legacy_epoch(self):
        epoch = int((NOW + timedelta(hours=2)).timestamp())
        result = self.hit(f'Claude AI usage limit reached|{epoch}')
        self.assertTrue(result['parsed'])
        self.assertEqual(result['reset_utc'], NOW + timedelta(hours=2))

    def test_claude_clock_with_zone(self):
        result = self.hit('5-hour limit reached ∙ resets 3pm (UTC)')
        self.assertEqual(result['reset_utc'], NOW.replace(hour=15))
        result = self.hit("You've hit your limit · resets 9:30am (UTC)")   # already past today -> tomorrow
        self.assertEqual(result['reset_utc'], (NOW + timedelta(days=1)).replace(hour=9, minute=30))

    def test_claude_clock_named_zone(self):
        result = self.hit('Claude usage limit reached. Your limit will reset at 3pm (America/New_York).')
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo('America/New_York')
        except Exception:
            self.skipTest('no tz database')
        self.assertEqual(result['reset_utc'], NOW.replace(hour=19))   # 15:00 EDT on 10 March 2026

    def test_claude_weekly_date(self):
        result = self.hit('Weekly limit reached ∙ resets Mar 14, 5pm (UTC)')
        self.assertEqual(result['reset_utc'], datetime(2026, 3, 14, 17, 0, tzinfo=timezone.utc))

    def test_codex_relative(self):
        result = self.hit("You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes.")
        self.assertEqual(result['reset_utc'], NOW + timedelta(hours=2, minutes=5))
        result = self.hit("You've hit your usage limit. Try again in 4 days 21 hours 15 minutes.")
        self.assertEqual(result['reset_utc'], NOW + timedelta(days=4, hours=21, minutes=15))
        result = self.hit('usage_limit_reached: try again in 45 min')
        self.assertEqual(result['reset_utc'], NOW + timedelta(minutes=45))

    def test_codex_clock(self):
        result = self.hit("You've hit your usage limit. Try again at 3:05 PM (UTC).")
        self.assertEqual(result['reset_utc'], NOW.replace(hour=15, minute=5))

    def test_iso_timestamp(self):
        result = self.hit('usage limit exceeded; resets 2026-03-10T18:30:00Z')
        self.assertEqual(result['reset_utc'], NOW.replace(hour=18, minute=30))

    def test_no_reset_time_defaults_to_five_hours(self):
        for text in ('Error: usage limit reached', 'Claude AI usage limit reached', 'usage limit reached, resets 3'):
            result = self.hit(text)
            self.assertFalse(result['parsed'])
            self.assertEqual(result['reset_utc'], NOW + timedelta(hours=5))

    def test_stale_or_absurd_reset_falls_back(self):
        past = int((NOW - timedelta(hours=1)).timestamp())
        self.assertEqual(self.hit(f'Claude AI usage limit reached|{past}')['reset_utc'], NOW + timedelta(hours=5))
        self.assertFalse(self.hit('usage limit reached; try again in 400 days')['parsed'])

    def test_not_a_limit(self):
        for text in ('', 'rate limited, retrying', 'Error: 529 overloaded', 'limit the output to 3 lines',
                     'ENOENT: no such file'):
            self.assertIsNone(limits.detect(text, NOW), text)


class LogTailTests(unittest.TestCase):
    def log(self, *lines):
        log = cli_events.RawLog(support.tempdir(self) / 'run.log')
        for item in lines:
            if item.startswith('[stderr] '):
                log.stderr(item[9:])
            elif item.startswith('[aether] '):
                log.header(note=item[9:])
            else:
                log.stdout(item)
        return log

    def test_error_result_counts(self):
        log = self.log('[aether] a', json.dumps({'type': 'result', 'is_error': True, 'result': 'Claude AI usage limit reached'}))
        self.assertIsNotNone(limits.detect_in_log(log, 'claude', NOW))

    def test_stderr_counts(self):
        self.assertIsNotNone(limits.detect_in_log(self.log('[stderr] usage limit reached'), 'claude', NOW))

    def test_assistant_text_and_tool_output_do_not_count(self):
        log = self.log(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'usage limit reached'}]}}),
                       json.dumps({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'hit your limit'}]}}),
                       json.dumps({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'usage limit reached'}}))
        self.assertIsNone(limits.detect_in_log(log, 'claude', NOW))
        self.assertIsNone(limits.detect_in_log(log, 'codex', NOW))

    def test_only_last_section(self):
        log = self.log('[aether] one', '[stderr] usage limit reached', '[aether] two', '[stderr] crash')
        self.assertIsNone(limits.detect_in_log(log, 'claude', NOW))

    def test_codex_error_event(self):
        log = self.log(json.dumps({'type': 'error', 'message': "You've hit your usage limit. Try again in 3 hours."}))
        self.assertEqual(limits.detect_in_log(log, 'codex', NOW)['reset_utc'], NOW + timedelta(hours=3))


if __name__ == '__main__':
    unittest.main()
