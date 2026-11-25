"""Stage remote viewer files atomically on the Mac; PDFs can be refreshed."""

import hashlib
import os
from pathlib import Path
import shlex
import subprocess
import tempfile


# The SSH destination is the locally verified alias. Check the hostname inside
# each request as well: a nested SSH session can have a different main.pdf.
REMOTE = """\
import os, socket, stat, sys, shutil
host, path, previous = sys.argv[1:]
if socket.gethostname().lower() != host.lower():
    sys.exit(42)
def version(s):
    return '%d:%d:%d:%d:%d' % (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
s = os.stat(path)
if not stat.S_ISREG(s.st_mode):
    sys.exit(1)
stamp = version(s)
if previous == stamp:
    sys.exit(0)
with open(path, 'rb') as src:
    out = sys.stdout.buffer
    shutil.copyfileobj(src, out)
    if version(os.fstat(src.fileno())) != stamp or version(os.stat(path)) != stamp:
        sys.exit(1)
    out.write(stamp.encode().ljust(128))
"""


def refresh(alias, hostname, path, dest, previous, stopped, image=False):
    cmd = shlex.join(["python3", "-c", REMOTE, hostname, path, previous])
    with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as packet:
        try:
            result = subprocess.run(["/usr/bin/ssh", "-T", "-oBatchMode=yes", "-oConnectTimeout=8",
                                     "--", alias, cmd], stdin=subprocess.DEVNULL, stdout=packet,
                                    stderr=subprocess.DEVNULL, timeout=90)
            if result.returncode == 42:
                raise ValueError("The file hostname does not match this pane's SSH destination.")
            result.check_returncode()
            total = os.fstat(packet.fileno()).st_size
            if total == 0 and previous:
                return previous
            if total <= 128:
                raise OSError("Incomplete download")
            packet.seek(-128, os.SEEK_END)
            stamp = packet.read().decode("ascii").rstrip()
            parts = stamp.split(":")
            if len(parts) != 5 or not all(p.isdigit() for p in parts):
                raise OSError("Invalid download response")
            size = int(parts[2])
            packet.seek(0)
            if size != total - 128:
                raise OSError("Incomplete download")
            packet.truncate(size)
            if image:
                mime = subprocess.check_output(["/usr/bin/file", "-b", "--mime-type", packet.name], timeout=10)
                if not mime.startswith(b"image/"):
                    raise ValueError("Preview requires an image file.")
            else:
                if packet.read(5) != b"%PDF-":
                    raise OSError("Incomplete PDF")
                packet.seek(-min(1024, size), os.SEEK_END)
                if not packet.read().rstrip().endswith(b"%%EOF"):
                    raise OSError("PDF rebuild is incomplete")
            if not stopped.is_set():
                os.replace(packet.name, dest)
            return stamp
        finally:
            Path(packet.name).unlink(missing_ok=True)


def destination(alias, hostname, path, pane, kind="live-pdf"):
    directory = Path.home() / "Library/Caches/kzw" / kind / hashlib.sha256(
        repr((alias, hostname, path, pane)).encode()).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / Path(path).name


def open_image(alias, hostname, path, pane, stopped):
    dest = destination(alias, hostname, path, pane, "images")
    refresh(alias, hostname, path, dest, "", stopped, image=True)
    if not stopped.is_set():
        subprocess.run(["/usr/bin/open", "-b", "com.apple.Preview", str(dest)], check=True, timeout=15)


def watch(alias, hostname, path, pane, stopped):
    dest = destination(alias, hostname, path, pane)
    previous = ""
    while not stopped.is_set():
        try:
            current = refresh(alias, hostname, path, dest, previous, stopped)
        except (OSError, subprocess.SubprocessError):
            if not previous:
                raise ValueError("Could not fetch a complete PDF over SSH. Retry M p after the build finishes.")
            stopped.wait(5)
            continue
        if not previous and current and not stopped.is_set():
            subprocess.run(["/usr/bin/open", "-a", "Skim", str(dest)], check=True, timeout=15)
        previous = current
        stopped.wait(2)
