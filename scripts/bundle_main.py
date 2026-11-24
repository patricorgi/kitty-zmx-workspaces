"""Entry point embedded in the installed kzw zipapp."""

import errno
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile


def main():
    program = Path(sys.argv[0]).resolve()
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    runtime = data / "kzw/runtime" / "__BUILD_ID__"
    # Kitty requires actual files for kittens/watchers, as do the inline SSH
    # bridge and Python subprocesses. Publish the complete build atomically.
    if not runtime.is_dir():
        runtime.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = Path(tempfile.mkdtemp(dir=runtime.parent))
        try:
            with zipfile.ZipFile(program) as archive:
                for member in archive.namelist():
                    if member.startswith("kzw/"):
                        archive.extract(member, staging)
            try:
                staging.rename(runtime)
            except OSError as exc:
                if exc.errno not in (errno.EEXIST, errno.ENOTEMPTY) or not runtime.is_dir():
                    raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    sys.path.insert(0, str(runtime))
    os.environ["PYTHONPATH"] = os.pathsep.join(filter(None, [str(runtime), os.environ.get("PYTHONPATH")]))
    os.environ["KZW_EXECUTABLE"] = str(program)
    if sys.argv[1:] == ["_runtime-directory"]:
        print(runtime)
        return 0
    from kzw.cli import entrypoint
    return entrypoint()


if __name__ == "__main__":
    raise SystemExit(main())
