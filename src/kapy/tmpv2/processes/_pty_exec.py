"""Child-only terminal setup; a close-on-exec pipe reports setup failures to the parent."""

import fcntl
import os
import sys
import termios

if __name__ == "__main__":
    error_fd = int(sys.argv[1])
    os.set_inheritable(error_fd, False)
    try:
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    except OSError:
        os.write(error_fd, b"failed")
        os._exit(127)
