$ErrorActionPreference = 'Stop'
$toolRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$outputRoot = Join-Path $toolRoot 'release'
$venvPython = Join-Path $toolRoot '.venv-build\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } else { 'python' }
$internalName = 'OriginBatchLayoutAssistant'
$displayName = 'Origin' + [char]0x6279 + [char]0x91CF + [char]0x6392 + `
    [char]0x7248 + [char]0x52A9 + [char]0x624B

Push-Location $toolRoot
try {
    if (Test-Path $outputRoot) {
        $resolvedToolRoot = [IO.Path]::GetFullPath($toolRoot)
        $resolvedOutputRoot = [IO.Path]::GetFullPath($outputRoot)
        if (-not $resolvedOutputRoot.StartsWith(
            $resolvedToolRoot,
            [StringComparison]::OrdinalIgnoreCase
        )) {
            throw 'Refusing to clean a release directory outside the tool workspace.'
        }
        Remove-Item -LiteralPath $resolvedOutputRoot -Recurse -Force
    }
    & $python -m unittest discover -s tests -v
    & $python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onedir `
        --name $internalName `
        --distpath $outputRoot `
        --workpath (Join-Path $toolRoot 'build') `
        --specpath (Join-Path $toolRoot 'build') `
        --paths $toolRoot `
        (Join-Path $toolRoot 'launcher.py')

    $builtDirectory = Join-Path $outputRoot $internalName
    Rename-Item `
        -LiteralPath (Join-Path $builtDirectory ($internalName + '.exe')) `
        -NewName ($displayName + '.exe')
    Rename-Item -LiteralPath $builtDirectory -NewName $displayName
    Copy-Item `
        -LiteralPath (Join-Path $toolRoot 'README.md') `
        -Destination (Join-Path (Join-Path $outputRoot $displayName) 'README.md')
}
finally {
    Pop-Location
}
