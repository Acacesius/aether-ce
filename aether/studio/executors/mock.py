"""DEMO executor. Deterministic scripted steps; performs no real action. Every event it causes is demo=true.

Steps: mock:success | mock:fail:<code> | mock:sleep:<seconds> | mock:hang | mock:artifact:<name> | mock:approval:<cap> | mock:verdict:pass|fail
(mock:verdict is a scripted DEMO review for a reviewer-role stage; it judges nothing.)
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import threading

from aether.studio.executors.base import Handle

STEP_RE = re.compile(r'^mock:(success|hang|fail:(?P<code>[0-9]{1,3})|sleep:(?P<secs>[0-9]{1,4}(\.[0-9]{1,3})?)|'
                     r'artifact:(?P<name>[a-z][a-z0-9_-]{0,39})|approval:(?P<cap>[a-z][a-z0-9_.-]{0,63})|verdict:(?P<verdict>pass|fail))$')
PREFIX = '[DEMO] '


class MockExecutor:
    kind = 'mock'
    demo = True

    def capabilities(self):
        return frozenset({'demo.run', 'artifact.write'})

    def health(self):
        return {'status': 'ready', 'observed_utc': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'detail': 'DEMO mock executor (simulated, no real actions)'}

    def start(self, run, stage, emit):
        match = STEP_RE.match(stage.executor_step)
        if not match:
            raise ValueError('unknown mock step')
        handle = Handle(run.id, stage.key, stage.attempt)
        state = {'done': False, 'exit_code': None, 'blocked': None, 'cancel': threading.Event()}
        handle.state = state

        def log(text):
            emit('stage.log', {'stream': 'stdout', 'text': PREFIX + text, 'redacted': False, 'demo': True})

        def body():
            step = stage.executor_step
            log(f'simulated step {step} started; no real action is taken')
            code = 0
            if step == 'mock:success':
                log('simulated step succeeded')
            elif match.group('code') is not None:
                code = max(1, min(255, int(match.group('code'))))
                log(f'simulated failure, exit {code}')
            elif match.group('secs') is not None:
                if state['cancel'].wait(float(match.group('secs'))):
                    code = 143
                    log('simulated step interrupted by cancel')
                else:
                    log('simulated sleep finished')
            elif step == 'mock:hang':
                state['cancel'].wait()
                code = 143
                log('simulated hang interrupted')
            elif match.group('name') is not None:
                name = match.group('name')
                emit('artifact.write', {'name': name, 'media_type': 'text/plain', 'demo': True,
                                        'content': f'DEMO artifact "{name}" from run {run.id} stage {stage.key}. '
                                                   'Synthetic content; no real work was performed.\n'})
                log(f'simulated artifact {name} written')
            elif match.group('verdict') is not None:
                emit('stage.verdict', {'verdict': match.group('verdict'),
                                       'reason': 'DEMO scripted verdict; nothing was actually reviewed'})
                log(f'simulated review verdict {match.group("verdict")}')
            elif match.group('cap') in stage.approved:
                log(f'simulated step ran with the owner-granted approval for {match.group("cap")}')
            else:
                state['blocked'] = match.group('cap')
                log(f'simulated step requires approval for {state["blocked"]}')
                return
            state['exit_code'] = code
            state['done'] = True

        thread = threading.Thread(target=body, name=f'mock-{run.id}-{stage.key}', daemon=True)
        handle.extra['thread'] = thread
        thread.start()
        return handle

    def poll(self, handle):
        state = handle.state
        if state['blocked']:
            return {'state': 'blocked', 'exit_code': None, 'capability': state['blocked']}
        if state['done']:
            return {'state': 'exited', 'exit_code': state['exit_code']}
        return {'state': 'running', 'exit_code': None}

    def cancel(self, handle, grace_seconds):
        handle.state['cancel'].set()
