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

"""Linux ``systemd --user`` adapter for the personal PowerContext Server."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

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

_METADATA = re.compile(rb"^# X-PowerContext-Metadata: ([A-Za-z0-9_=-]+)$", re.MULTILINE)


class SystemdUserAdapter:
    identifier = "powercontext.service"

    def __init__(self, *, config_home: Path | None = None) -> None:
        root = config_home or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self.artifact_path = root / "systemd" / "user" / self.identifier
        self.lock_path = self.artifact_path.with_name(f".{self.identifier}.lock")

    def support(self) -> tuple[SupportState, str]:
        if sys.platform != "linux":
            return SupportState.UNSUPPORTED, "systemd user services are available only on Linux"
        if shutil.which("systemctl") is None:
            return SupportState.UNSUPPORTED, "systemctl is not installed or is not on PATH"
        result = self._run("show-environment", check=False)
        if result.returncode != 0:
            return SupportState.UNSUPPORTED, "the current user has no available systemd user manager"
        return SupportState.SUPPORTED, "systemd --user is available"

    def inspect(self) -> NativeRegistration:
        state, content, detail = inspect_artifact(self.artifact_path)
        if state is not RegistrationState.INSTALLED or content is None:
            return NativeRegistration(state, content=content, detail=detail)
        match = _METADATA.search(content)
        if not content.startswith(b"# Managed by PowerContext\n") or match is None:
            return NativeRegistration(
                RegistrationState.INVALID,
                content=content,
                detail=f"{self.artifact_path} is not owned by PowerContext",
            )
        try:
            definition = decode_metadata(match.group(1).decode("ascii"))
        except (UnicodeError, ValueError) as error:
            return NativeRegistration(RegistrationState.INVALID, content=content, detail=str(error))
        if definition.ownership != OWNERSHIP_MARKER or self.render(definition) != content:
            return NativeRegistration(
                RegistrationState.INVALID,
                content=content,
                detail="the installed systemd unit does not match its PowerContext metadata",
            )
        return NativeRegistration(RegistrationState.INSTALLED, definition=definition, content=content)

    def render(self, definition: ServiceDefinition) -> bytes:
        metadata = encode_metadata(definition)
        command = " ".join(_systemd_quote(argument) for argument in definition.launcher_arguments())
        return (
            "# Managed by PowerContext\n"
            f"# X-PowerContext-Metadata: {metadata}\n"
            "[Unit]\n"
            "Description=PowerContext personal Server\n"
            "After=network.target\n"
            "StartLimitIntervalSec=60\n"
            "StartLimitBurst=3\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"ExecStart={command}\n"
            "Restart=on-failure\n"
            "RestartSec=5s\n"
            "TimeoutStopSec=30s\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        ).encode()

    def write(self, content: bytes) -> None:
        atomic_write(self.artifact_path, content)

    def restore(self, content: bytes | None) -> None:
        if content is None:
            self.artifact_path.unlink(missing_ok=True)
        else:
            atomic_write(self.artifact_path, content)

    def reload(self) -> None:
        self._run("daemon-reload")

    def enable(self) -> None:
        self._run("enable", self.identifier)

    def start(self, *, reload_definition: bool) -> None:
        command = "restart" if reload_definition and self.manager_state() is ManagerState.ACTIVE else "start"
        self._run(command, self.identifier)

    def stop(self) -> None:
        if self.manager_state() is not ManagerState.INACTIVE:
            self._run("stop", self.identifier)

    def disable(self) -> None:
        self._run("disable", self.identifier)

    def remove(self) -> None:
        self.artifact_path.unlink(missing_ok=True)

    def manager_state(self) -> ManagerState:
        result = self._run("show", "--property=ActiveState", "--value", self.identifier, check=False)
        if result.returncode != 0:
            return ManagerState.INACTIVE
        state = result.stdout.strip()
        if state == "active":
            return ManagerState.ACTIVE
        if state == "failed":
            return ManagerState.FAILED
        if state in {"inactive", "activating", "deactivating"}:
            return ManagerState.INACTIVE
        return ManagerState.UNKNOWN

    def log_location(self, definition: ServiceDefinition | None) -> str | None:
        return f"journalctl --user --unit {self.identifier}"

    def _run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        command = ["systemctl", "--user", *arguments]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)  # noqa: S603
        except (OSError, subprocess.SubprocessError) as error:
            raise ServiceError(f"failed to execute systemctl --user: {error}") from error  # noqa: TRY003
        if check and result.returncode != 0:
            detail = _command_detail(result.stderr)
            raise ServiceError(f"systemctl --user {arguments[0]} failed{detail}")  # noqa: TRY003
        return result


def _systemd_quote(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        raise ServiceError(  # noqa: TRY003
            "service command arguments must not contain control characters", exit_code=2
        )
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def _command_detail(stderr: str) -> str:
    detail = " ".join(stderr.strip().splitlines())
    return f": {detail[:500]}" if detail else ""


__all__: Sequence[str] = ["SystemdUserAdapter"]
