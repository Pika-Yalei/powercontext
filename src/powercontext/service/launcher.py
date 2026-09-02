# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Manager-owned preflight launcher for the persistent personal Server."""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from powercontext.cli.env_file import EnvironmentFileError
from powercontext.server import cli as server_cli
from powercontext.server.configuration import server_settings_context
from powercontext.service.adapters.base import atomic_write
from powercontext.service.model import ProbeState
from powercontext.service.probe import probe_server
from powercontext.transport import is_loopback_host

logger = logging.getLogger(__name__)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="powercontext-personal-service-launcher")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--failure-state", type=Path)
    parser.add_argument("--failure-limit", type=int, default=0)
    parser.add_argument("--failure-window-seconds", type=float, default=0)
    options = parser.parse_args(arguments)
    budget = _FailureBudget(
        path=options.failure_state,
        limit=options.failure_limit,
        window_seconds=options.failure_window_seconds,
    )
    if not budget.begin_attempt():
        logger.error("Personal service retry budget is exhausted; run `powercontext service install` to retry")
        return 0

    try:
        with server_settings_context(env_file=options.env_file, data_dir=options.data_dir) as settings:
            expected = _endpoint(settings.http.host, settings.http.port)
            if expected != options.endpoint or not is_loopback_host(settings.http.host):
                logger.error(
                    "Registered personal service endpoint does not match the current loopback Server configuration"
                )
                return budget.failure_exit_code()
            probe = probe_server(options.endpoint)
            if probe.state is ProbeState.LIVE:
                logger.info("PowerContext Server is already live; the personal service launcher will exit")
                budget.clear()
                return 0
            if probe.state is ProbeState.CONFLICT:
                logger.error("Personal service endpoint conflict: %s", probe.detail)
                return budget.failure_exit_code()
            server_cli._run_configured_server(settings)
            budget.clear()
            return 0
    except (EnvironmentFileError, OSError, ValidationError) as error:
        logger.error("Personal service configuration is invalid: %s", error)  # noqa: TRY400
        return budget.failure_exit_code()
    except SystemExit as error:
        if error.code is None or error.code == 0:
            budget.clear()
            return 0
        logger.error("PowerContext personal service exited during startup: %s", error)  # noqa: TRY400
        return budget.failure_exit_code()
    except Exception:
        logger.exception("PowerContext personal service failed")
        return budget.failure_exit_code()


class _FailureBudget:
    """Bound rapid launchd retries without supervising the Server process."""

    def __init__(self, *, path: Path | None, limit: int, window_seconds: float) -> None:
        self._path = path
        self._limit = limit
        self._window_seconds = window_seconds
        self._attempts = 0

    def begin_attempt(self) -> bool:
        if self._path is None:
            return True
        if self._limit < 1 or self._window_seconds <= 0:
            logger.error("Personal service retry-budget configuration is invalid")
            return False
        now = time.time()
        try:
            attempts = [attempt for attempt in _read_attempts(self._path) if now - attempt <= self._window_seconds]
            if len(attempts) >= self._limit:
                self._attempts = len(attempts)
                return False
            attempts.append(now)
            atomic_write(self._path, json.dumps(attempts, separators=(",", ":")).encode(), mode=0o600)
        except (OSError, ValueError) as error:
            logger.error("Cannot update the personal service retry budget: %s", error)  # noqa: TRY400
            return False
        self._attempts = len(attempts)
        return True

    def failure_exit_code(self) -> int:
        if self._path is not None and self._attempts >= self._limit:
            logger.error(
                "Personal service retry budget exhausted after %d attempts; "
                "run `powercontext service install` to retry",
                self._attempts,
            )
            return 0
        return 1

    def clear(self) -> None:
        if self._path is None:
            return
        try:
            self._path.unlink(missing_ok=True)
        except OSError as error:
            logger.warning("Cannot clear the personal service retry budget: %s", error)


def _read_attempts(path: Path) -> list[float]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid retry-budget state") from error  # noqa: TRY003
    if not isinstance(payload, list) or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) for value in payload
    ):
        raise ValueError("invalid retry-budget state")  # noqa: TRY003
    return [float(value) for value in payload]


def _endpoint(host: str, port: int) -> str:
    normalized = host.strip("[]")
    rendered_host = f"[{normalized}]" if ":" in normalized else normalized
    endpoint = f"http://{rendered_host}:{port}"
    parsed = urlsplit(endpoint)
    if parsed.hostname is None:
        raise ValueError("invalid Server endpoint")  # noqa: TRY003
    return endpoint


if __name__ == "__main__":
    raise SystemExit(main())
