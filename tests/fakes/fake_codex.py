"""Fake Codex CLI for tests (stdlib only). Prints ``codex exec --json`` lines. The scenario is the first word of the
prompt read from stdin (``exec ... -`` reads the prompt from stdin):

  success        thread/turn/item lines, a command and a file change, turn.completed usage; exit 0
  legacy         the older {"id", "msg": {...}} line format; exit 0
  limit <msg>    error + turn.failed with <msg> (default: "hit your usage limit ... try again in 2 hours 5 minutes"); exit 1
  crash          garbled stdout, stderr panic; exit 101
  slow           sleeps; killed by tests
Every run records argv, cwd and environment variable names in $CODEX_HOME/invocation.json.
"""
import json
import os
from pathlib import Path
import sys
import time


def out(obj):
    sys.stdout.write(json.dumps(obj) + '\n')
    sys.stdout.flush()


def main():
    prompt = sys.stdin.read()
    home = Path(os.environ.get('CODEX_HOME', '.'))
    home.mkdir(parents=True, exist_ok=True)
    (home / 'invocation.json').write_text(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(), 'prompt': prompt,
                                                      'env': sorted(os.environ)}), encoding='utf-8')
    words = prompt.split(None, 1)
    scenario = words[0] if words else 'success'
    rest = words[1].strip() if len(words) > 1 else ''
    if scenario == 'success':
        out({'type': 'thread.started', 'thread_id': 'fake-thread'})
        out({'type': 'turn.started'})
        out({'type': 'item.completed', 'item': {'id': 'item_0', 'type': 'reasoning', 'text': 'thinking'}})
        out({'type': 'item.started', 'item': {'id': 'item_1', 'type': 'command_execution', 'command': 'ls',
                                              'status': 'in_progress'}})
        out({'type': 'item.completed', 'item': {'id': 'item_1', 'type': 'command_execution', 'command': 'ls',
                                                'aggregated_output': 'README.md\n', 'exit_code': 0, 'status': 'completed'}})
        Path('codex_output.txt').write_text('written by fake codex\n', encoding='utf-8')
        out({'type': 'item.completed', 'item': {'id': 'item_2', 'type': 'file_change', 'status': 'completed',
                                                'changes': [{'path': 'codex_output.txt', 'kind': 'add'}]}})
        out({'type': 'item.completed', 'item': {'id': 'item_3', 'type': 'agent_message', 'text': 'All done.'}})
        out({'type': 'turn.completed', 'usage': {'input_tokens': 200, 'cached_input_tokens': 50, 'output_tokens': 30}})
        return 0
    if scenario == 'legacy':
        out({'id': '0', 'msg': {'type': 'task_started'}})
        out({'id': '0', 'msg': {'type': 'agent_message', 'message': 'legacy hello'}})
        out({'id': '0', 'msg': {'type': 'token_count', 'input_tokens': 10, 'output_tokens': 4}})
        out({'id': '0', 'msg': {'type': 'task_complete', 'last_agent_message': 'legacy hello'}})
        return 0
    if scenario == 'limit':
        message = rest or ("You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes.")
        out({'type': 'thread.started', 'thread_id': 'fake-thread'})
        out({'type': 'error', 'message': message})
        out({'type': 'turn.failed', 'error': {'message': message}})
        return 1
    if scenario == 'crash':
        sys.stdout.write('\x00\xff garbled {{{\n')
        sys.stdout.flush()
        sys.stderr.write("thread 'main' panicked at fake\n")
        return 101
    if scenario == 'slow':
        out({'type': 'turn.started'})
        time.sleep(120)
        return 0
    return 9


if __name__ == '__main__':
    sys.exit(main())
