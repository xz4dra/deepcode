#!/usr/bin/env bash
# Install the DeepCode launcher so `deepcode` works from any directory.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BIN_DIR="${XDG_BIN_HOME:-$HOME/.local/bin}"
LAUNCHER="$BIN_DIR/deepcode"

echo "DeepCode: $APP_DIR"
echo "Python:   $PYTHON_BIN"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    echo "Creating virtual environment in $VENV_DIR"
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

echo "Installing dependencies"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

mkdir -p "$BIN_DIR"

cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec "$VENV_DIR/bin/python" "$APP_DIR/main.py" "\$@"
EOF
chmod +x "$LAUNCHER"

# Make ~/.local/bin available in every kind of shell.  .zshenv is read by
# every zsh invocation (interactive, login, scripts); .profile/.bash_profile
# cover POSIX/login bash; the interactive rc file covers the desktop shell.
PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'

ensure_path() {
    local file="$1"
    touch "$file"
    if ! grep -Fq '.local/bin' "$file"; then
        {
            printf '\n# DeepCode launcher\n'
            printf '%s\n' "$PATH_LINE"
        } >> "$file"
        echo "Added ~/.local/bin to PATH in $file"
    fi
}

ensure_path "$HOME/.zshenv"
ensure_path "$HOME/.profile"
ensure_path "$HOME/.bash_profile"

case "$(basename "${SHELL:-}")" in
    zsh)  RC_FILE="$HOME/.zshrc" ;;
    bash) RC_FILE="$HOME/.bashrc" ;;
    *)    RC_FILE="$HOME/.profile" ;;
esac
ensure_path "$RC_FILE"

echo
echo "Installed: $LAUNCHER"
echo "Run 'exec \$SHELL' or open a new terminal, then:"
echo "  deepcode"
echo
echo "The file tree uses the current directory, so this also works:"
echo "  cd /path/to/project && deepcode"
