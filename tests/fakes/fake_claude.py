"""Fake Claude Code CLI for tests (stdlib only). Prints ``stream-json`` lines like ``claude -p --output-format
stream-json --verbose``. The scenario is the first word of the prompt read from stdin:

  success        assistant text + tool_use + tool_result + result (usage, cost); writes fake_output.txt; exit 0
  limit <msg>    result is_error with <msg> (default: legacy "usage limit reached|<epoch in 2h>"); exit 1
  stderr_limit <msg>  <msg> on stderr only; exit 1
  error_result   result is_error, no limit text; exit 0
  talk_limit     the *assistant* mentions a usage limit, then the CLI crashes; exit 2
  crash          garbled stdout, a traceback on stderr; exit 3
  limit_first    limits once per workspace (marker .limit_first in cwd), then behaves like success
  hold <secs>    like success but sleeps <secs> first (concurrency tests)
  stream <secs>  emits a few live events, sleeps <secs>, then behaves like success (UI screenshots)
  slow           starts a sleeping child (pid in $CLAUDE_CONFIG_DIR/child.pid), then sleeps; killed by tests
Every run records argv, cwd and environment variable names in $CLAUDE_CONFIG_DIR/invocation.json.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def main():
    prompt = sys.stdin.read()
    config = Path(os.environ.get('CLAUDE_CONFIG_DIR', '.'))
    config.mkdir(parents=True, exist_ok=True)
    (config / 'invocation.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': prompt,
                                                        'env': sorted(os.environ)}), encoding='utf-8')
    words = prompt.split(None, 1)
    scenario = words[0] if words else 'success'
    rest = words[1].strip() if len(words) > 1 else ''
    out({'type': 'system', 'subtype': 'init', 'session_id': 'fake-session', 'tools': ['Read', 'Edit'], 'model': 'fake'})
    if scenario == 'success':
        out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'I will write the file.'},
                                                           {'type': 'tool_use', 'id': 'tu_1', 'name': 'Write',
                                                            'input': {'file_path': 'fake_output.txt'}}],
                                              'usage': {'input_tokens': 5, 'output_tokens': 5}}})
        Path('fake_output.txt').write_text('written by fake claude\n', encoding='utf-8')
        out({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tu_1',
                                                      'content': [{'type': 'text', 'text': 'File written'}]}]}})
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Done: wrote fake_output.txt',
             'total_cost_usd': 0.0123, 'usage': {'input_tokens': 100, 'output_tokens': 20, 'cache_read_input_tokens': 7}})
        return 0
    if scenario == 'limit_first':
        marker = Path('.limit_first')
        if not marker.exists():
            marker.write_text('x', encoding='utf-8')
            out({'type': 'result', 'subtype': 'success', 'is_error': True,
                 'result': f'Claude AI usage limit reached|{int(time.time()) + 7200}'})
            return 1
        scenario = 'success'
    if scenario == 'hold':
        time.sleep(float(rest or 1))
        scenario = 'success'
    if scenario == 'stream':   # live output first, then wait <secs> (UI screenshots of a running run)
        out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Reading the project files first.'},
                                                           {'type': 'tool_use', 'id': 'tu_1', 'name': 'Read',
                                                            'input': {'file_path': 'README.md'}}]}})
        out({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'tu_1',
                                                      'content': [{'type': 'text', 'text': 'File read'}]}]}})
        out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Now drafting the change.'}]}})
        time.sleep(float(rest or 1))
        scenario = 'success'
    if scenario == 'success':
        out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'ok'})
        return 0
    if scenario == 'limit':
        message = rest or f'Claude AI usage limit reached|{int(time.time()) + 7200}'
        out({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': message})
        return 1
    if scenario == 'stderr_limit':
        sys.stderr.write((rest or 'Error: usage limit reached') + '\n')
        return 1
    if scenario == 'error_result':
        out({'type': 'result', 'subtype': 'error_during_execution', 'is_error': True, 'result': 'something broke'})
        return 0
    if scenario == 'talk_limit':
        out({'type': 'assistant', 'message': {'content': [
            {'type': 'text', 'text': 'Note: when the usage limit reached message appears, resets 3pm.'}]}})
        return 2
    if scenario == 'crash':
        sys.stdout.write('{"type": "assistant", "message": {\n')
        sys.stdout.write('not json at all\n')
        sys.stdout.flush()
        sys.stderr.write('Traceback (most recent call last):\nRuntimeError: fake crash\n')
        return 3
    if scenario == 'slow':
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        (config / 'child.pid').write_text(str(child.pid), encoding='utf-8')
        out({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'working slowly'}]}})
        time.sleep(120)
        return 0
    out({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': f'unknown scenario {scenario}'})
    return 9


if __name__ == '__main__':
    sys.exit(main())
