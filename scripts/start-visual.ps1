param([string]$Distribution = 'Ubuntu')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$wslRoot = & wsl -d $Distribution -- wslpath -a $projectRoot
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve project directory in WSL' }
& wsl -d $Distribution -- bash "$($wslRoot.Trim())/scripts/start-visual.sh"
exit $LASTEXITCODE
