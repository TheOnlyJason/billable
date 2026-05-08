# billable note — PowerShell helper
#
# Lightweight alternative to the AutoHotkey script. No external installs needed.
# Pops a small WPF window for note text, then runs `billable note`.
#
# Usage:
#   1. Source this file in your $PROFILE so the function is always available:
#        . "c:\Users\aiteam.user\Jason\billable\scripts\billable-note.ps1"
#   2. Then either:
#        bnote                          # opens a small input window
#        bnote -Text "what I did"       # bypass the dialog, log directly
#        bnote -Text "..." -Minutes 30 -Matter acme-website
#   3. Bind a global hotkey (optional): create a Windows shortcut to
#        powershell.exe -NoProfile -WindowStyle Hidden -Command "& { . 'c:\...\billable-note.ps1'; bnote }"
#      then assign a shortcut key in the .lnk's properties.

# Auto-resolve repo dir from THIS script's location (scripts/ is one level
# below the repo root). No editing needed if you cloned to any path.
# $PSScriptRoot is set when the file is dot-sourced or run as a script.
$Script:BillableDir = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Script:BillablePy  = Join-Path $BillableDir ".venv\Scripts\python.exe"

function bnote {
    [CmdletBinding()]
    param(
        [string]$Text,
        [int]$Minutes = 0,
        [string]$Matter = ""
    )

    if (-not $Text) {
        Add-Type -AssemblyName Microsoft.VisualBasic
        $Text = [Microsoft.VisualBasic.Interaction]::InputBox(
            "What did you just do?", "billable note", ""
        )
    }
    $Text = $Text.Trim()
    if (-not $Text) {
        Write-Host "  (cancelled)" -ForegroundColor DarkGray
        return
    }

    $args = @("-m", "billable.cli", "note", $Text)
    if ($Minutes -gt 0) { $args += @("--minutes", $Minutes) }
    if ($Matter)        { $args += @("--matter", $Matter) }

    Push-Location $Script:BillableDir
    try {
        & $Script:BillablePy @args
    } finally {
        Pop-Location
    }
}
