# Usage and troubleshooting

Start with the [project README](../README.md) for installation and a first workflow.

## Python and platform requirements

Use Python 3.9 or newer. On macOS with Homebrew, install it with `brew install python` if needed. To select a specific interpreter:

```bash
SCENARIO_RUNNER_PYTHON=/opt/homebrew/bin/python3 ./start
SCENARIO_RUNNER_PYTHON=/opt/homebrew/bin/python3 ./stop.sh
```

Intel Homebrew installations usually use `/usr/local/bin/python3`. The same option applies to startup and shutdown.

Terminal mode uses Terminal.app on macOS and `gnome-terminal` or `x-terminal-emulator` on Linux. Background commands do not need a terminal application. Docker targets require Docker CLI and a running Engine/Desktop; Linux containers need Bash, Python 3, `ps`, and `tee`.

## Terminal permissions on macOS

The first terminal launch may request permission to control Terminal.app. Allow it in **System Settings → Privacy & Security → Automation** for the application that started the server. A refusal appears as an error; background commands still work.

Terminal.app receives a short command that launches a separate script. It uses the terminal window's environment plus the target's optional setup command; it does not repeat Bash login initialization.

Stopping a terminal command closes its dedicated Terminal window. If you added other tabs, the application reports an error and leaves the window available for a later retry rather than closing the extra tabs. AppleScript errors are reported as failures.

## Targets, presets, and workspaces

Only the local target is configured initially. Add Docker targets in settings with a container name, host/container working paths, a container launch command, and an optional environment setup command.

Each preset can contain any number of commands. Target and execution mode are selected per command. A workspace has an optional main path and variables written as `{{name}}`. Built-in variables include `{{project_path}}`, `{{bag_path}}`, and `{{bag_folder}}`.

Background mode has no interactive stdin or TTY. Use a terminal for `sudo`, interactive SSH, or scripts that ask for input. When a Docker working directory is incorrect, the interface reports the path found inside the container and keeps the terminal open after an early launch failure.

The workspace's stop-owned action stops only commands and terminal windows it created. It does not stop the container or unrelated processes. Targets with active commands can be edited after those commands are stopped.

Empty commands and invalid regular expressions are rejected when saving. A missing local working directory produces an error before execution; the application does not substitute your home directory.

## Server lifecycle

Repeated `./start` calls open the same server. Concurrent starts use a file lock. A different service on the requested port is reported as a conflict and is not stopped or treated as Scenario Runner.

```bash
./start --foreground                  # Visible server output
SCENARIO_RUNNER_NO_OPEN=1 ./start      # No browser opening
./stop.sh                            # Stop the web server
SCENARIO_RUNNER_PORT=8020 ./start      # Select a port after stopping
```

The background server runs until stopped or until you log out. It does not install login autostart. Before upgrading an older Linux installation that used systemd, stop its previous service:

```bash
systemctl --user disable --now scenario-runner.service
```

Saved settings and presets remain available.

## Isolate installations

`SCENARIO_RUNNER_CONFIG_DIR` and `SCENARIO_RUNNER_CACHE_DIR` override the settings and runtime directories. Use the same values for startup and shutdown.

Runtime files include `server.json` and `launcher.lock` in the cache directory. Shutdown checks the server path and its unique process identifier; a stale PID is not used to stop an unrelated process.

## Persistence and recovery

Settings, preset, and workspace changes are validated and written before they replace the in-memory configuration. Temporary JSON files are removed after a failed write. Process starts/stops already performed are outside this rollback.

On startup, a workspace referencing a missing preset is skipped with a log entry. Other workspaces remain available, and the original `workspaces.json` is not overwritten while loading.

Check `~/.cache/scenario-runner/server.log` when startup, configuration, or terminal operations fail. Status polling keeps existing panels when their data has not changed.

## Manual integration checks

Create a local preset containing `pwd; echo "Hello from Scenario Runner"`. Run it first in the background and then in a terminal. Check output, exit status, and stopping.

For Docker, configure an existing Linux container and run `pwd; echo "Hello from Docker"` in both modes. These checks exercise integrations outside the automated suite.
