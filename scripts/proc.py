"""Shared subprocess-failure translation for docker/gh invocations.

``docker_helper.py`` and ``container_builder.py`` each shell out to Docker
and follow the same shape at every call site: run a command, and on a
non-zero exit translate the :class:`subprocess.CalledProcessError` into the
caller's own domain exception with a descriptive message. This module
centralizes that translation so it's implemented once instead of once per
call site. Dry-run branching and the "would run" logging stay in each
caller, since their wording legitimately differs call to call.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence


def run_or_raise(
    cmd: Sequence[str],
    *,
    error_cls: type[Exception],
    error_message: str,
    input: bytes | None = None,  # noqa: A002 — matches subprocess.run's kwarg name
) -> subprocess.CompletedProcess:
    """Run *cmd* via ``subprocess.run(cmd, check=True)`` and return the result.

    Parameters
    ----------
    error_cls:
        Exception type raised — chained from the original
        :class:`subprocess.CalledProcessError` — when *cmd* exits non-zero.
    error_message:
        Message template for *error_cls*, with a ``{returncode}`` placeholder
        filled in from the failed process's exit code.
    input:
        Forwarded to :func:`subprocess.run` (e.g. a token piped to
        ``docker login --password-stdin``).

    Raises
    ------
    error_cls
        If the subprocess exits non-zero.
    """
    try:
        return subprocess.run(cmd, check=True, input=input)
    except subprocess.CalledProcessError as exc:
        raise error_cls(error_message.format(returncode=exc.returncode)) from exc
