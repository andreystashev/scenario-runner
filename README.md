# Scenario Runner

[![Tests](https://github.com/andreystashev/scenario-runner/actions/workflows/tests.yml/badge.svg)](https://github.com/andreystashev/scenario-runner/actions/workflows/tests.yml)

A local web interface for repeatable commands and workflows on **macOS and Linux**. Run commands on your computer or in configured Docker containers, group them into reusable presets, and monitor them from named workspace panels.

The application uses the **Python standard library** and a browser interface. Docker is optional and only needed for container targets.

## Features

- Run each command in a terminal window or in the background.
- Mix local and Docker targets within one preset.
- Set workspace paths and variables such as `{{project_path}}` and `{{bag_path}}`.
- Import and export presets as JSON.
- Detect existing container processes using a regular expression.
- Stop commands and terminal windows created by a workspace.
- Customize the theme, accent color, typography, scale, and density.

## Quick start

Requirements: **Python 3.9+**, Bash, and macOS or Linux.

```bash
git clone https://github.com/andreystashev/scenario-runner.git
cd scenario-runner
./start
```

Open **<http://127.0.0.1:8010>**. The launcher starts the server in the background and opens your browser. Running `./start` again opens the existing server. On macOS, you can also double-click `start.command` in Finder.

The first launch starts with no presets. Create one in the scenario editor, add commands, then create a workspace panel to run it. For a first local command, try:

```bash
pwd; echo "Hello from Scenario Runner"
```

Stop the web server with:

```bash
./stop.sh
```

Stopping the server leaves running scenario commands alive. Stop those commands from their workspace panel first when needed. The launcher does not install login autostart.

## Configure a workflow

1. Add a target in settings: local or Docker.
2. Create a preset and add commands in the scenario editor.
3. Choose the target and terminal/background mode for each command.
4. Create a workspace panel and set its path and custom variables.

Docker targets need a container name, host/container paths, a launch command, and optionally an environment setup command. If the container is stopped, the application opens a terminal with its configured launch command.

Commands run in **Bash**, including when your usual shell is zsh. Background mode has no interactive input or TTY; use terminal mode for commands that prompt for input.

The server listens on loopback and executes commands with your account's permissions. Review imported commands before running them and keep the port local.

## Configuration and logs

| Location | Contents |
| --- | --- |
| `~/.config/scenario-runner/` | Settings, presets, and workspace definitions |
| `~/.cache/scenario-runner/` | Runtime files and logs |
| `~/.cache/scenario-runner/server.log` | Server log |

```bash
SCENARIO_RUNNER_PORT=8020 ./start
SCENARIO_RUNNER_NO_OPEN=1 ./start
./start --foreground
```

Stop the existing server before changing the port. See the [usage and troubleshooting guide](docs/usage.md) for Python selection, isolated installations, terminal permissions, and migration from the old Linux service.

## Architecture

| Component | Responsibility |
| --- | --- |
| [launcher.py](launcher.py) | Start/stop coordination, browser opening, and server identity checks |
| [server.py](server.py) | Local HTTP API, configuration, targets, and command lifecycle |
| [static/](static/) | Browser interface |
| `start`, `start.command`, `stop.sh` | Shell and Finder entry points |

Configuration changes are validated and persisted before they become visible in memory. JSON writes use a separate temporary file, `fsync`, and atomic replacement. Failed validation or saving leaves the previous configuration intact. Process actions already performed cannot be rolled back by a failed configuration save.

## Development and tests

No package installation is required:

```bash
python3 -m unittest -q
python3 -m py_compile server.py launcher.py
bash -n start start.sh stop.sh start.command
node --check static/app.js
```

Node.js is only needed for the optional JavaScript syntax check. GitHub Actions runs checks on macOS and Linux with Python 3.9, 3.12, and 3.13.

Tests use temporary directories, local HTTP ports, and harmless local commands. They cover startup, port conflicts, command stopping, process ownership, configuration validation, failed writes, and API responses. They do not automate Terminal.app or Docker integration and do not touch your saved settings.
