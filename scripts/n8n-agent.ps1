# Starts the n8n Kiro CLI agent. Reads N8N_MCP_TOKEN from this repo's git-ignored .env into this
# process only (never printed, never written elsewhere), then runs `kiro-cli chat --agent n8n`.
$envFile = Join-Path $PSScriptRoot '..\.env'
$line = Get-Content $envFile | Where-Object { $_ -match '^N8N_MCP_TOKEN=' } | Select-Object -First 1
if (-not $line) { Write-Error 'N8N_MCP_TOKEN is not set in .env'; exit 1 }
$env:N8N_MCP_TOKEN = $line.Substring('N8N_MCP_TOKEN='.Length).Trim()
kiro-cli chat --agent n8n @args
