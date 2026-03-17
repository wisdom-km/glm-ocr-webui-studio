$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $root 'launch_glm_ocr_desktop.bat'
$shortcutPath = Join-Path $root 'GLM-OCR Desktop.lnk'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $target
$shortcut.WorkingDirectory = $root
$shortcut.WindowStyle = 1
$shortcut.Description = 'Launch GLM-OCR local server and web GUI'
$shortcut.Save()

Write-Host "Created shortcut: $shortcutPath"
