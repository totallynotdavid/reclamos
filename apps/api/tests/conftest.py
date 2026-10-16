import shutil
import subprocess

import pytest


def docker_daemon_available() -> bool:
    docker = shutil.which("docker")
    if docker is None:
        return False
    try:
        result = subprocess.run(
            [docker, "info"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


@pytest.fixture
def docker_required() -> None:
    if not docker_daemon_available():
        pytest.skip("A running Docker daemon is required for this integration test")
