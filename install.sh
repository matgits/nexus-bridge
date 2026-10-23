#!/bin/sh
# Install the Nexus bridge so local agents can reach the sidebar.
#
#   install.sh [--repo PATH] [--no-push] EXTENSION_ID [EXTENSION_ID...]
#   install.sh --uninstall
#
# Copies the host to a stable folder, registers it with every installed
# Chromium-based browser for the given extension ids, links `nexusctl` into
# ~/.local/bin and records the bookmarks repository. Nothing runs until the
# sidebar's Agent tab connects.
set -eu

HOST_NAME="com.nexus_ai_browser.bridge"
SRC_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HOME_DIR=${NEXUS_INSTALL_HOME:-$HOME}

case "$(uname -s)" in
  Darwin)
    STATE_DIR="$HOME_DIR/Library/Application Support/NexusBridge"
    SUPPORT="$HOME_DIR/Library/Application Support"
    BROWSERS="Google/Chrome|Google/Chrome Beta|Google/Chrome Dev|Google/Chrome Canary|Chromium|BraveSoftware/Brave-Browser|Microsoft Edge|Vivaldi|Arc/User Data"
    ;;
  Linux)
    STATE_DIR="${XDG_STATE_HOME:-$HOME_DIR/.local/state}/nexus-bridge"
    SUPPORT="${XDG_CONFIG_HOME:-$HOME_DIR/.config}"
    BROWSERS="google-chrome|google-chrome-beta|google-chrome-unstable|chromium|BraveSoftware/Brave-Browser|microsoft-edge|vivaldi"
    ;;
  *)
    echo "install.sh: only macOS and Linux are supported." >&2
    exit 2
    ;;
esac
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME_DIR/.config}/nexus-bridge"
BIN_DIR="$STATE_DIR/bin"
LINK_DIR="$HOME_DIR/.local/bin"

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 2
}

each_browser_dir() {
  # Calls "$1 <NativeMessagingHosts dir>" for every installed browser profile root.
  old_ifs=$IFS
  IFS='|'
  for browser in $BROWSERS; do
    IFS=$old_ifs
    if [ -d "$SUPPORT/$browser" ]; then
      "$1" "$SUPPORT/$browser/NativeMessagingHosts"
    fi
    IFS='|'
  done
  IFS=$old_ifs
}

remove_manifest() {
  if [ -f "$1/$HOST_NAME.json" ]; then
    rm -f "$1/$HOST_NAME.json"
    echo "Removed $1/$HOST_NAME.json"
  fi
}

if [ "${1:-}" = "--uninstall" ]; then
  each_browser_dir remove_manifest
  if [ -L "$LINK_DIR/nexusctl" ]; then rm -f "$LINK_DIR/nexusctl"; fi
  rm -rf "$BIN_DIR"
  echo "Uninstalled. Kept $CONFIG_DIR/config.json."
  exit 0
fi

REPO=""
PUSH=""
IDS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) [ $# -ge 2 ] || usage; REPO=$2; shift 2 ;;
    --no-push) PUSH="false"; shift ;;
    -h|--help) usage ;;
    -*) echo "install.sh: unknown option $1" >&2; usage ;;
    *)
      if ! printf '%s' "$1" | grep -Eq '^[a-p]{32}$'; then
        echo "install.sh: '$1' is not an extension id (32 letters a-p; copy it from the Agent tab)." >&2
        exit 2
      fi
      IDS="$IDS $1"
      shift
      ;;
  esac
done
[ -n "$IDS" ] || usage

PYTHON=""
for candidate in /usr/bin/python3 "$(command -v python3 2>/dev/null || true)"; do
  if [ -n "$candidate" ] && [ -x "$candidate" ] &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
    PYTHON=$candidate
    break
  fi
done
if [ -z "$PYTHON" ]; then
  echo "install.sh: Python 3.9 or newer is required (xcode-select --install provides it on macOS)." >&2
  exit 1
fi

if [ -n "$REPO" ]; then
  REPO=$(CDPATH= cd -- "$REPO" 2>/dev/null && pwd -P) || { echo "install.sh: no such folder: $REPO" >&2; exit 1; }
  if [ "$(git -C "$REPO" rev-parse --show-toplevel 2>/dev/null)" != "$REPO" ]; then
    echo "install.sh: $REPO is not the top folder of a Git repository." >&2
    exit 1
  fi
fi

umask 077
mkdir -p "$BIN_DIR" "$CONFIG_DIR"
chmod 700 "$STATE_DIR"
cp "$SRC_DIR/nexusctl.py" "$BIN_DIR/nexusctl.py"
cat > "$BIN_DIR/nexusctl" <<EOF
#!/bin/sh
exec "$PYTHON" "$BIN_DIR/nexusctl.py" "\$@"
EOF
chmod 755 "$BIN_DIR/nexusctl"
chmod 644 "$BIN_DIR/nexusctl.py"

# JSON is written by Python so paths with quotes or spaces stay valid.
MANIFEST=$("$PYTHON" - "$HOST_NAME" "$BIN_DIR/nexusctl" $IDS <<'EOF'
import json, sys
name, path, ids = sys.argv[1], sys.argv[2], sys.argv[3:]
print(json.dumps({
    "name": name,
    "description": "Nexus AI Browser bridge for local agents",
    "path": path,
    "type": "stdio",
    "allowed_origins": ["chrome-extension://%s/" % i for i in ids],
}, indent=2))
EOF
)

INSTALLED=0
write_manifest() {
  mkdir -p "$1"
  printf '%s\n' "$MANIFEST" > "$1/$HOST_NAME.json"
  chmod 644 "$1/$HOST_NAME.json"
  echo "Registered with $1"
  INSTALLED=$((INSTALLED + 1))
}
each_browser_dir write_manifest
if [ "$INSTALLED" -eq 0 ]; then
  echo "install.sh: no Chromium-based browser found under $SUPPORT." >&2
  exit 1
fi

"$PYTHON" - "$CONFIG_DIR/config.json" "$REPO" "$PUSH" <<'EOF'
import json, os, sys
path, repo, push = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path) as fh:
        config = json.load(fh)
    if not isinstance(config, dict):
        config = {}
except (OSError, ValueError):
    config = {}
if repo:
    config["repo"] = repo
config.setdefault("repo", None)
if push:
    config["push"] = push == "true"
config.setdefault("push", True)
config.setdefault("requireSecretScan", True)
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(config, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("Config: %s (repo: %s, push: %s)" % (path, config["repo"] or "not set", config["push"]))
EOF

mkdir -p "$LINK_DIR"
ln -sf "$BIN_DIR/nexusctl" "$LINK_DIR/nexusctl"
echo "Linked $LINK_DIR/nexusctl"
case ":$PATH:" in
  *":$LINK_DIR:"*) ;;
  *) echo "Add $LINK_DIR to PATH to run nexusctl directly." ;;
esac
if ! command -v gitleaks >/dev/null 2>&1 && [ ! -x /opt/homebrew/bin/gitleaks ] && [ ! -x /usr/local/bin/gitleaks ]; then
  echo "Sync needs gitleaks to block secrets: brew install gitleaks"
fi
echo "Done. Reload the extension or press Connect in the sidebar's Agent tab."
