#!/bin/bash
# Generate a Claude Desktop configuration for the LibreOffice MCP server from the template.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
TEMPLATE="$PROJECT_ROOT/config/claude_config.json.template"
OUTPUT="${1:-$HOME/.config/claude/claude_desktop_config.json}"

if [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    echo "Usage: $0 [OUTPUT_FILE]"
    echo "Default output: ~/.config/claude/claude_desktop_config.json"
    exit 0
fi

mkdir -p "$(dirname "$OUTPUT")"
if [ -f "$OUTPUT" ]; then
    echo "$OUTPUT already exists. Overwrite? (y/N)"
    read -r response
    if [[ ! "$response" =~ ^[Yy]$ ]]; then
        echo "Skipped. Merge the 'libreoffice' entry from $TEMPLATE manually instead."
        exit 0
    fi
fi

sed "s|/path/to/mcp-libre|$PROJECT_ROOT|g" "$TEMPLATE" > "$OUTPUT"
echo "Created $OUTPUT"
