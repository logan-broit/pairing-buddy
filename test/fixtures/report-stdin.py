# Drive the generated hook's three background launches on an owned PTY, with no timing-dependent startup delays.
import fcntl
import json
import os
import pty
import signal
import socket
import subprocess
import sys
import termios
import time
import tty
from contextlib import ExitStack
from pathlib import Path

TIMEOUT = 10
shell, folder, preload = sys.argv[1:]
folder = Path(folder)


def interrupted(signum, frame):
    raise TimeoutError('PTY probe interrupted')


signal.signal(signal.SIGTERM, interrupted)


def running(pid):
    result = subprocess.run(
        ['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=TIMEOUT
    )
    # A zombie has finished native teardown even if its disowning shell has not reaped it.
    return result.returncode == 0 and not result.stdout.strip().startswith('Z')


def wait_exit(pid):
    end = time.monotonic() + TIMEOUT
    while running(pid):
        if time.monotonic() >= end:
            raise TimeoutError(f'report {pid} did not exit')
        time.sleep(0.01)


def line(connection):
    with connection.makefile('rb') as stream:
        return stream.readline()


with ExitStack() as stack:
    master, slave = pty.openpty()
    stack.callback(os.close, master)
    stack.callback(os.close, slave)
    cooked = termios.tcgetattr(slave)
    assert cooked[3] & termios.ICANON and cooked[3] & termios.ECHO
    gate_read, gate_write = os.pipe()
    stack.callback(os.close, gate_read)
    stack.callback(os.close, gate_write)
    barrier = stack.enter_context(socket.socket(socket.AF_UNIX))
    daemon = stack.enter_context(socket.socket(socket.AF_UNIX))
    for server, name in [(barrier, 'barrier'), (daemon, 'daemon')]:
        server.bind(str(folder / name))
        server.listen()
        server.settimeout(TIMEOUT)

    # Run the registered callbacks without a line editor: the controller supplies the cooked -> raw transition.
    # Interactive job control matters: a noninteractive shell may itself redirect background stdin to /dev/null.
    gate = f'read -r gate <&{gate_read}'
    if shell == 'bash':
        args = ['--noprofile', '--norc', '-i', '-c']
        start = 'history -s true; _pair_arm; true'
        finish = '_pair_precmd'
    elif shell == 'zsh':
        args = ['-d', '-f', '-i', '-c']
        start = '_pair_preexec true'
        finish = '_pair_precmd'
    else:
        args = ['--no-config', '-i', '-c']
        start = 'emit fish_preexec true'
        finish = 'emit fish_postexec true'
    script = f'source ./hook; {gate}; {start}; {gate}; {finish}; {gate}'
    env = {
        'PATH': os.environ['PATH'], 'HOME': str(folder), 'TERM': 'xterm',
        'XDG_CONFIG_HOME': str(folder), 'XDG_RUNTIME_DIR': str(folder),
        'PAIR_CONFIG': str(folder / 'absent.json'), 'PAIR_AUTH': str(folder / 'absent-auth.json'),
        'PAIR_SOCKET': str(folder / 'daemon'), 'PAIR_REPORT_BARRIER': str(folder / 'barrier'),
        'NODE_OPTIONS': f'--import={json.dumps(preload)}',
    }
    # Only the child acquires this new PTY; never the user's controlling terminal.
    def controlling_terminal():
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)

    log = stack.enter_context(open(folder / 'shell.log', 'w+'))
    child = subprocess.Popen(
        [shell, *args, script], cwd=folder, env=env, stdin=slave, stdout=log, stderr=log,
        start_new_session=True, preexec_fn=controlling_terminal, pass_fds=(gate_read,),
    )
    reporters = []
    seen = []
    corruptions = []
    try:
        for expected in ['shell', 'command_start', 'command']:
            # The preload cannot run until Node has captured fd 0's cooked termios. It waits for our release,
            # so neither a slow machine nor the real report's import time changes the ordering.
            with barrier.accept()[0] as connection:
                connection.settimeout(TIMEOUT)
                pid = int(line(connection))
                reporters.append(pid)
                tty.setraw(slave)
                assert not termios.tcgetattr(slave)[3] & (termios.ICANON | termios.ECHO)
                connection.sendall(b'go\n')
            with daemon.accept()[0] as connection:
                connection.settimeout(TIMEOUT)
                message = json.loads(line(connection))
            assert message['type'] == expected, message
            assert message['shell'] == child.pid, message
            if expected != 'shell':
                assert message['cmd'] == 'true' and message['n'] == 1, message
            if expected == 'command':
                assert message['exit'] == 0, message
            wait_exit(pid)
            reporters.remove(pid)
            flags = termios.tcgetattr(slave)[3]
            if flags & (termios.ICANON | termios.ECHO):
                corruptions.append(
                    f'{shell} {expected} report restored cooked input: '
                    f'canonical={bool(flags & termios.ICANON)}, echo={bool(flags & termios.ECHO)}'
                )
            seen.append(expected)
            termios.tcsetattr(slave, termios.TCSANOW, cooked)
            os.write(gate_write, b'next\n')
        assert child.wait(timeout=TIMEOUT) == 0
        assert not corruptions, '\n'.join(corruptions)
        print(json.dumps(seen))
    finally:
        for pid in reporters:
            if running(pid):
                os.kill(pid, signal.SIGKILL)
                wait_exit(pid)
        if child.poll() is None:
            child.kill()
        child.wait(timeout=TIMEOUT)
        log.seek(0)
        output = log.read()
        if sys.exc_info()[0]:
            print(output, file=sys.stderr)
