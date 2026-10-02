"""Bounded HAFlow-owned subprocesses; external Agent tools remain outside this API."""
import os
import selectors
import signal
import subprocess
import tempfile
import time
from .supervisor.state import redact_text


def validate_tool_input(argv, input_data=b'', *, timeout=30, output_limit=65536):
    if not isinstance(argv, (list, tuple)) or not argv or len(argv) > 128:
        raise ValueError('argv exceeds argument budget')
    if any(not isinstance(arg, str) for arg in argv):
        raise ValueError('argv must contain strings')
    if any('\x00' in arg for arg in argv):
        raise ValueError('NUL in argv')
    if any(len(arg.encode()) > 4096 for arg in argv) or sum(len(arg.encode()) for arg in argv) > 65536:
        raise ValueError('argv exceeds byte budget')
    if isinstance(input_data, str):
        input_data = input_data.encode()
    if not isinstance(input_data, bytes):
        raise ValueError('input must be text or bytes')
    if b'\x00' in input_data:
        raise ValueError('NUL in input')
    if len(input_data) > 1024 * 1024:
        raise ValueError('input exceeds byte budget')
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ValueError('timeout exceeds execution budget')
    if type(output_limit) is not int or not 1 <= output_limit <= 1024 * 1024:
        raise ValueError('output_limit exceeds byte budget')
    return input_data


def run_bounded(argv, input_data=b'', *, timeout=30, output_limit=65536, cwd=None, env=None):
    data = validate_tool_input(argv, input_data, timeout=timeout, output_limit=output_limit)
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    status, total = 'completed', 0
    started = time.monotonic()
    with tempfile.TemporaryFile() as stdin:
        stdin.write(data)
        stdin.seek(0)
        process = subprocess.Popen(argv, stdin=stdin, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=env,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for name in buffers:
                pipe = getattr(process, name)
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, name)
            try:
                while selector.get_map():
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        status = 'timeout'
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        allowed = max(0, output_limit - total)
                        buffers[key.data].extend(chunk[:allowed])
                        total += min(len(chunk), allowed)
                        if len(chunk) > allowed:
                            status = 'output_limit'
                            break
                    if status != 'completed':
                        break
                if status == 'completed':
                    try:
                        process.wait(timeout=max(0.001, timeout - (time.monotonic() - started)))
                    except subprocess.TimeoutExpired:
                        status = 'timeout'
            finally:
                if process.poll() is None or status != 'completed':
                    # The process group was created by this exact Popen call;
                    # never act on a cached Pane/PID from a different execution.
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=1)
                    if status != 'completed':
                        # Parent exit is not proof that its owned children
                        # obeyed SIGTERM or stopped holding the output pipes.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                process.stdout.close()
                process.stderr.close()
    # A budget stop may cut a credential before the redactor can recognize
    # it. Drop capped output; timeout retains only complete records.
    if status == 'output_limit':
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    elif status == 'timeout':
        buffers = {name: value[:value.rfind(b'\n') + 1] for name, value in buffers.items()}
    stdout = redact_text(bytes(buffers['stdout']).decode('utf-8', errors='replace')).encode()[:output_limit].decode('utf-8', errors='ignore')
    remaining = output_limit - len(stdout.encode())
    stderr = redact_text(bytes(buffers['stderr']).decode('utf-8', errors='replace')).encode()[:remaining].decode('utf-8', errors='ignore')
    return {'status': status, 'exit_code': process.returncode,
            'stdout': stdout, 'stderr': stderr,
            'side_effects': 'possible' if status == 'completed' else 'unknown',
            'owned_process_group': process.pid,
            'elapsed_seconds': time.monotonic() - started}
