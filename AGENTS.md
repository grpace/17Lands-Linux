# 17Lands for Linux

KDE system tray app that wraps the upstream `seventeenlands` MTG Arena log client: one-command install, starts at login, and keeps drafts and matches uploading to 17Lands in the background. Users install it with `curl ... install.sh | bash` from `main`, and installed copies update themselves from `main`. Unofficial and not affiliated with 17lands.com. The repo is public (`grpace/17Lands-Linux`, default branch `main`, MIT).

## Stack
- One Python file, `seventeenlands_tray.py` (Python 3.10+, ruff `target-version = "py310"`), PyQt6. The installer prefers the distro's PyQt6 so windows follow the KDE theme; pip's wheel is the fallback.
- The upload client is the PyPI package `seventeenlands`, installed by `install.sh` with `pip --user` or `pipx`. The tray starts it and reads its log; its code is not in this repo.
- Tests use stdlib `unittest`. Lint is ruff (rules in `ruff.toml`) and shellcheck. CI (`.github/workflows/ci.yml`, Python 3.12) runs both on every push to `main` and every pull request.

## Commands
- `QT_QPA_PLATFORM=offscreen python3 -m unittest discover -s tests -v`: tests.
- `ruff check . && ruff format --check . && shellcheck install.sh uninstall.sh`: lint, as CI runs it.
- `./install.sh`: installs from the checkout into `~/.local` (see Ask First).
- `SEVENTEENLANDS_TRAY_INSTALLER=<path to install.sh>`: makes the Update button run a local installer instead of the one on GitHub, for testing updates.

## Where Things Live
- `seventeenlands_tray.py`: the whole app: log parsing and status, dashboard, settings and setup wizard, the single-instance Unix socket, the daily update check, and the CLI (`--show`, `--settings`, `--setup`, `--update`, `--quit`).
- `install.sh` / `uninstall.sh`: also served raw from `main` for `curl | bash`. The installer writes `~/.local/bin/seventeenlands-tray`, the systemd user service `seventeenlands-tray.service`, a desktop file and icons.
- `tests/test_tray.py`: tests for the non-GUI logic.
- User files: `~/.mtga_follower.ini` (the client token), `~/.config/seventeenlands-tray/`, `~/.seventeenlands/seventeenlands.log`, `~/.local/state/seventeenlands-tray/update.log`.

## Conventions
- Never put the token on a command line or in a log. The upstream client reads `~/.mtga_follower.ini` itself, and the installer keeps that file mode 600.
- Match tray and client processes exactly through `/proc`, limited to the current user, so `--quit` never touches other processes.
- User-facing strings use Title Case for buttons and headings.

## Gotchas
- **`main` is the release channel.** Once a day the tray reads `__version__` from `seventeenlands_tray.py` on `main` (raw.githubusercontent.com). When it is higher than the installed one, an Update button appears that runs `install.sh` from `main`. A push to `main` reaches every new install at once, and a version bump is offered to every installed copy. There are no tags or GitHub releases.
- The version lives only in `__version__`. Keep it plain dotted integers (`1.2.0`): it is parsed into an integer tuple.
- This machine runs an installed copy as `seventeenlands-tray.service`. `./install.sh` stops and replaces it; `uninstall.sh` removes it.

## Before Calling a Change Done
Run the tests and the full lint line above (CI also checks `ruff format`). For installer or UI changes, the real check is installing from the checkout with `./install.sh` and looking at the changed windows; ask first.

## Git
- Conventional commits (`feat:`, `fix:`); the body says why. A commit that bumps the version names it in the subject, e.g. `feat: ... (v1.2.0)`.
- Never commit `__pycache__/`, a token or `~/.mtga_follower.ini`.

## Ask First
- Pushing to `main`: it is what every `curl | bash` install and every in-app update gets, and a `__version__` bump offers the update to every installed copy.
- Running `install.sh`, `uninstall.sh` or `seventeenlands-tray --update` on this machine. They use `sudo` for distro packages and replace or remove the running tray and its service.
- Anything that reads, writes or prints the token or `~/.mtga_follower.ini`.
