import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from config import (
    SERVICE_GIT_PULL_TIMEOUT,
    SERVICE_RESTART_DELAY_SECONDS,
    SERVICE_RESTART_HOST,
    SERVICE_RESTART_PORT,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESTART_WORKER = Path(__file__).with_name("restart_worker.py")


class ServiceUpdateError(RuntimeError):
    pass


def update_repository(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    branch = run_git(project_root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    before_revision = run_git(project_root, "rev-parse", "HEAD").strip()
    pull_output = run_git(project_root, "pull", "--ff-only")
    after_revision = run_git(project_root, "rev-parse", "HEAD").strip()
    return {
        "branch": branch,
        "before_revision": before_revision,
        "after_revision": after_revision,
        "updated": before_revision != after_revision,
        "pull_output": pull_output.strip()[-8000:],
    }


def run_git(project_root: Path, *args: str) -> str:
    git_env = os.environ.copy()
    git_env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=project_root,
            env=git_env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SERVICE_GIT_PULL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ServiceUpdateError(f"Git 命令执行失败: {error}") from error

    output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
    if completed.returncode != 0:
        raise ServiceUpdateError(output or f"git {' '.join(args)} 执行失败")
    return output


def default_restart_command() -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "uvicorn",
        "main:app",
        "--host",
        SERVICE_RESTART_HOST,
        "--port",
        str(SERVICE_RESTART_PORT),
    ]


def schedule_service_restart(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    restart_command = default_restart_command()
    worker_command = [
        sys.executable,
        "-B",
        str(RESTART_WORKER),
        "--parent-pid",
        str(os.getpid()),
        "--cwd",
        str(project_root),
        "--command-json",
        json.dumps(restart_command, ensure_ascii=False),
    ]
    popen_kwargs: dict[str, Any] = {
        "cwd": project_root,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        )
    else:
        popen_kwargs["start_new_session"] = True

    try:
        subprocess.Popen(worker_command, **popen_kwargs)
    except OSError as error:
        raise ServiceUpdateError(f"无法启动服务重启进程: {error}") from error

    timer = threading.Timer(SERVICE_RESTART_DELAY_SECONDS, terminate_current_process)
    timer.daemon = True
    timer.start()
    return {
        "scheduled": True,
        "delay_seconds": SERVICE_RESTART_DELAY_SECONDS,
        "command": restart_command,
    }


def terminate_current_process() -> None:
    os._exit(0)
