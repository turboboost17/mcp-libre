# Prerequisites

## LibreOffice 7.x or newer, with its UNO Python bridge

| Platform | Install | Python that can `import uno` |
|----------|---------|------------------------------|
| Windows | Official installer from libreoffice.org | `C:\Program Files\LibreOffice\program\python.exe`, found automatically |
| macOS | Official `.dmg` | `/Applications/LibreOffice.app/Contents/Resources/python`, found automatically |
| Debian / Ubuntu | `sudo apt install libreoffice python3-uno` | the system `python3` |
| Fedora | `sudo dnf install libreoffice libreoffice-pyuno` | the system `python3` |

If LibreOffice is installed somewhere unusual, set `LIBREMCP_OFFICE_PROGRAM` to its `program`
directory. You can also set `LIBREMCP_OFFICE_PYTHON` to point at an interpreter that can
`import uno`.

## Python 3.12+ and uv

The server itself runs in a uv-managed virtual environment:

```bash
# https://docs.astral.sh/uv/getting-started/installation/
uv sync --locked
```

Java is not required. Node.js is not required.

## Verify

```bash
uv run --locked python src/main.py --check
```

This prints the LibreOffice program directory, the UNO Python, the pipe name, and the enabled tool
groups. It then tries to connect. It never starts LibreOffice.
