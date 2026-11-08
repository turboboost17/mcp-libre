#!/bin/bash
# LibreOffice MCP Server - quick commands

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT" || exit 1

case "${1:-help}" in
    test) uv run --locked pytest ;;
    server) exec uv run --locked python src/main.py ;;
    version) uv run --locked python src/main.py --version ;;
    status) uv run --locked python src/main.py --check ;;
    *)
        echo "Usage: $0 {test|server|version|status}"
        echo "  test     Run the test suite (uses its own headless LibreOffice)"
        echo "  server   Start the MCP server on stdio"
        echo "  version  Show the server version"
        echo "  status   Show LibreOffice discovery and connection status"
        ;;
esac
