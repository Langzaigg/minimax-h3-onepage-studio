import argparse
import ctypes
import json
import os
import subprocess
import time
from pathlib import Path


def wait_for_process_exit(process_id: int, timeout_seconds: float = 30.0) -> None:
    if os.name == "nt":
        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, process_id)
        if handle:
            try:
                ctypes.windll.kernel32.WaitForSingleObject(handle, int(timeout_seconds * 1000))
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        return

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(process_id, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            return
        time.sleep(0.2)


def start_service(command: list[str], cwd: Path) -> None:
    kwargs = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--command-json", required=True)
    args = parser.parse_args()

    command = json.loads(args.command_json)
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        raise ValueError("重启命令必须是非空字符串数组")

    wait_for_process_exit(args.parent_pid)
    start_service(command, args.cwd.resolve())


if __name__ == "__main__":
    main()
