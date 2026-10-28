# Make one Reel from YOUR media folders (dry-run: does not post).
#   scripts\run_once.ps1             -> dry run
#   scripts\run_once.ps1 -Publish    -> really publish (needs publishing.enabled: true + credentials)
param([switch]$Publish, [string]$Slot = "manual")
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($Publish) { & .\.venv\Scripts\python.exe -m reel_agent run --slot $Slot --publish }
else          { & .\.venv\Scripts\python.exe -m reel_agent run --slot $Slot }
