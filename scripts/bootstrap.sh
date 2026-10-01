#!/usr/bin/env bash
# Laminary laptop bootstrap (macOS, Apple Silicon). Idempotent: safe to re-run.
# Never writes secrets, never overwrites .env. See docs/ENVIRONMENTS.md "Local laptop setup".
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BREWFILE="$REPO/scripts/Brewfile"
CHECK_ONLY=0; WANT_ANDROID=0; WANT_EAS=0; DOCKER_RUNTIME=desktop

usage() {
  cat <<'USAGE'
Usage: scripts/bootstrap.sh [--check] [--android] [--eas] [--orbstack] [--help]

  (no flags)   Install/configure everything needed for Phases 0-1, then verify.
  --check      Verify only. Installs and writes nothing.
  --android    Also install JDK 17 (Temurin), Android platform-tools (adb) and Android Studio.
  --eas        Also install eas-cli globally with npm (needed from Phase 3).
  --orbstack   Use OrbStack instead of Docker Desktop (see licence note in docs/ENVIRONMENTS.md).
  --help       Show this help.

Manual steps it will NOT automate: Xcode (App Store), `sudo xcodebuild -license accept`,
first launch of the Docker app, all accounts and API keys.
USAGE
}

for a in "$@"; do
  case "$a" in
    --check) CHECK_ONLY=1 ;;
    --android) WANT_ANDROID=1 ;;
    --eas) WANT_EAS=1 ;;
    --orbstack) DOCKER_RUNTIME=orbstack ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $a" >&2; usage >&2; exit 2 ;;
  esac
done

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
# Run a mutating command, or just say what would run in --check mode.
act() { if [ "$CHECK_ONLY" = 1 ]; then note "[check] would run: $*"; else "$@"; fi; }

if [ "$(uname -s)" != Darwin ]; then echo "macOS only." >&2; exit 1; fi
[ "$(uname -m)" = arm64 ] || echo "Warning: not Apple Silicon; paths assume /opt/homebrew." >&2
[ "$CHECK_ONLY" = 1 ] && say "CHECK MODE: nothing will be installed or written"

BREW_PREFIX=/opt/homebrew
load_brew_env() { if [ -x "$BREW_PREFIX/bin/brew" ]; then eval "$("$BREW_PREFIX/bin/brew" shellenv)"; fi; }
export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1
load_brew_env
# Keg-only formulae, put on PATH for this run only.
add_keg_paths() {
  local k
  for k in node@22 libpq; do
    if [ -d "$BREW_PREFIX/opt/$k/bin" ]; then PATH="$BREW_PREFIX/opt/$k/bin:$PATH"; fi
  done
  export PATH
}
add_keg_paths

# ---------------------------------------------------------------- 1. Xcode
say "1. Xcode and command line tools (manual steps)"
if xcode-select -p >/dev/null 2>&1; then note "Command line tools: present ($(xcode-select -p))"
else
  note "Command line tools missing. Run: xcode-select --install   (GUI prompt; Homebrew's installer also triggers it)"
fi
if [ -d /Applications/Xcode.app ]; then
  if xcodebuild -license check >/dev/null 2>&1; then note "Xcode: installed, licence accepted"
  else note "Xcode installed but licence not accepted. Run: sudo xcodebuild -license accept"; fi
else
  note "Xcode.app not found. Install from the Mac App Store (large download). Needed only for iOS"
  note "simulator/builds (Phase 3); Phases 0-1 do not need it. Continuing."
fi

# ---------------------------------------------------------------- 2. Homebrew
say "2. Homebrew"
if have brew; then note "present: $(brew --version | head -1)"
elif [ "$CHECK_ONLY" = 1 ]; then note "[check] Homebrew missing; would install from https://brew.sh"
else
  note "Installing Homebrew (official installer; may ask for your password)"
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  load_brew_env
fi
# shellcheck disable=SC2016  # literal text to print, must not expand
ZPROFILE_LINE='eval "$(/opt/homebrew/bin/brew shellenv)"'
if have brew && ! grep -qsF "$ZPROFILE_LINE" "$HOME/.zprofile" 2>/dev/null; then
  note "Add to ~/.zprofile (not done automatically):  $ZPROFILE_LINE"
fi

# ---------------------------------------------------------------- 3. Brewfile
say "3. Brewfile packages"
if have brew; then
  # Homebrew 7+ refuses formulae from untrusted taps. Trust ONLY taps named in the Brewfile.
  if brew trust --help >/dev/null 2>&1; then
    TRUSTED="$(brew trust --json v1 2>/dev/null || true)"   # read-only listing
    # shellcheck disable=SC2013  # tap names contain no whitespace
    for T in $(sed -n 's/^tap[[:space:]]*"\([^"]*\)".*/\1/p' "$BREWFILE"); do
      if printf '%s' "$TRUSTED" | grep -qF "\"$T\""; then note "tap $T already trusted"
      elif [ "$CHECK_ONLY" = 1 ]; then note "[check] would run: brew trust --tap $T"
      else brew trust --tap "$T" || { brew tap "$T" && brew trust --tap "$T"; }; fi
    done
  else
    note "This Homebrew has no 'brew trust'; skipping tap trust"
  fi
  if [ "$CHECK_ONLY" = 1 ]; then
    brew bundle check --file="$BREWFILE" --verbose || note "[check] some Brewfile packages missing; a real run installs them"
  else
    HOMEBREW_NO_ENV_HINTS=1 brew bundle --file="$BREWFILE"
  fi
  # node@22 is keg-only; link so `node` works in every shell (idempotent).
  if [ "$CHECK_ONLY" = 0 ] && [ -d "$BREW_PREFIX/opt/node@22" ] && ! [ -e "$BREW_PREFIX/bin/node" ]; then
    brew link --overwrite node@22
  fi
else
  note "brew unavailable; skipping"
fi
add_keg_paths   # kegs now exist on a fresh machine; needed for the verification table
note "For psql, add to ~/.zshrc:  export PATH=\"/opt/homebrew/opt/libpq/bin:\$PATH\""
note "For direnv (optional), add to ~/.zshrc:  eval \"\$(direnv hook zsh)\""

# ---------------------------------------------------------------- 4. Docker
say "4. Docker runtime ($DOCKER_RUNTIME)"
if [ "$DOCKER_RUNTIME" = orbstack ]; then CASK=orbstack; APP=/Applications/OrbStack.app
else CASK=docker-desktop; APP=/Applications/Docker.app; fi
if [ -d "$APP" ] || have docker; then note "present"
elif have brew; then act brew install --cask "$CASK"
else note "needs Homebrew first"; fi
if have docker && ! docker info >/dev/null 2>&1; then
  note "Docker CLI found but the daemon is not running. Open the app once, accept its prompts, wait for it to start."
fi

# ---------------------------------------------------------------- 5. Opt-in
if [ "$WANT_ANDROID" = 1 ]; then
  say "5a. Android (opt-in)"
  if have brew; then
    for c in temurin@17 android-platform-tools android-studio; do
      if brew list --cask "$c" >/dev/null 2>&1; then note "$c present"; else act brew install --cask "$c"; fi
    done
    note "Open Android Studio once to install the SDK; then set ANDROID_HOME=\$HOME/Library/Android/sdk"
  fi
fi
if [ "$WANT_EAS" = 1 ]; then
  say "5b. eas-cli (opt-in)"
  if have eas; then note "present: $(eas --version 2>/dev/null | head -1)"
  elif have npm; then act npm install -g eas-cli
  else note "npm missing; run again after Node is installed"; fi
fi

# ---------------------------------------------------------------- 6. pipeline/
say "6. pipeline/ (Python)"
PYPROJ="$REPO/pipeline/pyproject.toml"; PYREQ="$REPO/pipeline/requirements.txt"
if [ -f "$PYPROJ" ]; then
  # Lower bound X.Y of requires-python (">=3.12,<3.13" -> 3.12). Matches double-quoted values only;
  # anything else falls back to 3.12 (pyproject pins the 3.12 line). `uv pip install -e` enforces the full specifier.
  PYVER="$(sed -n 's/^requires-python *= *"[^0-9]*\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p' "$PYPROJ" | head -1)"
  PYVER="${PYVER:-3.12}"
  note "manifest: pyproject.toml, Python $PYVER, extras: dev"
  VENV="$REPO/pipeline/.venv"
  if ! have uv; then note "uv missing; skipping venv setup"
  else
    if [ -x "$VENV/bin/python" ] && "$VENV/bin/python" -c "import sys; sys.exit(0 if '%d.%d' % sys.version_info[:2] == '$PYVER' else 1)"; then
      note "venv present with Python $PYVER"
    else
      # --clear: uv refuses to replace an existing (wrong-version) venv non-interactively without it
      act uv venv --clear --python "$PYVER" "$VENV"
    fi
    # uv pip install is a no-op when already satisfied. Editable install keeps tests importing local code.
    if [ "$CHECK_ONLY" = 0 ]; then
      ( cd "$REPO/pipeline" && VIRTUAL_ENV="$VENV" uv pip install -e ".[dev,annotate]" )
    else note "[check] would run: uv pip install -e \"pipeline[dev,annotate]\""; fi
  fi
elif [ -f "$PYREQ" ]; then
  note "requirements.txt found but no pyproject; not expected for this repo. Skipping rather than guessing."
else
  note "No pipeline/pyproject.toml or requirements.txt. Skipping."
fi

# ---------------------------------------------------------------- 7. app/
say "7. app/ (Expo)"
if [ -f "$REPO/app/package.json" ]; then
  if   [ -f "$REPO/app/pnpm-lock.yaml" ]; then PM=pnpm
  elif [ -f "$REPO/app/yarn.lock" ];       then PM=yarn
  else PM=npm; fi
  note "manifest: package.json, package manager: $PM"
  if ! have "$PM"; then note "$PM not installed (not in Brewfile). Install it, then re-run. Skipping."
  elif [ "$CHECK_ONLY" = 1 ]; then note "[check] would run $PM install in app/"
  else
    case "$PM" in
      npm)  if [ -f "$REPO/app/package-lock.json" ]; then (cd "$REPO/app" && npm ci)
            else note "No package-lock.json; running npm install (commit the lockfile it creates; CI needs it)"; (cd "$REPO/app" && npm install); fi ;;
      pnpm) (cd "$REPO/app" && pnpm install --frozen-lockfile) ;;
      yarn) (cd "$REPO/app" && yarn install --frozen-lockfile) ;;
    esac
  fi
else
  note "No app/package.json yet (Expo not scaffolded; frontend does that). Skipping."
fi

# ---------------------------------------------------------------- 8. .env
say "8. .env"
cd "$REPO"
if [ -f .env ]; then
  note ".env exists; leaving it untouched"
elif [ ! -f .env.example ]; then note "no .env.example; nothing to copy"
elif git check-ignore -q .env 2>/dev/null; then act cp -n .env.example .env; [ "$CHECK_ONLY" = 1 ] || note "created .env from .env.example (empty values; fill locally)"
else note "REFUSING to create .env: it is not gitignored. Fix .gitignore first."; fi
if [ -f .env ]; then
  if git check-ignore -q .env; then note "gitignore: .env is ignored (OK)"; else note "WARNING: .env is NOT gitignored"; fi
  if git ls-files --error-unmatch .env >/dev/null 2>&1; then note "WARNING: .env is TRACKED by git. Run: git rm --cached .env"; fi
fi

# ---------------------------------------------------------------- 8b. git hooks
say "8b. git hooks (.githooks/pre-commit: ruff + pytest in pipeline/)"
if ! git rev-parse --git-dir >/dev/null 2>&1; then note "not a git checkout; skipping"
elif [ "$(git config --local --get core.hooksPath 2>/dev/null)" = .githooks ]; then note "core.hooksPath already .githooks"
else
  OLD_HOOKS="$(git config --local --get core.hooksPath 2>/dev/null || true)"
  if [ -n "$OLD_HOOKS" ]; then
    if [ "$CHECK_ONLY" = 1 ]; then note "core.hooksPath is currently '$OLD_HOOKS'; a full run would replace it"
    else note "core.hooksPath was '$OLD_HOOKS'; replacing it. To restore: git config --local core.hooksPath '$OLD_HOOKS'"; fi
  fi
  act git config --local core.hooksPath .githooks
fi

# ---------------------------------------------------------------- 9. Verify
say "9. Verification"
FAIL=0
row() { # name, required(1/0), version-command...
  local name="$1" req="$2"; shift 2
  local out first rc=0
  out="$("$@" 2>&1)" || rc=$?   # capture all output first: no SIGPIPE false negatives
  first="$(printf '%s\n' "$out" | head -1)"
  if [ "$rc" = 0 ] && [ -n "$first" ]; then printf '  PASS     %-12s %s\n' "$name" "$first"
  elif [ "$rc" = 127 ]; then
    if [ "$req" = 1 ]; then printf '  MISSING  %-12s\n' "$name"; FAIL=1
    else printf '  optional %-12s (not installed)\n' "$name"; fi
  else
    printf '  FAILED   %-12s (rc=%s) %s\n' "$name" "$rc" "$first"
    if [ "$req" = 1 ]; then FAIL=1; fi
  fi
}
cmd() { have "$1" || return 127; "$@"; }   # 127 = not found on PATH
row git 1 cmd git --version
row brew 1 cmd brew --version
row node 1 cmd node --version
row npm 1 cmd npm --version
row watchman 1 cmd watchman --version
row uv 1 cmd uv --version
row gh 1 cmd gh --version
row jq 1 cmd jq --version
row direnv 0 cmd direnv version
row supabase 1 cmd supabase --version
row psql 1 cmd psql --version
row docker 1 cmd docker --version
row gitleaks 0 cmd gitleaks version
row shellcheck 0 cmd shellcheck --version
row xcodebuild 0 cmd xcodebuild -version
if [ -x "$REPO/pipeline/.venv/bin/python" ]; then row pipeline-venv 1 cmd "$REPO/pipeline/.venv/bin/python" --version
else printf '  MISSING  %-12s\n' pipeline-venv; FAIL=1; fi
if [ "$(git config --local --get core.hooksPath 2>/dev/null)" = .githooks ] && [ -x "$REPO/.githooks/pre-commit" ]; then
  printf '  PASS     %-12s core.hooksPath=.githooks\n' git-hooks
else printf '  MISSING  %-12s run scripts/bootstrap.sh (sets core.hooksPath)\n' git-hooks; FAIL=1; fi
if have docker && docker info >/dev/null 2>&1; then printf '  PASS     %-12s running\n' docker-daemon
else printf '  MISSING  %-12s not running\n' docker-daemon; FAIL=1; fi
[ "$WANT_EAS" = 1 ] && row eas 1 cmd eas --version
if [ "$WANT_ANDROID" = 1 ]; then row java 1 cmd java -version; row adb 1 cmd adb version; fi
# shellcheck disable=SC2015  # printf does not fail
[ -f "$REPO/.env" ] && printf '  PASS     %-12s present, gitignored=%s\n' .env "$(git check-ignore -q .env && echo yes || echo NO)" \
  || { printf '  MISSING  %-12s\n' .env; FAIL=1; }

cat <<'TODO'

Manual steps still needed (only you can do these; enter keys in .env or vendor dashboards, never in chat):
  - Supabase local stack (Phases 0-1): `supabase start`, then copy keys from `supabase status` into .env
  - Anthropic Console: API key `laminary-dev` + monthly spend limit  -> ANTHROPIC_API_KEY   (Phase 1)
  - TMDB: account + API key; confirm commercial terms                -> TMDB_API_KEY        (Phase 1)
  - WIKIMEDIA_USER_AGENT in .env with a real contact address         (Phase 1)
  - Embedding provider account [OPEN]                                -> EMBEDDING_API_KEY   (Phase 1)
  - Supabase hosted dev project, `supabase login`                                           (Phase 2)
  - Availability vendor [OPEN]                                       -> AVAILABILITY_API_KEY (Phase 2)
  - PostHog project                                                                         (Phase 3)
  - Apple Developer Program ($99/yr), Expo account + `eas login`                            (Phase 3)
TODO
[ "$CHECK_ONLY" = 1 ] || echo "  - Open a new terminal after this run so PATH changes take effect."
[ "$FAIL" = 0 ] && echo "RESULT: all required tools present." || echo "RESULT: some required items MISSING (see above). On a first run this is expected: open Docker once (accept prompts), open a new terminal, then re-run --check."
[ "$CHECK_ONLY" = 1 ] && exit 0 || exit "$FAIL"
