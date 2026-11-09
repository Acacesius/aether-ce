"""Adapter sandbox launcher (AHS-04, ADAPTER_PROTOCOL.md §3). Standalone: stdlib only, imports nothing from aether.

    python3 -I -S -B adapter_sandbox.py SCRATCH SCRIPT [ARGS...]

Confines the current process, then runs the adapter SCRIPT in-process (runpy), so no execve is ever needed after
confinement and execve can be denied outright:

1. rlimits: no core dumps, bounded open files, file size and address space.
2. Landlock: filesystem = read/execute on system library dirs, the interpreter's stdlib and the script's directory;
   read/write on SCRATCH; /dev/null; nothing else (no data dir, no home, no tokens). ABI >= 4 also denies TCP
   bind/connect; ABI >= 6 denies signals and abstract unix sockets outside the sandbox.
3. seccomp (after PR_SET_NO_NEW_PRIVS): no new processes (fork, vfork, clone without CLONE_THREAD, clone3 -> ENOSYS so
   libc falls back to clone), no execve/execveat, no socket/socketpair, no io_uring, ptrace, bpf, perf, userfaultfd,
   unshare or setns.

Any step that cannot be applied exits 70 with ``sandbox_unavailable: <step>`` on stderr. It never runs unconfined.
"""
import ctypes
import errno
import os
import platform
import runpy
import struct
import sys

try:                                   # POSIX only; importing this module must work everywhere
    import resource
except ImportError:                    # Windows
    resource = None

EXIT_UNAVAILABLE = 70

# Landlock (include/uapi/linux/landlock.h)
SYS_LANDLOCK_CREATE_RULESET, SYS_LANDLOCK_ADD_RULE, SYS_LANDLOCK_RESTRICT_SELF = 444, 445, 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_TRUNCATE, FS_IOCTL_DEV = 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1
SCOPE_ABSTRACT_UNIX_SOCKET, SCOPE_SIGNAL = 1 << 0, 1 << 1
FILE_RIGHTS = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV
READ_RIGHTS = FS_EXECUTE | FS_READ_FILE | FS_READ_DIR

# seccomp (include/uapi/linux/seccomp.h, filter.h)
PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 38, 22, 2
RET_ALLOW, RET_KILL_PROCESS = 0x7FFF0000, 0x80000000
RET_EPERM, RET_ENOSYS = 0x00050000 | errno.EPERM, 0x00050000 | errno.ENOSYS
LD_W_ABS, JEQ_K, JGE_K, JSET_K, RET_K = 0x20, 0x15, 0x35, 0x45, 0x06
CLONE_THREAD = 0x00010000
X32_SYSCALL_BIT = 0x40000000
ARCH = {
    'x86_64': {'audit': 0xC000003E, 'clone': 56, 'clone3': 435, 'x32': True,
               'deny': {'fork': 57, 'vfork': 58, 'execve': 59, 'execveat': 322, 'socket': 41, 'socketpair': 53,
                        'io_uring_setup': 425, 'ptrace': 101, 'bpf': 321, 'perf_event_open': 298,
                        'userfaultfd': 323, 'unshare': 272, 'setns': 308}},
    'aarch64': {'audit': 0xC00000B7, 'clone': 220, 'clone3': 435, 'x32': False,
                'deny': {'execve': 221, 'execveat': 281, 'socket': 198, 'socketpair': 199, 'io_uring_setup': 425,
                         'ptrace': 117, 'bpf': 280, 'perf_event_open': 241, 'userfaultfd': 282, 'unshare': 97,
                         'setns': 268}},
}
SYSTEM_LIB_DIRS = ('/usr/lib', '/usr/lib64', '/lib', '/lib64', '/usr/local/lib')


class Unavailable(Exception):
    pass


def availability():
    """``(True, '')`` when this platform can apply the Linux sandbox, else ``(False, message)``. Evaluated at call time
    and never loads libc, so it is safe to call (and to import this module) on Windows and macOS."""
    if not sys.platform.startswith('linux') or os.name != 'posix':
        return False, ('adapter sandbox unavailable: it needs Linux (Landlock + seccomp); adapters are disabled on '
                       'this platform and are never run unsandboxed. CLI executors are unaffected.')
    if resource is None or not hasattr(ctypes, 'CDLL'):
        return False, 'adapter sandbox unavailable: resource/ctypes support missing; adapters are disabled'
    if platform.machine() not in ARCH:
        return False, f'adapter sandbox unavailable: unsupported architecture {platform.machine()}; adapters are disabled'
    return True, ''


def _libc():
    ok, message = availability()
    if not ok:
        raise Unavailable(message)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    libc.prctl.restype = ctypes.c_int
    return libc


def _check(result, step):
    if result < 0:
        raise Unavailable(f'{step} ({errno.errorcode.get(ctypes.get_errno(), "?")})')
    return result


def limit_resources():
    if resource is None:
        raise Unavailable('resource limits unavailable on this platform')
    for name, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 64),
                        (resource.RLIMIT_FSIZE, 64 * 1024 * 1024), (resource.RLIMIT_AS, 1024 * 1024 * 1024)):
        soft, hard = resource.getrlimit(name)
        cap = value if hard == resource.RLIM_INFINITY else min(value, hard)
        resource.setrlimit(name, (cap, cap))


def landlock(libc, read_dirs, scratch):
    abi = libc.syscall(SYS_LANDLOCK_CREATE_RULESET, None, ctypes.c_size_t(0),
                       ctypes.c_uint32(LANDLOCK_CREATE_RULESET_VERSION))
    _check(abi, 'landlock')
    handled = (1 << 13) - 1                      # ABI 1: EXECUTE .. MAKE_SYM
    if abi >= 2:
        handled |= 1 << 13                       # REFER
    if abi >= 3:
        handled |= FS_TRUNCATE
    if abi >= 5:
        handled |= FS_IOCTL_DEV
    if abi >= 6:
        attr = struct.pack('=QQQ', handled, NET_BIND_TCP | NET_CONNECT_TCP, SCOPE_ABSTRACT_UNIX_SOCKET | SCOPE_SIGNAL)
    elif abi >= 4:
        attr = struct.pack('=QQ', handled, NET_BIND_TCP | NET_CONNECT_TCP)
    else:
        attr = struct.pack('=Q', handled)
    buffer = ctypes.create_string_buffer(attr, len(attr))
    ruleset = _check(libc.syscall(SYS_LANDLOCK_CREATE_RULESET, buffer, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0)),
                     'landlock ruleset')
    rules = [(path, READ_RIGHTS) for path in read_dirs] + [(scratch, handled), ('/dev/null', FS_READ_FILE | FS_WRITE_FILE | FS_TRUNCATE)]
    for path, rights in rules:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
        try:
            if not os.path.isdir(path):
                rights &= FILE_RIGHTS
            rule = ctypes.create_string_buffer(struct.pack('=Qi', rights & handled, fd), 12)
            _check(libc.syscall(SYS_LANDLOCK_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(LANDLOCK_RULE_PATH_BENEATH),
                                rule, ctypes.c_uint32(0)), f'landlock rule {path}')
        finally:
            os.close(fd)
    _check(libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0)), 'landlock restrict')
    os.close(ruleset)
    return abi


def seccomp_program(arch):
    """Return the filter as a list of (code, jt, jf, k); jumps are resolved from symbolic labels."""
    spec = ARCH[arch]
    prog = [(LD_W_ABS, 0, 0, 4), (JEQ_K, 'arch_ok', 'kill', spec['audit']), ('label', 'arch_ok'), (LD_W_ABS, 0, 0, 0)]
    if spec['x32']:
        prog.append((JGE_K, 'eperm', 'next', X32_SYSCALL_BIT))
    for nr in sorted(spec['deny'].values()):
        prog.append((JEQ_K, 'eperm', 'next', nr))
    prog += [(JEQ_K, 'enosys', 'next', spec['clone3']), (JEQ_K, 'clone', 'next', spec['clone']),
             (RET_K, 0, 0, RET_ALLOW),
             ('label', 'clone'), (LD_W_ABS, 0, 0, 16), (JSET_K, 'allow', 'eperm', CLONE_THREAD),
             ('label', 'allow'), (RET_K, 0, 0, RET_ALLOW),
             ('label', 'eperm'), (RET_K, 0, 0, RET_EPERM),
             ('label', 'enosys'), (RET_K, 0, 0, RET_ENOSYS),
             ('label', 'kill'), (RET_K, 0, 0, RET_KILL_PROCESS)]
    labels, real = {}, []
    for item in prog:
        if item[0] == 'label':
            labels[item[1]] = len(real)
        else:
            real.append(item)
    out = []
    for index, (code, jt, jf, k) in enumerate(real):
        def offset(target):
            if target in (0, 'next'):
                return 0
            delta = labels[target] - index - 1
            assert 0 <= delta < 256, 'seccomp jump out of range'
            return delta
        out.append((code, offset(jt), offset(jf), k))
    return out


def seccomp(libc, arch):
    program = seccomp_program(arch)
    raw = b''.join(struct.pack('=HBBI', *instr) for instr in program)
    filters = ctypes.create_string_buffer(raw, len(raw))

    class Fprog(ctypes.Structure):
        _fields_ = [('len', ctypes.c_ushort), ('filter', ctypes.c_void_p)]
    fprog = Fprog(len(program), ctypes.addressof(filters))
    _check(libc.prctl(PR_SET_SECCOMP, ctypes.c_ulong(SECCOMP_MODE_FILTER), ctypes.byref(fprog), ctypes.c_ulong(0),
                      ctypes.c_ulong(0)), 'seccomp')


def confine(scratch, script):
    arch = platform.machine()
    if arch not in ARCH:
        raise Unavailable(f'unsupported architecture {arch}')
    libc = _libc()
    limit_resources()
    read_dirs = {os.path.dirname(script)}
    read_dirs.update(p for p in SYSTEM_LIB_DIRS if os.path.isdir(p))
    read_dirs.update(p for p in sys.path if p and os.path.isdir(p))
    _check(libc.prctl(PR_SET_NO_NEW_PRIVS, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0)),
           'no_new_privs')
    abi = landlock(libc, sorted(read_dirs), scratch)
    seccomp(libc, arch)
    return abi


def main(argv):
    if len(argv) < 3 or not all(os.path.isabs(p) for p in argv[1:3]):
        sys.stderr.write('usage: adapter_sandbox.py SCRATCH SCRIPT [ARGS...] (absolute paths)\n')
        return 64
    scratch, script = argv[1], argv[2]
    try:
        confine(scratch, script)
    except (Unavailable, OSError) as error:
        sys.stderr.write(f'sandbox_unavailable: {error}\n')
        return EXIT_UNAVAILABLE
    sys.argv = [script] + argv[3:]
    sys.path.insert(0, os.path.dirname(script))
    runpy.run_path(script, run_name='__main__')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
