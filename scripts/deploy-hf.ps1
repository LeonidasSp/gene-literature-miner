<#
.SYNOPSIS
    Deploy origin/main to the Hugging Face Space, without merge conflicts.

.DESCRIPTION
    The Space needs main's files plus two differences: README.md must start with
    Hugging Face's YAML front matter, and docs/ (screenshots) must be left out,
    because Hugging Face rejects binary files pushed with plain git.

    Doing that with `git merge --squash` conflicts, because every squash loses the
    link back to main's history. This script never merges. It builds the deploy
    commit directly from main's tree:

        main's files  +  front matter on README.md  -  the excluded paths

    using git plumbing and a temporary index, so it does NOT check out any branch
    and does NOT touch your working tree. The commit is a normal fast-forward on top
    of what Hugging Face already has. Before pushing it checks that the result
    differs from main ONLY by README.md and the excluded paths, that it adds no
    binary or oversized files, and (unless -NoWait) it then waits for the live Space
    to serve the new frontend.

    The front matter is taken from the README currently on the Space, so it stays
    whatever it is there (a default block is used on a first deploy).

.PARAMETER Ref
    What to deploy. Default: origin/main (what is merged on GitHub).

.PARAMETER ExcludePath
    Paths left out of the Hugging Face copy. Default: docs

.PARAMETER Remote
    The Hugging Face git remote. Default: space

.PARAMETER Branch
    The branch on that remote. Default: main

.PARAMETER DryRun
    Build and check the commit and show what would change, but push nothing and
    move no refs.

.PARAMETER Yes
    Do not ask for confirmation before pushing.

.PARAMETER NoWait
    Do not wait for the live Space to pick up the new version after pushing.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\deploy-hf.ps1
    Show the changes, ask, push, then wait until the Space serves the new frontend.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\deploy-hf.ps1 -DryRun
    See exactly what would be deployed without pushing anything.

.NOTES
    Pushing asks for your Hugging Face credentials as usual (user: your HF name,
    password: a write token). Windows PowerShell 5.1 and PowerShell 7 both work.
    This file is ASCII only on purpose (Windows PowerShell misreads UTF-8 scripts).
#>
[CmdletBinding()]
param(
    [string]$Ref = 'origin/main',
    [string[]]$ExcludePath = @('docs'),
    [string]$Remote = 'space',
    [string]$Branch = 'main',
    [switch]$DryRun,
    [switch]$Yes,
    [switch]$NoWait
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
try { [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12 } catch { }

# `powershell -File deploy-hf.ps1 -ExcludePath docs,assets` delivers ONE string
# "docs,assets" (only in-session calls get a real array), so split on commas here.
$ExcludePath = @($ExcludePath | ForEach-Object { $_ -split ',' } | Where-Object { $_.Trim() -ne '' })

$EmptyTree = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
$MaxFileBytes = 10MB   # Hugging Face refuses larger files without LFS/Xet

# ------------------------------------------------------------------ helpers
function Write-Step([string]$Message) { Write-Host ""; Write-Host "== $Message" -ForegroundColor Cyan }
function Write-Note([string]$Message) { Write-Host "   $Message" }
function Write-Warn([string]$Message) { Write-Host "   WARNING: $Message" -ForegroundColor Yellow }
function Stop-Deploy([string]$Message) {
    Write-Host ""
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function ConvertTo-CommandLineArg([string]$Value) {
    if ($Value -eq '') { return '""' }
    if ($Value -notmatch '[\s"]') { return $Value }
    return '"' + ($Value -replace '(\\*)"', '$1$1\"' -replace '(\\+)$', '$1$1') + '"'
}

# Run git and capture stdout as raw bytes (so UTF-8 files such as the README are
# never re-encoded by the console) plus stderr as text.
function Invoke-Git {
    param([Parameter(Mandatory)][string[]]$GitArgs)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'git'
    $psi.Arguments = ($GitArgs | ForEach-Object { ConvertTo-CommandLineArg $_ }) -join ' '
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $true
    $psi.WorkingDirectory = (Get-Location).Path
    $proc = [System.Diagnostics.Process]::Start($psi)
    $errTask = $proc.StandardError.ReadToEndAsync()
    $buffer = New-Object System.IO.MemoryStream
    $proc.StandardOutput.BaseStream.CopyTo($buffer)
    $proc.WaitForExit()
    $bytes = $buffer.ToArray()
    return [pscustomobject]@{
        Code  = $proc.ExitCode
        Bytes = $bytes
        Text  = ([System.Text.Encoding]::UTF8.GetString($bytes)).TrimEnd("`r", "`n")
        Err   = $errTask.Result
    }
}

function Invoke-GitChecked {
    param([Parameter(Mandatory)][string[]]$GitArgs)
    $r = Invoke-Git -GitArgs $GitArgs
    if ($r.Code -ne 0) {
        Stop-Deploy ("git " + ($GitArgs -join ' ') + " failed (exit $($r.Code)): " + $r.Err.Trim())
    }
    return $r
}

function Test-GitRef([string]$Name) {
    return ((Invoke-Git -GitArgs @('rev-parse', '--verify', '--quiet', $Name)).Code -eq 0)
}

function Split-NulRecords([string]$Text) {
    return @($Text -split "`0" | Where-Object { $_ -ne '' })
}

$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# ---------------------------------------------------------------- preflight
Write-Step 'Checking the repository'
$top = Invoke-Git -GitArgs @('rev-parse', '--show-toplevel')
if ($top.Code -ne 0) { Stop-Deploy 'Run this from inside the gene-literature-miner git repository.' }
Set-Location $top.Text
foreach ($name in @('origin', $Remote)) {
    if ((Invoke-Git -GitArgs @('remote', 'get-url', $name)).Code -ne 0) {
        Stop-Deploy "The git remote '$name' does not exist."
    }
}
$remoteUrl = (Invoke-GitChecked @('remote', 'get-url', $Remote)).Text
Write-Note "Deploying '$Ref' to remote '$Remote' ($remoteUrl), branch '$Branch'."

Write-Step 'Fetching the latest from GitHub and Hugging Face'
foreach ($name in @('origin', $Remote)) {
    $f = Invoke-Git -GitArgs @('fetch', '--quiet', $name)
    if ($f.Code -ne 0) { Stop-Deploy "Could not fetch '$name': $($f.Err.Trim())" }
}

if (-not (Test-GitRef "$Ref^{commit}")) { Stop-Deploy "'$Ref' does not exist. Has the pull request been merged?" }
$mainSha = (Invoke-GitChecked @('rev-parse', "$Ref^{commit}")).Text
$mainTree = (Invoke-GitChecked @('rev-parse', "$Ref^{tree}")).Text
$mainShort = (Invoke-GitChecked @('rev-parse', '--short', $mainSha)).Text
$mainSubject = (Invoke-GitChecked @('log', '-1', '--format=%s', $mainSha)).Text
Write-Note "$Ref is $mainShort : $mainSubject"

if ($Ref -eq 'origin/main' -and (Test-GitRef 'refs/heads/main')) {
    $counts = (Invoke-GitChecked @('rev-list', '--left-right', '--count', 'main...origin/main')).Text -split '\s+'
    if ([int]$counts[0] -gt 0 -or [int]$counts[1] -gt 0) {
        Write-Warn "Your local 'main' is $($counts[0]) commit(s) ahead and $($counts[1]) behind origin/main. Deploying origin/main (what GitHub has), not your local main."
    }
}

$spaceRef = "refs/remotes/$Remote/$Branch"
$spaceSha = $null
$spaceTree = $EmptyTree
if (Test-GitRef $spaceRef) {
    $spaceSha = (Invoke-GitChecked @('rev-parse', $spaceRef)).Text
    $spaceTree = (Invoke-GitChecked @('rev-parse', "$spaceSha^{tree}")).Text
    $spaceSubject = (Invoke-GitChecked @('log', '-1', '--format=%s', $spaceSha)).Text
    Write-Note "Hugging Face is at $((Invoke-GitChecked @('rev-parse','--short',$spaceSha)).Text) : $spaceSubject"
} else {
    Write-Note 'Hugging Face has no commits yet (first deploy).'
}

# ------------------------------------------------------------ build the tree
Write-Step 'Building the deploy commit from main (no merge, no checkout)'

# The front matter comes from the README already on the Space.
$frontMatter = $null
if ($spaceSha) {
    $spaceReadme = Invoke-Git -GitArgs @('show', "${spaceSha}:README.md")
    if ($spaceReadme.Code -eq 0) {
        $text = $Utf8NoBom.GetString($spaceReadme.Bytes).TrimStart([char]0xFEFF)
        $m = [regex]::Match($text, '(?s)\A---\r?\n.*?\r?\n---\r?\n')
        if ($m.Success) { $frontMatter = $m.Value }
    }
}
if (-not $frontMatter) {
    Write-Warn 'No front matter found on the Space; using the default block.'
    $emoji = [char]::ConvertFromUtf32(0x1F9EC)
    $frontMatter = "---`ntitle: Gene Literature Miner`nemoji: $emoji`ncolorFrom: blue`ncolorTo: green`nsdk: docker`napp_port: 7860`npinned: false`n---`n"
}

$tmpIndex = Join-Path ([System.IO.Path]::GetTempPath()) ("hf-sync-" + [guid]::NewGuid().ToString('N') + '.index')
$tmpReadme = Join-Path ([System.IO.Path]::GetTempPath()) ("hf-readme-" + [guid]::NewGuid().ToString('N') + '.md')
$env:GIT_INDEX_FILE = $tmpIndex
try {
    Invoke-GitChecked @('read-tree', $mainTree) | Out-Null

    $excluded = @()
    foreach ($p in $ExcludePath) {
        $clean = $p.Trim().Trim('/', '\').Replace('\', '/')
        if ($clean -eq '') { continue }
        $excluded += $clean
        Invoke-GitChecked @('rm', '-r', '-f', '--cached', '-q', '--ignore-unmatch', '--', $clean) | Out-Null
    }

    # README: front matter + a blank line + main's README, byte for byte.
    $mainReadme = Invoke-Git -GitArgs @('show', "${mainSha}:README.md")
    $readmeMode = '100644'
    $body = [byte[]]@()
    if ($mainReadme.Code -eq 0) {
        $body = $mainReadme.Bytes
        $entry = (Invoke-GitChecked @('ls-tree', $mainTree, '--', 'README.md')).Text
        if ($entry -match '^(\d+) ') { $readmeMode = $Matches[1] }
    } else {
        Write-Warn "main has no README.md; the Space README will contain only the front matter."
    }
    $bodyText = $Utf8NoBom.GetString($body)
    if ($bodyText.TrimStart([char]0xFEFF) -match '\A---\r?\n') {
        Write-Note 'main README already has front matter; using it as is.'
        $readmeBytes = $body
    } else {
        $readmeBytes = [byte[]]($Utf8NoBom.GetBytes($frontMatter + "`n") + $body)
    }
    [System.IO.File]::WriteAllBytes($tmpReadme, $readmeBytes)
    $blob = (Invoke-GitChecked @('hash-object', '-w', $tmpReadme)).Text
    Invoke-GitChecked @('update-index', '--add', '--cacheinfo', "$readmeMode,$blob,README.md") | Out-Null

    $newTree = (Invoke-GitChecked @('write-tree')).Text
}
finally {
    Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    Remove-Item $tmpIndex -ErrorAction SilentlyContinue
    Remove-Item $tmpReadme -ErrorAction SilentlyContinue
}

# ------------------------------------------------------------------- checks
Write-Step 'Checking the result'

# 1. Nothing to do?
if ($newTree -eq $spaceTree) {
    Write-Host ""
    Write-Host "Nothing to do: Hugging Face already has exactly what '$Ref' ($mainShort) would deploy." -ForegroundColor Green
    exit 0
}

# 2. The invariant: it may differ from main only by README.md and the excluded paths.
$diffFromMain = Invoke-GitChecked @('diff-tree', '-r', '-z', '--name-only', '--no-renames', $mainTree, $newTree)
$unexpected = @()
foreach ($path in (Split-NulRecords $diffFromMain.Text)) {
    $ok = ($path -eq 'README.md')
    foreach ($e in $excluded) { if ($path -eq $e -or $path.StartsWith("$e/")) { $ok = $true } }
    if (-not $ok) { $unexpected += $path }
}
if ($unexpected.Count -gt 0) {
    Stop-Deploy ("The deploy tree differs from $Ref in unexpected files: " + ($unexpected -join ', '))
}
Write-Note "Differs from $Ref only by README.md (front matter) and: $($excluded -join ', ')."

# 3. Nothing Hugging Face will reject: new/changed binary or oversized files.
$problems = @()
$numstat = Invoke-GitChecked @('diff-tree', '-r', '-z', '--numstat', '--no-renames', '--diff-filter=AM', $spaceTree, $newTree)
foreach ($rec in (Split-NulRecords $numstat.Text)) {
    if ($rec -match "^-\t-\t(.+)$") { $problems += "binary file: $($Matches[1])" }
}
$sizes = Invoke-GitChecked @('ls-tree', '-r', '-l', '-z', $newTree)
$changedPaths = @((Split-NulRecords (Invoke-GitChecked @('diff-tree', '-r', '-z', '--name-only', '--no-renames', '--diff-filter=AM', $spaceTree, $newTree)).Text))
foreach ($rec in (Split-NulRecords $sizes.Text)) {
    if ($rec -match "^\d+ \w+ [0-9a-f]+\s+(\d+)\t(.+)$") {
        if ([int64]$Matches[1] -gt $MaxFileBytes -and ($changedPaths -contains $Matches[2])) {
            $problems += ("file over 10 MB: {0} ({1:N1} MB)" -f $Matches[2], ([int64]$Matches[1] / 1MB))
        }
    }
}
if ($problems.Count -gt 0) {
    Write-Host ""
    foreach ($p in $problems) { Write-Host "   $p" -ForegroundColor Red }
    Stop-Deploy ("Hugging Face would reject this push (it refuses binary and oversized files sent with plain git). " +
                 "Leave them out with -ExcludePath, e.g.  -ExcludePath docs,<folder>.  Nothing was pushed.")
}
Write-Note 'No binary or oversized files are being added.'

# 4. Is the Space's last commit one this script (or a sync) made?
$outsideCommit = $false
if ($spaceSha -and $spaceSubject -notmatch '^Sync from main') {
    $outsideCommit = $true
    Write-Warn "The newest commit on the Space is not a sync ('$spaceSubject'). Deploying will overwrite whatever it changed."
}

# ------------------------------------------------------------------ summary
Write-Step "What will change on Hugging Face"
$stat = Invoke-GitChecked @('diff-tree', '-r', '--no-renames', '--name-status', $spaceTree, $newTree)
$lines = @($stat.Text -split "`n" | Where-Object { $_ -ne '' })
$added = @($lines | Where-Object { $_ -match '^A' }).Count
$modified = @($lines | Where-Object { $_ -match '^M' }).Count
$deleted = @($lines | Where-Object { $_ -match '^D' }).Count
Write-Note "$added added, $modified modified, $deleted deleted"
foreach ($l in ($lines | Select-Object -First 30)) { Write-Note $l }
if ($lines.Count -gt 30) { Write-Note "... and $($lines.Count - 30) more" }

$excludedText = $excluded -join ', '
$message = "Sync from main @ $mainShort (front matter added; excludes $excludedText)"
$commitArgs = @('commit-tree', $newTree, '-m', $message)
if ($spaceSha) { $commitArgs = @('commit-tree', $newTree, '-p', $spaceSha, '-m', $message) }
$commitResult = Invoke-Git -GitArgs $commitArgs
if ($commitResult.Code -ne 0) {
    Stop-Deploy ("Could not create the commit (is git user.name / user.email set?): " + $commitResult.Err.Trim())
}
$commit = $commitResult.Text
Write-Note "Deploy commit: $((Invoke-GitChecked @('rev-parse','--short',$commit)).Text) `"$message`""

if ($DryRun) {
    Write-Host ""
    Write-Host 'Dry run: nothing was pushed and no branches were moved.' -ForegroundColor Green
    exit 0
}

# --------------------------------------------------------------------- push
if (-not $Yes) {
    Write-Host ""
    $answer = Read-Host "Push this to Hugging Face ($Remote/$Branch)? [y/N]"
    if ($answer -notmatch '^(y|yes)$') { Stop-Deploy 'Cancelled. Nothing was pushed.' }
}

Write-Step "Pushing to $Remote/$Branch (you may be asked for your Hugging Face credentials)"
$previousPreference = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
& git push $Remote "${commit}:refs/heads/$Branch"
$pushCode = $LASTEXITCODE
$ErrorActionPreference = $previousPreference
if ($pushCode -ne 0) { Stop-Deploy 'The push was rejected or failed (see the message above). Nothing else was changed.' }

$null = Invoke-Git -GitArgs @('fetch', '--quiet', $Remote)
$landed = (Invoke-GitChecked @('rev-parse', $spaceRef)).Text
if ($landed -ne $commit) { Stop-Deploy "The push reported success but $spaceRef is $landed, not $commit." }
Write-Host "   Hugging Face now has $((Invoke-GitChecked @('rev-parse','--short',$commit)).Text)." -ForegroundColor Green

# Keep the old local 'hf' branch in step, unless it is checked out right now.
$current = Invoke-Git -GitArgs @('symbolic-ref', '--short', '-q', 'HEAD')
if ($current.Text -eq 'hf') {
    Write-Note "The local 'hf' branch is checked out, so it was left alone (it is not used by this script)."
} else {
    $null = Invoke-Git -GitArgs @('update-ref', 'refs/heads/hf', $commit)
}

# ------------------------------------------------------------- live check
if ($NoWait) { exit 0 }
if ($remoteUrl -notmatch 'huggingface\.co/spaces/([^/]+)/([^/.]+)') {
    Write-Note 'Skipping the live check (could not work out the Space URL from the remote).'
    exit 0
}
$spaceUrl = ("https://{0}-{1}.hf.space" -f $Matches[1].ToLower(), $Matches[2].ToLower())
Write-Step "Waiting for $spaceUrl to serve the new version"

$newFrontend = Invoke-Git -GitArgs @('show', "${commit}:frontend/index.html")
$oldFrontendSha = ''
if ($spaceSha) { $oldFrontendSha = (Invoke-Git -GitArgs @('rev-parse', '--verify', '--quiet', "${spaceSha}:frontend/index.html")).Text }
$newFrontendSha = (Invoke-Git -GitArgs @('rev-parse', '--verify', '--quiet', "${commit}:frontend/index.html")).Text
$canFingerprint = ($newFrontend.Code -eq 0 -and $newFrontendSha -ne $oldFrontendSha)
if (-not $canFingerprint) {
    Write-Note 'The frontend did not change in this deploy, so only "healthy" can be confirmed, not "new version".'
}
$wantHash = ''
if ($newFrontend.Code -eq 0) {
    $wantHash = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($newFrontend.Bytes))
}

$deadline = (Get-Date).AddMinutes(10)
$live = $false
while ((Get-Date) -lt $deadline) {
    try {
        $health = Invoke-WebRequest -Uri "$spaceUrl/health" -UseBasicParsing -TimeoutSec 20
        if ($health.StatusCode -eq 200) {
            if (-not $canFingerprint) { $live = $true; break }
            $page = Invoke-WebRequest -Uri "$spaceUrl/?nocache=$([guid]::NewGuid().ToString('N'))" -UseBasicParsing -TimeoutSec 30
            $gotHash = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($page.RawContentStream.ToArray()))
            if ($gotHash -eq $wantHash) { $live = $true; break }
            Write-Note 'Healthy, but still serving the previous version (the Space is rebuilding)...'
        }
    } catch {
        Write-Note 'Not answering yet (the Space is rebuilding)...'
    }
    Start-Sleep -Seconds 15
}
if ($live) {
    Write-Host ""
    Write-Host "LIVE: $spaceUrl is serving the new version." -ForegroundColor Green
    exit 0
}
Write-Warn "Gave up waiting after 10 minutes. The push succeeded; the Space may still be rebuilding. Check $spaceUrl (or its Settings > Restart)."
exit 0
