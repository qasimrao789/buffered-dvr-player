$ErrorActionPreference = 'Stop'

$Repo = 'https://github.com/qasimrao789/buffered-dvr-player.git'
$Tag = 'v1.0.0'
$Source = Split-Path -Parent $MyInvocation.MyCommand.Path
$Work = Join-Path $env:TEMP 'streamshift-dvr-publish'

Write-Host 'StreamShift DVR publisher' -ForegroundColor Cyan
Write-Host "Source: $Source"
Write-Host "Repo:   $Repo"
Write-Host

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw 'Git is not installed. Install Git for Windows first: winget install Git.Git'
}

if (Test-Path $Work) {
    Remove-Item $Work -Recurse -Force
}

git clone $Repo $Work
if ($LASTEXITCODE -ne 0) { throw 'git clone failed' }

# Remove all tracked working-tree files except .git, then copy this release source in.
Get-ChildItem $Work -Force | Where-Object { $_.Name -ne '.git' } | Remove-Item -Recurse -Force
Get-ChildItem $Source -Force | Where-Object {
    $_.Name -notin @('.git', '__pycache__', '.venv', 'build', 'dist', 'release', 'vendor')
} | ForEach-Object {
    Copy-Item $_.FullName -Destination $Work -Recurse -Force
}

Push-Location $Work
try {
    git add -A
    git config user.name 'qasimrao789'
    git config user.email '196899446+qasimrao789@users.noreply.github.com'

    $changes = git status --porcelain
    if ($changes) {
        git commit -m 'Release StreamShift DVR v1.0.0'
        if ($LASTEXITCODE -ne 0) { throw 'git commit failed' }
        git push origin main
        if ($LASTEXITCODE -ne 0) { throw 'git push failed' }
    } else {
        Write-Host 'Repository already matches this release source.' -ForegroundColor Yellow
    }

    $remoteTags = git ls-remote --tags origin "refs/tags/$Tag"
    if (-not $remoteTags) {
        git tag -a $Tag -m 'StreamShift DVR v1.0.0'
        git push origin $Tag
        if ($LASTEXITCODE -ne 0) { throw 'tag push failed' }
        Write-Host
        Write-Host "Pushed $Tag. GitHub Actions will now build the Windows installer and create the Release." -ForegroundColor Green
    } else {
        Write-Host "$Tag already exists on GitHub. Source was pushed, but the existing tag was left untouched." -ForegroundColor Yellow
    }
}
finally {
    Pop-Location
}

Write-Host
Write-Host 'Done. Open:' -ForegroundColor Green
Write-Host 'https://github.com/qasimrao789/buffered-dvr-player/actions'
Write-Host 'and then:'
Write-Host 'https://github.com/qasimrao789/buffered-dvr-player/releases'
