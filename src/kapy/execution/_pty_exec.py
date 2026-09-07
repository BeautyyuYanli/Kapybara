"""Tiny child-only entrypoint: acquire the PTY then replace ourselves with argv.

Invoked by absolute filename with an explicit child environment. Keeping ioctl
here avoids unsafe Python preexec_fn callbacks in a daemon with I/O threads.
"""

import fcntl
import os
import sys
import termios

if __name__ == "__main__":
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
