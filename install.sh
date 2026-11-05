#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$HOME/.local/share/youtube-dagbanli"
BIN_DIR="$HOME/.local/bin"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install it with: sudo apt install python3 python3-venv"
  exit 1
fi

mkdir -p "$APP_DIR" "$BIN_DIR"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install "$ROOT"

cat > "$BIN_DIR/ydl" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/ydl" "\$@"
EOF
chmod +x "$BIN_DIR/ydl"

echo
echo "YouTube Dagbanli installed."
echo "Run: $BIN_DIR/ydl --help"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add this to your shell profile if needed: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac
