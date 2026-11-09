"""Hostile adapter fixture. ``argv[1]`` picks the misbehaviour; Studio must reject or contain every one.

Protocol abuse: oversized, bad_seq, bad_chain, bad_version, unknown_cap, exec_request, garbage, dup_keys,
event_before_accept, undeclared_artifact. Lifecycle: crash, hang, ignore_cancel.
Privilege probes (escape): read the environment, read a file outside the sandbox (argv[2]), spawn a shell, exec, fork,
open TCP to a loopback port (argv[3]), signal the parent. Each outcome is reported as a log event; the run still ends
with a valid result so the test can read every report.
Also: leaky_log (emits a token-shaped string; Studio must store it redacted).
"""
import os
import socket
import subprocess
import sys

from adapter_kit import Peer

MODE = sys.argv[1]
peer = Peer()

if MODE == 'bad_version':
    peer.handshake(protocol='aether.studio.adapter/v0')
    sys.exit(0)
if MODE == 'unknown_cap':
    peer.handshake(capabilities=('observe', 'submit', 'shell', 'network'))
    sys.exit(0)

welcome = peer.handshake()
assert welcome['type'] == 'welcome', welcome
request = peer.next_request()
if request['type'] == 'bye':
    sys.exit(0)

if MODE == 'event_before_accept':
    peer.log('too early')
    peer.serve_pings(30)
    sys.exit(0)
if MODE == 'crash':
    peer.send({'type': 'accepted', 'ref': 'hostile-crash'})
    peer.log('about to crash')
    os._exit(3)

peer.send({'type': 'accepted', 'ref': 'hostile-' + MODE})

if MODE == 'oversized':
    peer.log('x' * (200 * 1024))
elif MODE == 'bad_seq':
    peer.log('one')
    peer.event('log', {'stream': 'stdout', 'text': 'three'}, seq=3)
elif MODE == 'bad_chain':
    peer.log('one')
    peer.event('log', {'stream': 'stdout', 'text': 'forged'}, prev='0' * 64)
elif MODE == 'exec_request':
    peer.send({'type': 'exec', 'argv': ['/bin/sh', '-c', 'id']})
elif MODE == 'garbage':
    peer.raw(b'this is not json\n')
elif MODE == 'dup_keys':
    peer.raw(b'{"type":"pong","n":1,"n":1}\n')
elif MODE == 'undeclared_artifact':
    peer.event('artifact', {'name': 'loot', 'media_type': 'text/plain', 'sha256': '0' * 64, 'size': 1}, 'x')
elif MODE == 'hang':
    peer.log('going silent')
    while True:
        os.read(0, 65536)   # read and ignore pings forever
elif MODE == 'ignore_cancel':
    peer.log('ignoring cancel')
    while True:
        os.read(0, 65536)
elif MODE == 'leaky_log':
    peer.log('leaked Bearer ' + 'A' * 30 + ' and more')
    peer.event('result', {'exit_code': 0})
elif MODE == 'escape':
    secret_path, port = sys.argv[2], int(sys.argv[3])

    def attempt(name, fn):
        try:
            outcome = 'ALLOWED:' + str(fn())[:60]
        except Exception as error:   # report the exception class only
            outcome = 'DENIED:' + type(error).__name__
        peer.log(f'{name}={outcome}')

    peer.log('env=' + ','.join(sorted(os.environ)))
    attempt('read_secret', lambda: open(secret_path).read())
    attempt('list_home', lambda: os.listdir(os.path.dirname(secret_path)))
    attempt('read_owner_token', lambda: open(os.path.join(os.path.dirname(secret_path), 'owner.' + 'token')).read())
    attempt('shell', lambda: subprocess.run(['/bin/sh', '-c', 'echo pwned'], capture_output=True).returncode)
    def system():
        if os.system('true') != 0:     # 127 << 8: libc could not spawn /bin/sh
            raise PermissionError('spawn refused')
        return 'ran'
    attempt('system', system)
    attempt('fork', lambda: os.fork())
    attempt('tcp', lambda: socket.create_connection(('127.0.0.1', port), timeout=2))
    attempt('unix_socket', lambda: socket.socket(socket.AF_UNIX))
    attempt('signal_parent', lambda: os.kill(os.getppid(), 0))
    attempt('scratch_write', lambda: open(os.path.join(os.getcwd(), 'note.txt'), 'w').write('ok'))
    attempt('execv', lambda: os.execv('/bin/sh', ['sh', '-c', 'echo pwned']))
    peer.event('result', {'exit_code': 0})
else:
    raise SystemExit('unknown mode ' + MODE)
peer.serve_pings(30)
