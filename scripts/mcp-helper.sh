#!/bin/bash
# LibreOffice MCP Server helper: dependency checks, tests, and running the server.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok() { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; }

check_dependencies() {
    local missing=0
    if command -v soffice >/dev/null 2>&1 || command -v libreoffice >/dev/null 2>&1; then
        ok "LibreOffice found"
    else
        fail "LibreOffice not found in PATH (set LIBREMCP_OFFICE_PROGRAM if installed elsewhere)"
        missing=1
    fi
    if command -v uv >/dev/null 2>&1; then
        ok "uv found: $(uv --version)"
    else
        fail "uv not found: https://docs.astral.sh/uv/"
        missing=1
    fi
    if [ $missing -eq 0 ]; then
        echo ""
        echo "Server diagnostics (does not start LibreOffice):"
        (cd "$PROJECT_ROOT" && uv run --locked python src/main.py --check)
    fi
    return $missing
}

show_requirements() {
    cat << EOF
Requirements
  1. LibreOffice 7.x or newer, including its Python/UNO bridge
       Windows/macOS: bundled with the official installer
       Debian/Ubuntu: sudo apt install libreoffice python3-uno
  2. Python 3.12+ and uv (https://docs.astral.sh/uv/)

The server reaches the running LibreOffice through a local named pipe only; no network port is opened.
EOF
}

run_tests() {
    cd "$PROJECT_ROOT"
    uv run --locked pytest "$@"
}

start_server() {
    cd "$PROJECT_ROOT"
    exec uv run --locked python src/main.py
}

show_help() {
    cat << EOF
Usage: $0 COMMAND

Commands:
  check         Check dependencies and the UNO connection
  requirements  Show installation requirements
  test          Run the test suite (extra arguments are passed to pytest)
  server        Start the MCP server on stdio
  config        Generate a Claude Desktop configuration (see generate-config.sh)
  help          Show this help
EOF
}

case "${1:-help}" in
    check) check_dependencies ;;
    requirements) show_requirements ;;
    test) shift; run_tests "$@" ;;
    server) start_server ;;
    config) shift; exec "$SCRIPT_DIR/generate-config.sh" "$@" ;;
    *) show_help ;;
esac
