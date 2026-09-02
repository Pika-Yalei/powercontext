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

"""macOS per-user LaunchAgent adapter for the personal PowerContext Server."""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from powercontext.service.adapters.base import atomic_write, decode_metadata, encode_metadata, inspect_artifact
from powercontext.service.model import (
    OWNERSHIP_MARKER,
    ManagerState,
    NativeRegistration,
    RegistrationState,
    ServiceDefinition,
    ServiceError,
    SupportState,
)

_FAILURE_LIMIT = 3
_FAILURE_WINDOW_SECONDS = 60


class LaunchdUserAdapter:
    identifier = "com.oceanbase.powercontext"

    def __init__(self, *, home: Path | None = None, uid: int | None = None) -> None:
        user_home = home or Path.home()
        self.artifact_path = user_home / "Library" / "LaunchAgents" / f"{self.identifier}.plist"
        self.lock_path = self.artifact_path.with_name(f".{self.identifier}.lock")
        self._uid = os.getuid() if uid is None else uid

    @property
    def _domain(self) -> str:
        return f"gui/{self._uid}"

    @property
    def _target(self) -> str:
        return f"{self._domain}/{self.identifier}"

    def support(self) -> tuple[SupportState, str]:
        if sys.platform != "darwin":
            return SupportState.UNSUPPORTED, "LaunchAgents are available only on macOS"
        if shutil.which("launchctl") is None:
            return SupportState.UNSUPPORTED, "launchctl is not installed or is not on PATH"
        result = self._run("print", self._domain, check=False)
        if result.returncode != 0:
            return SupportState.UNSUPPORTED, "the current launchd user domain is unavailable"
        return SupportState.SUPPORTED, "the launchd user domain is available"

    def inspect(self) -> NativeRegistration:
        state, content, detail = inspect_artifact(self.artifact_path)
        if state is not RegistrationState.INSTALLED or content is None:
            return NativeRegistration(state, content=content, detail=detail)
        try:
            payload = plistlib.loads(content)
            definition = _definition_from_payload(payload)
            expected = plistlib.loads(self.render(definition))
        except (TypeError, ValueError, plistlib.InvalidFileException) as error:
            return NativeRegistration(RegistrationState.INVALID, content=content, detail=str(error))
        if definition.ownership != OWNERSHIP_MARKER or payload != expected:
            return NativeRegistration(
                RegistrationState.INVALID,
                content=content,
                detail="the installed LaunchAgent does not match its PowerContext metadata",
            )
        return NativeRegistration(RegistrationState.INSTALLED, definition=definition, content=content)

    def render(self, definition: ServiceDefinition) -> bytes:
        log_dir = Path(definition.data_dir) / "logs"
        payload: dict[str, Any] = {
            "Label": self.identifier,
            "ProgramArguments": _launcher_arguments(definition),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 5,
            "ProcessType": "Background",
            "StandardOutPath": str(log_dir / "server.stdout.log"),
            "StandardErrorPath": str(log_dir / "server.stderr.log"),
            "EnvironmentVariables": {
                "POWERCONTEXT_SERVICE_OWNED": "true",
                "POWERCONTEXT_SERVICE_METADATA": encode_metadata(definition),
            },
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    def write(self, content: bytes) -> None:
        definition = _definition_from_payload(plistlib.loads(content))
        (Path(definition.data_dir) / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_write(self.artifact_path, content)

    def restore(self, content: bytes | None) -> None:
        if content is None:
            self.artifact_path.unlink(missing_ok=True)
        else:
            atomic_write(self.artifact_path, content)

    def reload(self) -> None:
        return None

    def enable(self) -> None:
        registration = self.inspect()
        if registration.definition is not None:
            (Path(registration.definition.data_dir) / "logs").mkdir(mode=0o700, parents=True, exist_ok=True)
            _failure_state_path(registration.definition).unlink(missing_ok=True)
        self._run("enable", self._target)

    def start(self, *, reload_definition: bool) -> None:
        loaded = self._is_loaded()
        if loaded and reload_definition:
            self._run("bootout", self._target)
            loaded = False
        if not loaded:
            self._run("bootstrap", self._domain, str(self.artifact_path))
        elif self.manager_state() is not ManagerState.ACTIVE:
            self._run("kickstart", "-k", self._target)

    def stop(self) -> None:
        if self._is_loaded():
            self._run("bootout", self._target)

    def disable(self) -> None:
        self._run("disable", self._target)

    def remove(self) -> None:
        self.artifact_path.unlink(missing_ok=True)

    def manager_state(self) -> ManagerState:
        result = self._run("print", self._target, check=False)
        if result.returncode != 0:
            return ManagerState.INACTIVE
        if "state = running" in result.stdout:
            return ManagerState.ACTIVE
        if "state = exited" in result.stdout:
            return ManagerState.FAILED
        return ManagerState.INACTIVE

    def log_location(self, definition: ServiceDefinition | None) -> str | None:
        if definition is None:
            return None
        return str(Path(definition.data_dir) / "logs")

    def _is_loaded(self) -> bool:
        return self._run("print", self._target, check=False).returncode == 0

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["launchctl", *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
        except (OSError, subprocess.SubprocessError) as error:
            raise ServiceError(f"failed to execute launchctl: {error}") from error  # noqa: TRY003
        if check and result.returncode != 0:
            detail = _command_detail(result.stderr)
            raise ServiceError(f"launchctl {arguments[0]} failed{detail}")  # noqa: TRY003
        return result


def _definition_from_payload(payload: dict[str, Any]) -> ServiceDefinition:
    environment = payload.get("EnvironmentVariables")
    if not isinstance(environment, dict) or environment.get("POWERCONTEXT_SERVICE_OWNED") != "true":
        raise ValueError("LaunchAgent is missing the PowerContext ownership marker")  # noqa: TRY003
    metadata = environment.get("POWERCONTEXT_SERVICE_METADATA")
    if not isinstance(metadata, str):
        raise TypeError("LaunchAgent is missing PowerContext service metadata")  # noqa: TRY003
    return decode_metadata(metadata)


def _failure_state_path(definition: ServiceDefinition) -> Path:
    return Path(definition.data_dir) / "logs" / "launchd-retry-state.json"


def _launcher_arguments(definition: ServiceDefinition) -> list[str]:
    return [
        *definition.launcher_arguments(),
        "--failure-state",
        str(_failure_state_path(definition)),
        "--failure-limit",
        str(_FAILURE_LIMIT),
        "--failure-window-seconds",
        str(_FAILURE_WINDOW_SECONDS),
    ]


def _command_detail(stderr: str) -> str:
    detail = " ".join(stderr.strip().splitlines())
    return f": {detail[:500]}" if detail else ""


__all__ = ["LaunchdUserAdapter"]
