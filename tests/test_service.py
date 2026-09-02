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

from __future__ import annotations

import json
import os
import plistlib
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from typing import cast
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from powercontext.service import launcher as service_launcher
from powercontext.service.adapters.base import decode_metadata, encode_metadata
from powercontext.service.adapters.launchd import LaunchdUserAdapter
from powercontext.service.adapters.systemd import SystemdUserAdapter
from powercontext.service.cli import app as service_app
from powercontext.service.controller import ServiceController
from powercontext.service.model import (
    DEFINITION_VERSION,
    OWNERSHIP_MARKER,
    DefinitionState,
    LivenessState,
    ManagerState,
    NativeRegistration,
    ProbeResult,
    ProbeState,
    RegistrationState,
    ServiceDefinition,
    ServiceError,
    ServiceStatus,
    SupportState,
)
from powercontext.service.probe import probe_server


def _definition(tmp_path: Path, **overrides: object) -> ServiceDefinition:
    values = {
        "ownership": OWNERSHIP_MARKER,
        "definition_version": DEFINITION_VERSION,
        "package_version": version("powercontext"),
        "python_executable": str(Path(sys.executable).resolve()),
        "endpoint": "http://127.0.0.1:8000",
        "data_dir": str(tmp_path / "data"),
        "env_file": None,
        **overrides,
    }
    return ServiceDefinition(**values)


class FakeAdapter:
    identifier = "powercontext.test"

    def __init__(self, tmp_path: Path) -> None:
        self.artifact_path = tmp_path / "powercontext.test"
        self.lock_path = tmp_path / ".powercontext.test.lock"
        self.definition: ServiceDefinition | None = None
        self.content: bytes | None = None
        self.manager = ManagerState.INACTIVE
        self.events: list[str] = []
        self.fail_enable = False

    def support(self) -> tuple[SupportState, str]:
        return SupportState.SUPPORTED, "fake manager available"

    def inspect(self) -> NativeRegistration:
        if self.definition is None:
            return NativeRegistration(RegistrationState.NOT_INSTALLED)
        return NativeRegistration(RegistrationState.INSTALLED, self.definition, self.content)

    def render(self, definition: ServiceDefinition) -> bytes:
        return encode_metadata(definition).encode()

    def write(self, content: bytes) -> None:
        self.events.append("write")
        self.content = content
        self.definition = decode_metadata(content.decode())

    def restore(self, content: bytes | None) -> None:
        self.events.append("restore")
        self.content = content
        self.definition = decode_metadata(content.decode()) if content is not None else None

    def reload(self) -> None:
        self.events.append("reload")

    def enable(self) -> None:
        self.events.append("enable")
        if self.fail_enable:
            raise ServiceError("enable failed")  # noqa: TRY003

    def start(self, *, reload_definition: bool) -> None:
        self.events.append(f"start:{reload_definition}")
        self.manager = ManagerState.ACTIVE

    def stop(self) -> None:
        self.events.append("stop")
        self.manager = ManagerState.INACTIVE

    def disable(self) -> None:
        self.events.append("disable")

    def remove(self) -> None:
        self.events.append("remove")
        self.definition = None
        self.content = None

    def manager_state(self) -> ManagerState:
        return self.manager

    def log_location(self, definition: ServiceDefinition | None) -> str | None:
        return "fake logs"


def _manager_probe(adapter: FakeAdapter):
    def probe(endpoint: str) -> ProbeResult:
        if adapter.manager is ManagerState.ACTIVE:
            return ProbeResult(ProbeState.LIVE, f"{endpoint} status=ok")
        return ProbeResult(ProbeState.UNREACHABLE, f"cannot reach {endpoint}")

    return probe


@pytest.fixture(autouse=True)
def _clear_server_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name == "POWERCONTEXT_HOME" or name.startswith("POWERCONTEXT_SERVER_"):
            monkeypatch.delenv(name, raising=False)


def test_service_controller_installs_and_starts_one_native_registration(tmp_path: Path) -> None:
    adapter = FakeAdapter(tmp_path)
    controller = ServiceController(adapter, probe=_manager_probe(adapter), sleep=lambda _: None)

    status = controller.install()

    assert status.ok
    assert adapter.definition is not None
    assert adapter.definition.endpoint == "http://127.0.0.1:8000"
    assert adapter.events == ["write", "reload", "enable", "start:True"]


def test_service_install_is_idempotent_when_definition_is_current(tmp_path: Path) -> None:
    adapter = FakeAdapter(tmp_path)
    controller = ServiceController(adapter, probe=_manager_probe(adapter), sleep=lambda _: None)
    controller.install()
    adapter.events.clear()

    status = controller.install()

    assert status.ok
    assert adapter.events == ["enable"]


def test_service_install_rejects_a_non_powercontext_listener_before_writing(tmp_path: Path) -> None:
    adapter = FakeAdapter(tmp_path)
    controller = ServiceController(
        adapter,
        probe=lambda _: ProbeResult(ProbeState.CONFLICT, "invalid liveness response"),
    )

    with pytest.raises(ServiceError, match="another listener"):
        controller.install()

    assert adapter.events == []


def test_service_install_restores_the_previous_definition_when_enable_fails(tmp_path: Path) -> None:
    adapter = FakeAdapter(tmp_path)
    previous = _definition(tmp_path, package_version="old")
    adapter.write(adapter.render(previous))
    adapter.events.clear()
    adapter.fail_enable = True
    controller = ServiceController(adapter, probe=_manager_probe(adapter), sleep=lambda _: None)

    with pytest.raises(ServiceError, match="enable failed"):
        controller.install()

    assert adapter.definition == previous
    assert adapter.events == ["write", "reload", "enable", "disable", "restore", "reload", "enable"]


def test_service_uninstall_stops_before_removing_the_owned_definition(tmp_path: Path) -> None:
    adapter = FakeAdapter(tmp_path)
    controller = ServiceController(adapter, probe=_manager_probe(adapter), sleep=lambda _: None)
    controller.install()
    adapter.events.clear()

    status = controller.uninstall()

    assert status.registration is RegistrationState.NOT_INSTALLED
    assert adapter.events == ["stop", "disable", "remove", "reload"]


def test_service_install_requires_persistent_config_for_shell_server_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POWERCONTEXT_SERVER_HTTP_PORT", "8123")
    adapter = FakeAdapter(tmp_path)

    with pytest.raises(ServiceError, match="do not copy shell environment variables") as raised:
        ServiceController(adapter).install()

    assert raised.value.exit_code == 2
    assert adapter.events == []


def test_service_install_accepts_a_private_environment_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = tmp_path / "powercontext.env"
    environment.write_text("POWERCONTEXT_SERVER_HTTP_PORT=8123\n", encoding="utf-8")
    environment.chmod(0o600)
    adapter = FakeAdapter(tmp_path)
    controller = ServiceController(adapter, probe=_manager_probe(adapter), sleep=lambda _: None)

    status = controller.install(env_file=environment)

    assert status.ok
    assert adapter.definition is not None
    assert adapter.definition.endpoint == "http://127.0.0.1:8123"
    assert adapter.definition.env_file is not None
    assert adapter.definition.env_file.path == str(environment)


def test_service_install_rejects_a_group_readable_environment_file(tmp_path: Path) -> None:
    environment = tmp_path / "powercontext.env"
    environment.write_text("POWERCONTEXT_SERVER_HTTP_PORT=8123\n", encoding="utf-8")
    environment.chmod(0o640)

    with pytest.raises(ServiceError, match="chmod 600"):
        ServiceController(FakeAdapter(tmp_path)).install(env_file=environment)


def test_systemd_definition_round_trips_and_detects_tampering(tmp_path: Path) -> None:
    adapter = SystemdUserAdapter(config_home=tmp_path)
    executable = str(tmp_path / "Power Context" / "bin" / "python")
    definition = _definition(tmp_path, python_executable=executable)
    rendered = adapter.render(definition)

    adapter.write(rendered)
    installed = adapter.inspect()

    assert installed.state is RegistrationState.INSTALLED
    assert installed.definition == definition
    assert f'"{executable}"'.encode() in rendered

    adapter.artifact_path.write_bytes(rendered + b"# changed\n")
    assert adapter.inspect().state is RegistrationState.INVALID


def test_launchd_definition_round_trips_with_argument_array_and_logs(tmp_path: Path) -> None:
    adapter = LaunchdUserAdapter(home=tmp_path, uid=501)
    executable = str(tmp_path / "Power Context" / "bin" / "python")
    definition = _definition(tmp_path, python_executable=executable)
    rendered = adapter.render(definition)

    adapter.write(rendered)
    installed = adapter.inspect()
    payload = plistlib.loads(rendered)

    assert installed.state is RegistrationState.INSTALLED
    assert installed.definition == definition
    assert payload["ProgramArguments"][0] == executable
    assert payload["StandardOutPath"].endswith("logs/server.stdout.log")


class _LivenessHandler(BaseHTTPRequestHandler):
    include_request_id = True

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if self.include_request_id:
            self.send_header("X-PowerContext-Request-ID", "0123456789abcdef")
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}')

    def log_message(self, format: str, *_args: object) -> None:  # noqa: A002
        return None


def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = cast(tuple[str, int], server.server_address)
    return server, thread, f"http://{host}:{port}"


def test_service_probe_recognizes_the_powercontext_liveness_contract() -> None:
    server, thread, endpoint = _serve(_LivenessHandler)
    try:
        assert probe_server(endpoint).state is ProbeState.LIVE
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_service_probe_treats_an_invalid_listener_as_a_conflict() -> None:
    class InvalidHandler(_LivenessHandler):
        include_request_id = False

    server, thread, endpoint = _serve(InvalidHandler)
    try:
        assert probe_server(endpoint).state is ProbeState.CONFLICT
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_service_probe_reports_an_absent_listener_as_unreachable() -> None:
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    _, port = listener.getsockname()
    listener.close()

    assert probe_server(f"http://127.0.0.1:{port}").state is ProbeState.UNREACHABLE


def test_service_status_json_preserves_the_stable_state_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    status = ServiceStatus(
        support=SupportState.SUPPORTED,
        registration=RegistrationState.INSTALLED,
        definition=DefinitionState.CURRENT,
        manager=ManagerState.ACTIVE,
        server_liveness=LivenessState.LIVE,
        endpoint="http://127.0.0.1:8000",
        log_location="fake logs",
    )
    controller = Mock()
    controller.status.return_value = status
    monkeypatch.setattr("powercontext.service.cli._controller", lambda: controller)

    result = CliRunner().invoke(service_app, ["status", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == status.as_json()


def test_service_launcher_hands_control_to_the_foreground_server_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    run_server = Mock()
    monkeypatch.setattr(
        service_launcher,
        "probe_server",
        lambda _endpoint: ProbeResult(ProbeState.UNREACHABLE, "not listening"),
    )
    monkeypatch.setattr(service_launcher.server_cli, "_run_configured_server", run_server)

    exit_code = service_launcher.main(["--endpoint", "http://127.0.0.1:8000"])

    assert exit_code == 0
    run_server.assert_called_once()
    assert run_server.call_args.args[0].http.host == "127.0.0.1"
    assert run_server.call_args.args[0].http.port == 8000


def test_service_launcher_does_not_start_over_an_existing_powercontext_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_server = Mock()
    monkeypatch.setattr(
        service_launcher,
        "probe_server",
        lambda endpoint: ProbeResult(ProbeState.LIVE, f"{endpoint} status=ok"),
    )
    monkeypatch.setattr(service_launcher.server_cli, "_run_configured_server", run_server)

    exit_code = service_launcher.main(["--endpoint", "http://127.0.0.1:8000"])

    assert exit_code == 0
    run_server.assert_not_called()
