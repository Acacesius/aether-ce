"""AHS-04: protocol codec, negotiation, hash chain, offline verification, sandbox filter shape (no processes)."""
import ast
import copy
import json
from pathlib import Path
import unittest

from aether.studio import adapter_protocol as proto
from aether.studio import adapter_sandbox, schema

SESSION = 'ab' * 16


def line(obj):
    return (json.dumps(obj) + '\n').encode()


def chain(events, session=SESSION):
    """Build valid event messages [(kind, data, content|None)] -> list of messages."""
    head, out = proto.chain_seed(session), []
    for seq, (kind, data, content) in enumerate(events, 1):
        message = {'type': 'event', 'seq': seq, 'prev': head, 'kind': kind, 'data': data}
        if content is not None:
            message['content'] = content
        head = proto.link_hash(seq, head, kind, data)
        out.append(message)
    return out


class CodecTests(unittest.TestCase):
    def test_valid_messages_decode(self):
        for message in ({'type': 'hello', 'protocol': proto.PROTOCOL, 'name': 'x', 'capabilities': []},
                        {'type': 'accepted', 'ref': 'r1'}, {'type': 'pong', 'n': 1},
                        {'type': 'event', 'seq': 1, 'prev': 'a', 'kind': 'log', 'data': {}}):
            self.assertEqual(proto.decode(line(message)), message)

    def test_rejections(self):
        cases = {
            'oversized': b'{"type":"pong","n":1,"pad":"' + b'x' * proto.MAX_LINE + b'"}\n',
            'truncated_line': b'{"type":"pong","n":1}',
            'invalid_json': b'not json\n',
            'duplicate_field': b'{"type":"pong","n":1,"n":2}\n',
            'unknown_message': line({'type': 'exec', 'argv': ['/bin/sh']}),
            'bad_fields': line({'type': 'pong', 'n': 1, 'shell': 'sh'}),
        }
        for code, raw in cases.items():
            with self.subTest(code=code), self.assertRaises(proto.ProtocolError) as caught:
                proto.decode(raw)
            self.assertEqual(caught.exception.code, code)
        with self.assertRaises(proto.ProtocolError):
            proto.decode(line([1, 2]))
        with self.assertRaises(proto.ProtocolError):
            proto.decode(b'\xff\xfe\n')

    def test_content_only_allowed_on_events(self):
        with self.assertRaises(proto.ProtocolError):
            proto.decode(line({'type': 'accepted', 'ref': 'r', 'content': 'x'}))

    def test_inbound_set_is_data_only(self):
        # No inbound message type can ask Studio to execute, connect, read files or change grants.
        self.assertEqual(set(proto.INBOUND), {'hello', 'accepted', 'pong', 'event'})
        self.assertEqual(set(proto.EVENT_KINDS), {'log', 'progress', 'status', 'artifact', 'result'})


class NegotiationTests(unittest.TestCase):
    def hello(self, caps, protocol=proto.PROTOCOL):
        return {'type': 'hello', 'protocol': protocol, 'name': 'adapter', 'capabilities': caps}

    def test_scopes_intersect_grants_and_allowlist(self):
        self.assertEqual(proto.negotiate(self.hello(['observe', 'submit', 'cancel']), ['observe']), ['observe'])
        self.assertEqual(proto.negotiate(self.hello(['observe']), ['observe', 'submit', 'cancel']), ['observe'])
        self.assertEqual(proto.negotiate(self.hello(['observe', 'submit']), []), [])

    def test_version_mismatch_refused(self):
        with self.assertRaises(proto.ProtocolError) as caught:
            proto.negotiate(self.hello(['observe'], 'aether.studio.adapter/v2'), ['observe'])
        self.assertEqual(caught.exception.code, 'version_mismatch')
        self.assertIn(proto.PROTOCOL, str(caught.exception))

    def test_unknown_capability_refused(self):
        for caps in (['observe', 'shell'], ['network'], ['exec'], ['adapter.submit']):
            with self.subTest(caps=caps), self.assertRaises(proto.ProtocolError) as caught:
                proto.negotiate(self.hello(caps), ['observe', 'submit', 'cancel'])
            self.assertEqual(caught.exception.code, 'unknown_capability')

    def test_malformed_capabilities_refused(self):
        for caps in ('observe', [1], ['observe', 'observe'], ['observe'] * 9):
            with self.subTest(caps=caps), self.assertRaises(proto.ProtocolError):
                proto.negotiate(self.hello(caps), ['observe'])

    def test_allowlist_is_the_capability_registry(self):
        self.assertEqual(proto.allowed_scopes(), frozenset({'observe', 'submit', 'cancel'}))
        for scope in proto.KNOWN_CAPABILITIES:
            self.assertIn(proto.studio_capability(scope), proto.capabilities.REGISTRY)


class ChainTests(unittest.TestCase):
    EVENTS = [('status', {'state': 'running'}, None), ('log', {'stream': 'stdout', 'text': 'hi'}, None),
              ('progress', {'done': 1, 'total': 2}, None),
              ('artifact', {'name': 'out', 'media_type': 'text/plain', 'sha256': schema.sha256_hex('abc'), 'size': 3},
               'abc'),
              ('result', {'exit_code': 0}, None)]

    def run_chain(self, messages):
        head = proto.chain_seed(SESSION)
        for seq, message in enumerate(messages, 1):
            head = proto.check_event(message, expected_seq=seq, head=head, outputs=('out',))
        return head

    def test_valid_chain(self):
        self.assertRegex(self.run_chain(chain(self.EVENTS)), r'^[0-9a-f]{64}$')

    def expect(self, messages, code):
        with self.assertRaises(proto.ProtocolError) as caught:
            self.run_chain(messages)
        self.assertEqual(caught.exception.code, code)

    def test_sequence_gap_replay_and_types(self):
        messages = chain(self.EVENTS)
        self.expect([messages[0], messages[2]], 'bad_sequence')
        self.expect([messages[0], messages[0]], 'bad_sequence')
        bad = copy.deepcopy(messages[0])
        bad['seq'] = True
        self.expect([bad], 'bad_sequence')

    def test_tampered_prev_or_data_breaks_chain(self):
        messages = chain(self.EVENTS)
        forged = copy.deepcopy(messages[1])
        forged['prev'] = '0' * 64
        self.expect([messages[0], forged], 'bad_chain')
        # altering data of event 1 changes h1, so event 2's prev no longer matches
        first = copy.deepcopy(messages[0])
        first['data']['state'] = 'waiting'
        self.expect([first, messages[1]], 'bad_chain')

    def test_kind_schemas(self):
        bad_events = [('log', {'stream': 'fd3', 'text': 'x'}), ('log', {'stream': 'stdout', 'text': 'x' * 4001}),
                      ('log', {'stream': 'stdout', 'text': 'x', 'cmd': 'sh'}), ('progress', {'done': 3, 'total': 2}),
                      ('status', {'state': 'succeeded'}), ('result', {'exit_code': 256}),
                      ('result', {'exit_code': '0'}), ('shell', {'argv': ['sh']})]
        for kind, data in bad_events:
            with self.subTest(kind=kind, data=data):
                self.expect(chain([(kind, data, None)]), 'bad_event')

    def test_artifact_rules(self):
        good = self.EVENTS[3]
        mismatch = chain([(good[0], dict(good[1], size=4), good[2])])
        self.expect(mismatch, 'artifact_mismatch')
        undeclared = chain([(good[0], dict(good[1], name='loot'), good[2])])
        self.expect(undeclared, 'artifact_undeclared')
        no_content = chain([(good[0], good[1], None)])
        self.expect(no_content, 'bad_event')
        stray = chain([('log', {'stream': 'stdout', 'text': 'x'}, 'payload')])
        self.expect(stray, 'bad_event')

    def test_event_limit(self):
        message = chain([('status', {'state': 'running'}, None)])[0]
        message['seq'] = proto.MAX_EVENTS + 1
        with self.assertRaises(proto.ProtocolError) as caught:
            proto.check_event(message, expected_seq=proto.MAX_EVENTS + 1, head=message['prev'])
        self.assertEqual(caught.exception.code, 'event_limit')


class StoredVerificationTests(unittest.TestCase):
    def stored(self):
        seed = proto.chain_seed(SESSION)
        events = [{'seq': 1, 'type': 'adapter.session', 'data': {'session': SESSION, 'chain_seed': seed}}]
        head = seed
        for i, (kind, data) in enumerate([('status', {'state': 'running'}), ('result', {'exit_code': 0})], 1):
            digest = proto.link_hash(i, head, kind, data)
            events.append({'seq': i + 1, 'type': 'adapter.event',
                           'data': {'session': SESSION, 'i': i, 'prev': head, 'sha256': digest, 'kind': kind,
                                    'data': data, 'redacted': False}})
            head = digest
        return events

    def test_verifies_and_detects_tampering(self):
        events = self.stored()
        self.assertEqual(proto.verify_stored(events), {'sessions': 1, 'events': 2, 'recomputed': 2, 'redacted': 0})
        tampered = copy.deepcopy(events)
        tampered[2]['data']['data']['exit_code'] = 1
        with self.assertRaises(proto.ProtocolError):
            proto.verify_stored(tampered)
        dropped = [events[0], events[2]]
        with self.assertRaises(proto.ProtocolError):
            proto.verify_stored(dropped)
        reseeded = copy.deepcopy(events)
        reseeded[0]['data']['chain_seed'] = '0' * 64
        with self.assertRaises(proto.ProtocolError):
            proto.verify_stored(reseeded)

    def test_redacted_link_checked_for_continuity(self):
        events = self.stored()
        events[1]['data']['redacted'] = True
        events[1]['data']['data'] = {'state': '[REDACTED]'}
        self.assertEqual(proto.verify_stored(events)['redacted'], 1)


class SandboxShapeTests(unittest.TestCase):
    def test_sandbox_is_standalone_stdlib(self):
        tree = ast.parse(Path(adapter_sandbox.__file__).read_text())
        modules = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        modules |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        self.assertNotIn('aether', modules)
        self.assertNotIn('subprocess', modules)

    def test_seccomp_program_denies_process_exec_and_sockets(self):
        for arch, spec in adapter_sandbox.ARCH.items():
            program = adapter_sandbox.seccomp_program(arch)
            self.assertLess(len(program), 64)
            denied = {k for code, _, _, k in program if code == adapter_sandbox.JEQ_K}
            for name in ('execve', 'execveat', 'socket', 'socketpair', 'io_uring_setup', 'ptrace'):
                self.assertIn(spec['deny'][name], denied, (arch, name))
            self.assertIn(spec['clone'], denied)
            self.assertIn(spec['clone3'], denied)
            self.assertEqual(program[-1], (adapter_sandbox.RET_K, 0, 0, adapter_sandbox.RET_KILL_PROCESS))


if __name__ == '__main__':
    unittest.main()
