"""Run a server, show its output, write the same output to a file, and when it
dies leave the window open with the reason on screen.

Windows gives a new console OR a redirected file, never both. A server started
with its own window writes nothing to the log; a server whose output is
redirected shows an empty window. Either way the one place anyone looks for
the reason it stopped is empty, which is how a crash on the second laptop came
to be reported as "did not answer in 90s - the reason is in <an empty file>".

Nothing here needs more than the standard library, so it runs under whichever
Python started it, including one with no project packages at all.
"""
import subprocess
import sys

if len(sys.argv) < 3:
    sys.exit("usage: run_logged.py <logfile> <command> [args...]")

log, cmd = sys.argv[1], sys.argv[2:]

with open(log, "wb", buffering=0) as fh:
    head = ("$ " + " ".join(cmd) + "\n\n").encode("utf-8", "replace")
    fh.write(head)
    sys.stdout.buffer.write(head)
    sys.stdout.flush()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
    except OSError as e:
        msg = f"could not start it: {e}\n".encode("utf-8", "replace")
        fh.write(msg)
        sys.stdout.buffer.write(msg)
        input("\nPress Enter to close. ")
        sys.exit(1)

    for line in p.stdout:
        sys.stdout.buffer.write(line)
        sys.stdout.flush()
        fh.write(line)
    code = p.wait()
    tail = f"\n[exited with code {code}]\n".encode()
    fh.write(tail)
    sys.stdout.buffer.write(tail)
    sys.stdout.flush()

# A server that stops on its own has something to say. Keeping the window up
# is the difference between a readable error and a window that blinks shut.
if code != 0:
    try:
        input("\nThis stopped on its own - the error is above. Press Enter to close. ")
    except EOFError:
        pass
sys.exit(code)
