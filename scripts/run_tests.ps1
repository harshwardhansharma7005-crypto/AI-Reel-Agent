# Run the automated test-suite (takes ~1-3 minutes: it renders real video).
Set-Location (Split-Path $PSScriptRoot -Parent)
& .\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
exit $LASTEXITCODE
