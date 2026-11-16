"""Run one generated function against input-only probes in a Linux sandbox."""

import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

MAX_CODE = 48_000
MAX_OUTPUT = 65_536

# Bubblewrap isolates the filesystem, users, processes, and IPC. The seccomp
# filter denies sockets even on hosts where loopback setup in a new netns fails.
BLOCKED = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "sendmsg", "recvmsg", "sendmmsg", "recvmmsg", "io_uring_setup",
    "io_uring_enter", "io_uring_register", "clone", "clone3", "fork", "vfork",
    "unshare", "setns", "ptrace", "process_vm_readv", "process_vm_writev",
    "bpf", "userfaultfd", "keyctl", "add_key", "request_key", "perf_event_open",
    "mount", "umount2", "pivot_root", "open_by_handle_at", "name_to_handle_at",
)

PROBE = """
import json
import runpy
import sys

inputs = json.load(sys.stdin)
evaluate = runpy.run_path('/candidate.py')['evaluate']
results = [evaluate(item) for item in inputs]
print(json.dumps(results, allow_nan=False))
"""


class SandboxError(Exception):
    """The execution environment failed its startup check."""


def _export_filter(fd):
    try:
        lib = ctypes.CDLL("libseccomp.so.2")
    except OSError as exc:
        raise SandboxError("libseccomp.so.2 is required") from exc
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
    ]
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise SandboxError("could not initialize seccomp")
    try:
        for name in BLOCKED:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0):
                raise SandboxError(f"could not block syscall {name}")
        if lib.seccomp_export_bpf(context, fd):
            raise SandboxError("could not export seccomp filter")
    finally:
        lib.seccomp_release(context)


class Sandbox:
    def __init__(self, timeout_s):
        self.timeout_s = timeout_s
        self.bwrap = shutil.which("bwrap")
        self.prlimit = shutil.which("prlimit")
        if not self.bwrap or not self.prlimit or not Path("/usr/bin/python3").is_file():
            raise SandboxError("Linux with bubblewrap, prlimit, and /usr/bin/python3 is required")

    def verify(self):
        checks = """
import os
import socket
def evaluate(_):
    results = {}
    for name, action in {
        'socket': lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        'process': os.fork,
        'host_fs': lambda: open('/etc/passwd'),
    }.items():
        try:
            action()
            results[name] = False
        except OSError:
            results[name] = True
    results['env'] = set(os.environ) <= {'PWD', 'LC_CTYPE'}
    return results
"""
        expected = {"socket": True, "process": True, "host_fs": True, "env": True}
        values, status, details = self._run(checks, [{}])
        if status or values != [expected]:
            raise SandboxError(f"sandbox startup check failed ({status or 'isolation check'}). {details}")

    def _run(self, source, inputs):
        with tempfile.TemporaryDirectory(prefix="llm-profiler-") as folder:
            path = Path(folder) / "candidate.py"
            path.write_text(source, encoding="utf-8")
            with tempfile.TemporaryFile() as rules, tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                _export_filter(rules.fileno())
                rules.seek(0)
                command = [
                    self.prlimit, "--as=268435456", "--cpu=3", "--fsize=65536",
                    "--nofile=128", "--core=0", "--", self.bwrap,
                    "--unshare-all", "--unshare-user", "--share-net", "--disable-userns",
                    "--die-with-parent", "--new-session", "--clearenv",
                    "--ro-bind", str(Path("/usr/bin/python3").resolve()), "/usr/bin/python3",
                    "--ro-bind", "/usr/lib", "/usr/lib", "--ro-bind-try", "/usr/lib64", "/usr/lib64",
                    "--dev", "/dev",
                    "--ro-bind", str(path), "/candidate.py", "--chdir", "/",
                ]
                for alias in ("/bin", "/lib", "/lib64"):
                    if Path(alias).is_symlink():
                        command.extend(("--symlink", os.readlink(alias), alias))
                    elif Path(alias).exists():
                        command.extend(("--ro-bind", alias, alias))
                command.extend(("--seccomp", str(rules.fileno()), "--", "/usr/bin/python3", "-I", "-B", "-c", PROBE))
                try:
                    process = subprocess.run(
                        command, input=json.dumps(inputs).encode(), stdout=stdout, stderr=stderr,
                        timeout=self.timeout_s, pass_fds=(rules.fileno(),), env={"PATH": "/usr/bin:/bin"},
                    )
                except subprocess.TimeoutExpired:
                    return None, "judge_timeout", ""
                if process.returncode:
                    stderr.seek(0)
                    details = stderr.read(400).decode(errors="replace").strip()
                    status = "sandbox_error" if details.startswith("bwrap:") else "candidate_execution_error"
                    return None, status, details if status == "sandbox_error" else ""
                stdout.seek(0)
                try:
                    payload = json.loads(stdout.read(MAX_OUTPUT))
                except (ValueError, RecursionError):
                    return None, "invalid_candidate_output", ""
                if not isinstance(payload, list) or len(payload) != len(inputs):
                    return None, "invalid_candidate_output", ""
                return payload, None, ""

    def evaluate(self, source, cases):
        outputs, status, _ = self._run(source, [case["input"] for case in cases])
        if status:
            return {case["id"]: False for case in cases}, status
        # Canonical JSON keeps False distinct from 0 and accepts key reorderings.
        matches = {}
        for case, output in zip(cases, outputs, strict=True):
            try:
                matches[case["id"]] = (
                    json.dumps(output, sort_keys=True, allow_nan=False)
                    == json.dumps(case["expected"], sort_keys=True, allow_nan=False)
                )
            except (ValueError, RecursionError):
                matches[case["id"]] = False
        return matches, None
