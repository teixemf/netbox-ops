"""Run a terminal capture fixture in an actual pseudoterminal."""
from __future__ import annotations

import errno
import os
import pty
import select
import subprocess
import sys
import time


def run_capture(module, output, language, monochrome):
    master, slave = pty.openpty()
    env = dict(os.environ, TERM='xterm-256color', LINES='45', COLUMNS='160')
    env.pop('NO_COLOR', None)
    if monochrome:
        env['NO_COLOR'] = '1'
    proc = None
    terminal_output = bytearray()
    try:
        proc = subprocess.Popen([sys.executable, '-m', module, str(output), language],
                                stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 20
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                raise AssertionError('curses capture timed out')
            if select.select([master], [], [], 0.1)[0]:
                try:
                    terminal_output.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
        if proc.returncode != 0:
            raise AssertionError(terminal_output.decode(errors='replace'))
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait()
        if slave is not None:
            os.close(slave)
        os.close(master)
