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
import logging
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from powercontext.cli.env_file import EnvironmentFileError
from powercontext.server import cli as server_cli
from powercontext.server.configuration import server_settings_context
from powercontext.service.model import ProbeState
from powercontext.service.probe import probe_server
from powercontext.transport import is_loopback_host

logger = logging.getLogger(__name__)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="powercontext-personal-service-launcher")
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--env-file", type=Path)
    options = parser.parse_args(arguments)

    try:
        with server_settings_context(env_file=options.env_file) as settings:
            expected = _endpoint(settings.http.host, settings.http.port)
            if expected != options.endpoint or not is_loopback_host(settings.http.host):
                logger.error(
                    "Registered personal service endpoint does not match the current loopback Server configuration"
                )
                return 2
            probe = probe_server(options.endpoint)
            if probe.state is ProbeState.LIVE:
                logger.info("PowerContext Server is already live; the personal service launcher will exit")
                return 0
            if probe.state is ProbeState.CONFLICT:
                logger.error("Personal service endpoint conflict: %s", probe.detail)
                return 1
            server_cli._run_configured_server(settings)
            return 0
    except (EnvironmentFileError, OSError, ValidationError) as error:
        logger.error("Personal service configuration is invalid: %s", error)  # noqa: TRY400
        return 2
    except Exception:
        logger.exception("PowerContext personal service failed")
        return 1


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
