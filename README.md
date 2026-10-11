# Scenario Runner

[![Tests](https://github.com/andreystashev/scenario-runner/actions/workflows/tests.yml/badge.svg)](https://github.com/andreystashev/scenario-runner/actions/workflows/tests.yml)

A local web interface for repeatable commands and workflows on **macOS and Linux**. Run commands on your computer or in configured Docker containers, group them into reusable scenarios, and monitor them from named panels.

The application uses the **Python standard library** and a browser interface. Docker is optional and only needed for container environments.

## Features

- Run each command in a terminal window or in the background.
- Mix local and Docker environments within one scenario.
- Set panel paths and variables such as `{{project_path}}` and `{{bag_path}}`.
- Import and export scenarios as JSON.
- Detect existing container processes using a regular expression.
- Stop commands and terminal windows created by a panel.
- Search panels and commands, filter active panels or errors, and copy command output.
- Duplicate scenarios and commands, reorder commands, and protect unsaved drafts.
- Switch between Russian and English with a persistent RU / EN preference.
- Preview and customize light/dark themes, semantic colors, typography, text size, density, opacity, and animations.
- Give individual panels their own accent, background, text color, and command layout.

The interface uses three terms: a **scenario** is a saved set of commands; a **panel** runs a scenario with its own data path, variables, and command selection; a **run environment** is a local computer or Docker container.

Language is selected in the top bar and saved in the current browser. Russian is the default. UI labels and generated messages are translated; user names, commands, paths, variables, and program output remain literal.

## Quick start

Requirements: **Python 3.9+**, Bash, and macOS or Linux.

```bash
git clone https://github.com/andreystashev/scenario-runner.git
cd scenario-runner
./start
```

Open **<http://127.0.0.1:8010>**. The launcher starts the server in the background and opens your browser. Running `./start` again opens the existing server. On macOS, you can also double-click `start.command` in Finder.

The first launch starts with no scenarios. Create one in the scenario editor, add commands, then create a panel to run it. For a first local command, try:

```bash
pwd; echo "Hello from Scenario Runner"
```

Stop the web server with:

```bash
./stop.sh
```

Stopping the server leaves running scenario commands alive. Stop those commands from their panel first when needed. The launcher does not install login autostart.

## Configure a workflow

1. Add a run environment in settings: local or Docker.
2. Create a scenario and add commands in the scenario editor.
3. Choose the environment and terminal/background mode for each command.
4. Create a panel and set its path and custom variables.

Docker environments need a container name, host/container paths, a launch command, and optionally an environment setup command. If the container is stopped, the application opens a terminal with its configured launch command.

Commands run in **Bash**, including when your usual shell is zsh. Background mode has no interactive input or TTY; use terminal mode for commands that prompt for input.

The server listens on loopback and executes commands with your account's permissions. Review imported commands before running them and keep the port local.

## Configuration and logs

| Location                              | Contents                                       |
| ------------------------------------- | ---------------------------------------------- |
| `~/.config/scenario-runner/`          | Settings, scenarios, and workspace definitions |
| `~/.cache/scenario-runner/`           | Runtime files and logs                         |
| `~/.cache/scenario-runner/server.log` | Server log                                     |

```bash
SCENARIO_RUNNER_PORT=8020 ./start
SCENARIO_RUNNER_NO_OPEN=1 ./start
./start --foreground
```

Stop the existing server before changing the port. See the [usage and troubleshooting guide](docs/usage.md) for Python selection, isolated installations, terminal permissions, and migration from the old Linux service.

## Architecture

| Component                           | Responsibility                                                         |
| ----------------------------------- | ---------------------------------------------------------------------- |
| [launcher.py](launcher.py)          | Start/stop coordination, browser opening, and server identity checks   |
| [server.py](server.py)              | Local HTTP API, configuration, run environments, and command lifecycle |
| [static/](static/)                  | Browser interface                                                      |
| `start`, `start.command`, `stop.sh` | Shell and Finder entry points                                          |

Configuration changes are validated and persisted before they become visible in memory. JSON writes use a separate temporary file, `fsync`, and atomic replacement. Failed validation or saving leaves the previous configuration intact. Process actions already performed cannot be rolled back by a failed configuration save.

## Development and tests

No package installation is required:

```bash
python3 -m unittest -q
python3 -m py_compile server.py launcher.py
bash -n start start.sh stop.sh start.command
npm run check
npm test
```

Node.js 20+ is needed for JavaScript syntax checks and the built-in frontend test runner; the application itself does not require Node.js. GitHub Actions runs checks on macOS and Linux with Python 3.9, 3.12, and 3.13.

Tests use temporary directories, local HTTP ports, and harmless local commands. They cover startup, port conflicts, command stopping, process ownership, configuration validation, failed writes, API responses, search, import/copy workflows, variables, and localization. They do not automate Terminal.app or Docker integration and do not touch your saved settings.
