# Repository Guidelines

## Project Structure & Module Organization

`main.py` is the application entry point, and `run.sh` launches it through the project virtual environment. Application code lives in `fsapp/`: GTK/libadwaita UI components are in `application.py`, `window.py`, `pane.py`, and dialog/row modules; transfer, connection, configuration, and file models are separate modules. Filesystem implementations live under `fsapp/backend/`, with a shared base plus local and SFTP backends. Standalone test scripts are in `tests/`. Desktop integration and the application icon live in `data/`; RPM build files live in `packaging/`. The UI otherwise uses system GTK icons and widgets.

## Build, Test, and Development Commands

Create the environment once (system packages must provide GTK4, libadwaita, and PyGObject):

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
```

Run the application with `./run.sh`. This project has no compilation step. Run every headless suite (no display needed): `tests/selftest.py`,
`tests/transfer_safety.py`, `tests/sftp_contract.py`, `tests/connection_lifecycle.py`,
`tests/task_management.py`, `tests/hostkey_policy.py` and `tests/transfer_resilience.py`.
UI suites are individual executable scripts, for example `.venv/bin/python tests/ui_smoke.py`;
run all `tests/ui_*.py` scripts before merging UI, connection, or transfer changes.
Host-key tests point `HOME` at a temporary directory; never exercise them against the
real `~/.ssh/known_hosts`. UI tests briefly open windows and therefore require a graphical display.

## Coding Style & Naming Conventions

Use four-space indentation and follow PEP 8. Name modules, functions, and variables in `snake_case`, classes in `PascalCase`, and constants in `UPPER_SNAKE_CASE`. Keep backend-specific behavior behind the interfaces in `fsapp/backend/base.py`, and keep GTK updates on the GLib/GTK event path. No formatter or linter is configured, so preserve the existing import grouping, concise docstrings, and surrounding style.

## Testing Guidelines

Tests use custom `check()` helpers rather than pytest. Name new UI tests `tests/ui_<feature>.py` and give each a directly runnable `main()`. Isolate persistent settings with `FSTRANSFOR_CONFIG_HOME`, use temporary directories, and always clean them up. Cover success, cancellation, and error paths for filesystem operations; avoid depending on a live SSH server.

## Commit & Pull Request Guidelines

Use short, imperative subjects such as `Fix remote delete refresh`, and keep commits focused. Pull requests should explain user-visible behavior, list the exact test scripts run, link related issues, and include screenshots or a short recording for UI changes. Call out configuration migrations, destructive file operations, and SSH/security implications explicitly. The public repository is `Luyao-1024/fs_transfor`; preserve the MIT license in source and packaged distributions.
