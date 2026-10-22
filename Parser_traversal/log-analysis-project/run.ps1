$ErrorActionPreference = "Stop"
python ".\main.py" --config ".\config.json"
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
