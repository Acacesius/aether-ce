"""CLI_EXECUTOR: JSON-line parsing for Claude stream-json and Codex exec --json; raw log."""
import json
import unittest

from aether.studio import cli_events
from aether.studio.cli_events import parse_line

from tests.studio import support


def line(obj):
    return json.dumps(obj)


class ClaudeParseTests(unittest.TestCase):
    def test_assistant_text_and_tool_use(self):
        events = parse_line(line({'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'hello'}, {'type': 'tool_use', 'id': 't1', 'name': 'Edit', 'input': {'a': 1}}]}}),
            'claude')
        self.assertEqual(events, [{'kind': 'assistant_text', 'text': 'hello'},
                                  {'kind': 'tool_use', 'id': 't1', 'name': 'Edit', 'input': '{"a": 1}'}])

    def test_tool_result_string_and_blocks(self):
        for content in ('ok', [{'type': 'text', 'text': 'ok'}]):
            events = parse_line(line({'type': 'user', 'message': {'content': [
                {'type': 'tool_result', 'tool_use_id': 't1', 'content': content, 'is_error': True}]}}), 'claude')
            self.assertEqual(events, [{'kind': 'tool_result', 'id': 't1', 'text': 'ok', 'is_error': True}])

    def test_result_usage_and_final(self):
        events = parse_line(line({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'done',
                                  'total_cost_usd': 0.5, 'usage': {'input_tokens': 3, 'output_tokens': 4,
                                                                   'cache_read_input_tokens': 2}}), 'claude')
        self.assertEqual(events[0], {'kind': 'usage', 'input_tokens': 3, 'output_tokens': 4, 'cached_tokens': 2,
                                     'cost_usd': 0.5})
        self.assertEqual(events[1], {'kind': 'final_result', 'text': 'done', 'subtype': 'success', 'is_error': False})

    def test_error_subtype_is_error(self):
        events = parse_line(line({'type': 'result', 'subtype': 'error_max_turns'}), 'claude')
        self.assertTrue(events[-1]['is_error'])

    def test_assistant_usage_is_not_double_counted(self):
        events = parse_line(line({'type': 'assistant', 'message': {'content': [], 'usage': {'input_tokens': 9}}}), 'claude')
        self.assertEqual(events, [])

    def test_unknown_and_garbled_lines(self):
        self.assertEqual(parse_line(line({'type': 'system', 'subtype': 'init'}), 'claude'), [])
        self.assertEqual(parse_line('', 'claude'), [])
        self.assertEqual(parse_line('{"type": "assist', 'claude'), [{'kind': 'raw', 'text': '{"type": "assist'}])
        self.assertEqual(parse_line('[1, 2]', 'claude'), [{'kind': 'raw', 'text': '[1, 2]'}])
        self.assertEqual(parse_line(b'\xff\xfe bad', 'claude')[0]['kind'], 'raw')
        # JSON of the right type with nonsense inside must not raise
        for weird in ({'type': 'assistant', 'message': 'x'}, {'type': 'assistant', 'message': {'content': [1, None]}},
                      {'type': 'user', 'message': {'content': 'x'}}, {'type': 'result', 'usage': 'x', 'errors': [1]}):
            parse_line(line(weird), 'claude')


class CodexParseTests(unittest.TestCase):
    def test_item_lines(self):
        cmd = parse_line(line({'type': 'item.started', 'item': {'id': 'i1', 'type': 'command_execution', 'command': 'ls'}}), 'codex')
        self.assertEqual(cmd, [{'kind': 'tool_use', 'id': 'i1', 'name': 'command', 'input': '"ls"'}])
        res = parse_line(line({'type': 'item.completed', 'item': {'id': 'i1', 'type': 'command_execution',
                                                                  'aggregated_output': 'x', 'exit_code': 2}}), 'codex')
        self.assertEqual(res, [{'kind': 'tool_result', 'id': 'i1', 'text': 'x', 'is_error': True}])
        msg = parse_line(line({'type': 'item.completed', 'item': {'id': 'i2', 'type': 'agent_message', 'text': 'hi'}}), 'codex')
        self.assertEqual(msg, [{'kind': 'assistant_text', 'text': 'hi'}])
        self.assertEqual(parse_line(line({'type': 'item.completed', 'item': {'type': 'reasoning', 'text': 'x'}}), 'codex'), [])

    def test_usage_and_errors(self):
        usage = parse_line(line({'type': 'turn.completed', 'usage': {'input_tokens': 5, 'cached_input_tokens': 1,
                                                                     'output_tokens': 2}}), 'codex')
        self.assertEqual(usage, [{'kind': 'usage', 'input_tokens': 5, 'output_tokens': 2, 'cached_tokens': 1}])
        self.assertEqual(parse_line(line({'type': 'error', 'message': 'boom'}), 'codex'), [{'kind': 'error', 'text': 'boom'}])
        self.assertEqual(parse_line(line({'type': 'turn.failed', 'error': {'message': 'bad'}}), 'codex'),
                         [{'kind': 'error', 'text': 'bad'}])

    def test_legacy_msg_lines(self):
        self.assertEqual(parse_line(line({'id': '0', 'msg': {'type': 'agent_message', 'message': 'x'}}), 'codex'),
                         [{'kind': 'assistant_text', 'text': 'x'}])
        usage = parse_line(line({'id': '0', 'msg': {'type': 'token_count', 'input_tokens': 3, 'output_tokens': 1}}), 'codex')
        self.assertEqual(usage[0]['input_tokens'], 3)
        final = parse_line(line({'id': '0', 'msg': {'type': 'task_complete', 'last_agent_message': 'end'}}), 'codex')
        self.assertEqual(final[0]['kind'], 'final_result')

    def test_garbled(self):
        self.assertEqual(parse_line('\x00 garbage', 'codex')[0]['kind'], 'raw')
        self.assertEqual(parse_line(line({'type': 'item.completed', 'item': 'x'}), 'codex'), [])


class RawLogTests(unittest.TestCase):
    def test_append_utf8_and_tail_of_last_section(self):
        path = support.tempdir(self) / 'run.log'
        log = cli_events.RawLog(path)
        log.header(stage='a', attempt=1)
        log.stdout('first ü')
        log.header(stage='a', attempt=2)
        log.stdout('second ✓')
        log.stderr('oops\nmore')
        self.assertEqual(log.tail(), ['second ✓', '[stderr] oops more'])
        text = path.read_text(encoding='utf-8')
        self.assertIn('first ü', text)
        self.assertEqual(text.count('[aether] '), 2)
        cli_events.RawLog(path).stdout('third')     # a new writer appends, never truncates
        self.assertIn('first ü', path.read_text(encoding='utf-8'))

    def test_tail_bounded(self):
        log = cli_events.RawLog(support.tempdir(self) / 'big.log')
        for i in range(5000):
            log.stdout(f'line {i:05d} ' + 'x' * 40)
        tail = log.tail(4096)
        self.assertLess(len(tail), 100)
        self.assertEqual(tail[-1][:10], 'line 04999')
        self.assertEqual(cli_events.RawLog(support.tempdir(self) / 'none.log').tail(), [])


if __name__ == '__main__':
    unittest.main()
