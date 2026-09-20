<#
.SYNOPSIS
    End-to-end test of deploy-hf.ps1 in a throwaway sandbox (nothing real is touched).

.DESCRIPTION
    Builds a fake GitHub (origin.git) and a fake Hugging Face (hf.git) as bare
    repositories in a temp folder, then runs deploy-hf.ps1 against them through the
    situations that matter: first deploy, no-op, a real change, the conflict-prone
    case that broke `git merge --squash`, binary/oversized files, a commit made on
    the Space outside the sync, and running from another branch with a dirty tree.

    Run it after changing deploy-hf.ps1:
        powershell -ExecutionPolicy Bypass -File .\scripts\test-deploy-hf.ps1

    ASCII only on purpose (Windows PowerShell misreads UTF-8 scripts).
#>
[CmdletBinding()]
param([switch]$Keep)

$ErrorActionPreference = 'Stop'
$script:Checks = 0
$script:Failures = 0

function Check([bool]$Condition, [string]$Message, [string]$Detail = '') {
    $script:Checks++
    if ($Condition) { Write-Host "  PASS  $Message" -ForegroundColor Green }
    else {
        $script:Failures++
        Write-Host "  FAIL  $Message" -ForegroundColor Red
        if ($Detail) { Write-Host ("        detail: " + ($Detail -replace "`r?`n", "`n                ")) -ForegroundColor DarkYellow }
    }
}
# Compare lists of paths regardless of order or case.
function SameSet($Actual, $Expected) { ((@($Actual) | Sort-Object) -join ',') -eq ((@($Expected) | Sort-Object) -join ',') }
function Section([string]$Title) { Write-Host ""; Write-Host "--- $Title" -ForegroundColor Cyan }

$deploy = Join-Path $PSScriptRoot 'deploy-hf.ps1'
$psExe = (Get-Process -Id $PID).Path
$sandbox = Join-Path ([System.IO.Path]::GetTempPath()) ("hfdt-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $sandbox | Out-Null
$seed = Join-Path $sandbox 'seed'
$originGit = Join-Path $sandbox 'origin.git'
$hfGit = Join-Path $sandbox 'hf.git'
$work = Join-Path $sandbox 'work'   # the clone the deploy script runs in
$dev = Join-Path $sandbox 'dev'     # a second clone used to make changes on "GitHub"
$hfEdit = Join-Path $sandbox 'hfedit'

$env:GIT_AUTHOR_NAME = 'Test'; $env:GIT_AUTHOR_EMAIL = 'test@example.com'
$env:GIT_COMMITTER_NAME = 'Test'; $env:GIT_COMMITTER_EMAIL = 'test@example.com'
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
$emoji = [char]::ConvertFromUtf32(0x1F9EC)
$png = [byte[]](0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0, 0, 0, 0x0D, 1, 2, 3, 0, 255)

function G([string]$Dir, [string[]]$GitArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        $o = & git -C $Dir @GitArgs 2>&1
        if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed in ${Dir}: $o" }
        return (($o | Out-String).TrimEnd())
    } finally { $ErrorActionPreference = $old }
}
function GNoFail([string]$Dir, [string[]]$GitArgs) {
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { $o = & git -C $Dir @GitArgs 2>&1; return [pscustomobject]@{ Code = $LASTEXITCODE; Out = (($o | Out-String).TrimEnd()) } }
    finally { $ErrorActionPreference = $old }
}
function Put([string]$Repo, [string]$Rel, $Content) {
    $p = Join-Path $Repo $Rel
    New-Item -ItemType Directory -Force -Path (Split-Path $p) | Out-Null
    if ($Content -is [byte[]]) { [System.IO.File]::WriteAllBytes($p, $Content) }
    else { [System.IO.File]::WriteAllText($p, [string]$Content, $Utf8NoBom) }
}
function Setup-Repo([string]$Dir) {
    G $Dir @('config', 'core.autocrlf', 'false') | Out-Null
    G $Dir @('config', 'user.name', 'Test') | Out-Null
    G $Dir @('config', 'user.email', 'test@example.com') | Out-Null
}
function Change-Main([string]$Message, [scriptblock]$Edit) {
    & $Edit
    G $dev @('add', '-A') | Out-Null
    G $dev @('commit', '-q', '-m', $Message) | Out-Null
    G $dev @('push', '-q', 'origin', 'main') | Out-Null
}
function Deploy([string[]]$ExtraArgs = @(), [string]$Cwd = $work, [string]$Answer = $null) {
    Push-Location $Cwd
    $old = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try {
        if ($null -ne $Answer) {   # answer the "Push this to Hugging Face? [y/N]" prompt
            $out = $Answer | & $psExe -NoProfile -ExecutionPolicy Bypass -File $deploy -NoWait @ExtraArgs 2>&1 | Out-String
        } else {
            $out = & $psExe -NoProfile -ExecutionPolicy Bypass -File $deploy -NoWait @ExtraArgs 2>&1 | Out-String
        }
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $old; Pop-Location }
    return [pscustomobject]@{ Code = $code; Out = $out }
}
function HfSha { G $hfGit @('rev-parse', 'main') }
function SyncFetch { G $work @('fetch', '-q', 'origin') | Out-Null; G $work @('fetch', '-q', 'space') | Out-Null }
function DiffNames { SyncFetch; @((G $work @('diff', '--name-only', 'origin/main', 'space/main')) -split '\r?\n' | Where-Object { $_ -ne '' } | Sort-Object) }
function BlobOf([string]$Rev) { (GNoFail $work @('rev-parse', '--verify', '--quiet', $Rev)).Out }

try {
    # ------------------------------------------------------------------ setup
    G $sandbox @('init', '-q', '-b', 'main', $seed) | Out-Null
    Setup-Repo $seed
    Put $seed 'README.md' ("# $emoji Gene Literature Miner`n`nMine genes. Cafe: caf$([char]0xE9) - dash $([char]0x2014) done.`n")
    Put $seed 'backend/main.py' "print('v1')`n"
    Put $seed 'frontend/index.html' ((1..40 | ForEach-Object { "<p>line $_ v1</p>" }) -join "`n")
    Put $seed 'docs/screenshot.png' $png
    Put $seed 'tests/t.py' "pass`n"
    G $seed @('add', '-A') | Out-Null
    G $seed @('commit', '-q', '-m', 'seed') | Out-Null
    G $sandbox @('clone', '-q', '--bare', $seed, $originGit) | Out-Null
    G $sandbox @('init', '-q', '--bare', '-b', 'main', $hfGit) | Out-Null
    G $sandbox @('clone', '-q', '-c', 'core.autocrlf=false', $originGit, $work) | Out-Null
    Setup-Repo $work
    G $work @('remote', 'add', 'space', $hfGit) | Out-Null
    G $sandbox @('clone', '-q', '-c', 'core.autocrlf=false', $originGit, $dev) | Out-Null
    Setup-Repo $dev

    # ------------------------------------------------------- S0: first deploy
    Section 'First deploy to an empty Hugging Face'
    $r = Deploy @('-Yes')
    Check ($r.Code -eq 0) "exits 0 (got $($r.Code))"
    Check ((HfSha) -match '^[0-9a-f]{40}$') 'the Space now has a commit'
    $d = DiffNames
    Check (SameSet $d @('README.md', 'docs/screenshot.png')) 'differs from main only by README.md and docs/' ($d -join ', ')
    Check ((BlobOf 'space/main:docs/screenshot.png') -eq '') 'docs/ is not on the Space'
    $tmp = Join-Path $sandbox 'expected-readme.md'
    $cmd = "git -C `"$work`" cat-file blob origin/main:README.md > `"$tmp.body`""
    cmd /c $cmd | Out-Null
    $fmDefault = "---`ntitle: Gene Literature Miner`nemoji: $emoji`ncolorFrom: blue`ncolorTo: green`nsdk: docker`napp_port: 7860`npinned: false`n---`n"
    [System.IO.File]::WriteAllBytes($tmp, [byte[]]($Utf8NoBom.GetBytes($fmDefault + "`n") + [System.IO.File]::ReadAllBytes("$tmp.body")))
    Check ((BlobOf 'space/main:README.md') -eq (G $work @('hash-object', $tmp))) 'README = default front matter + blank line + main README, byte for byte (emoji and accents intact)'
    $short = G $work @('rev-parse', '--short', 'origin/main')
    Check ((G $work @('log', '-1', '--format=%s', 'space/main')) -like "Sync from main @ $short*") 'commit message names the main commit'
    Check (((G $work @('rev-list', '--parents', '-n', '1', 'space/main')) -split ' ').Count -eq 1) 'first deploy has no parent'

    # ----------------------------------------------------------- S1: no-op
    Section 'Running it again with nothing new'
    $before = HfSha
    $r = Deploy @('-Yes')
    Check ($r.Code -eq 0 -and $r.Out -match 'Nothing to do') 'says there is nothing to do'
    Check ((HfSha) -eq $before) 'pushes nothing (no empty commit)'

    # --------------------------------------------- S2: change + front matter kept
    Section 'A real change (front matter edited on the Space, docs changes, new code)'
    G $sandbox @('clone', '-q', '-c', 'core.autocrlf=false', $hfGit, $hfEdit) | Out-Null
    Setup-Repo $hfEdit
    $readme = [System.IO.File]::ReadAllText((Join-Path $hfEdit 'README.md'), $Utf8NoBom).Replace('title: Gene Literature Miner', 'title: Custom Space Title')
    Put $hfEdit 'README.md' $readme
    G $hfEdit @('commit', '-q', '-am', 'Sync from main (edit front matter)') | Out-Null
    G $hfEdit @('push', '-q', 'origin', 'main') | Out-Null
    Change-Main 'code + docs change' {
        Put $dev 'backend/main.py' "print('v2')`n"
        Put $dev 'backend/new.py' "x = 1`n"
        Put $dev 'README.md' ("# $emoji Gene Literature Miner`n`nNew section added.`n")
        Put $dev 'docs/two.png' ($png + [byte[]](9, 9, 0))
    }
    $prev = HfSha
    $hfBranchBefore = G $work @('rev-parse', 'refs/heads/hf')
    $r = Deploy @('-DryRun')
    Check ($r.Code -eq 0 -and $r.Out -match 'Dry run') 'dry run succeeds and says so'
    Check ((HfSha) -eq $prev) 'dry run pushes nothing'
    Check ((G $work @('rev-parse', 'refs/heads/hf')) -eq $hfBranchBefore) 'dry run moves no local branches'
    $r = Deploy @('-Yes')
    Check ($r.Code -eq 0) "deploy exits 0 (got $($r.Code))"
    SyncFetch
    Check ((G $work @('rev-parse', 'space/main^')) -eq $prev) 'a normal fast-forward on top of what the Space had'
    $d = DiffNames
    Check (SameSet $d @('README.md', 'docs/screenshot.png', 'docs/two.png')) 'differs from main only by README.md and docs/' ($d -join ', ')
    Check ((G $work @('show', 'space/main:README.md')) -match 'title: Custom Space Title') 'front matter was taken from the Space (kept the edit)'
    Check ((G $work @('show', 'space/main:README.md')) -match 'New section added') 'README body is main''s new text'
    Check ((BlobOf 'space/main:backend/main.py') -eq (BlobOf 'origin/main:backend/main.py')) 'code files are identical to main'
    $st = G $work @('status', '--porcelain')
    $why = ''
    if ($st -ne '') { $why = $st + "`n" + ((G $work @('diff', '--no-color', '--stat')) ) + "`n" + ((GNoFail $work @('diff', '--no-color', '--text', '--', 'README.md')).Out -split '\r?\n' | Select-Object -First 14 | Out-String) }
    Check ($st -eq '') 'your working tree was not touched' $why
    Check ((G $work @('symbolic-ref', '--short', 'HEAD')) -eq 'main') 'you are still on main'
    Check ((G $work @('rev-parse', 'refs/heads/hf')) -eq (HfSha)) 'the old local hf branch was kept in step'

    # ------------------------------------------ S3: the case that broke the squash
    Section 'Overlapping edits to the same file across syncs (what conflicted with git merge --squash)'
    foreach ($n in 2, 3, 4) {
        Change-Main "index.html edit $n" {
            $lines = (1..40 | ForEach-Object { if ($_ -ge 10 -and $_ -le 20) { "<p>line $_ edit $n</p>" } else { "<p>line $_ v1</p>" } })
            Put $dev 'frontend/index.html' ($lines -join "`n")
        }
        $r = Deploy @('-Yes')
        SyncFetch
        Check ($r.Code -eq 0 -and (BlobOf 'space/main:frontend/index.html') -eq (BlobOf 'origin/main:frontend/index.html')) "round ${n}: no conflict, index.html identical to main"
    }

    # ------------------------------------------------ S4: binary outside docs
    Section 'A binary file outside docs/'
    Change-Main 'add a logo' { Put $dev 'assets/logo.png' $png }
    $prev = HfSha
    $r = Deploy @('-Yes')
    Check ($r.Code -ne 0) 'refuses (non-zero exit)'
    Check ($r.Out -match 'binary file: assets/logo\.png') 'names the offending file'
    Check ((HfSha) -eq $prev) 'pushed nothing'
    $r = Deploy @('-Yes', '-ExcludePath', 'docs,assets')
    Check ($r.Code -eq 0) 'succeeds when the folder is excluded (comma list through -File)' $r.Out
    Check ((BlobOf 'space/main:assets/logo.png') -eq '') 'the excluded folder is not on the Space'
    Change-Main 'remove the logo' { G $dev @('rm', '-q', '-r', 'assets') | Out-Null }

    # ---------------------------------------------------- S5: oversized file
    Section 'A file over 10 MB'
    Change-Main 'add a huge file' { Put $dev 'data/big.txt' ('a' * 11MB) }
    $prev = HfSha
    $r = Deploy @('-Yes')
    Check ($r.Code -ne 0 -and $r.Out -match 'file over 10 MB: data/big\.txt') 'refuses and names it'
    Check ((HfSha) -eq $prev) 'pushed nothing'
    Change-Main 'remove the huge file' { G $dev @('rm', '-q', '-r', 'data') | Out-Null }

    # -------------------------------------------------- S6: outside commit
    Section 'Someone committed to the Space by hand'
    G $hfEdit @('pull', '-q', 'origin', 'main') | Out-Null
    Put $hfEdit 'backend/main.py' "print('hand edit on the space')`n"
    G $hfEdit @('commit', '-q', '-am', 'manual hotfix') | Out-Null
    G $hfEdit @('push', '-q', 'origin', 'main') | Out-Null
    Change-Main 'another change' { Put $dev 'backend/main.py' "print('v3')`n" }
    $prev = HfSha
    $r = Deploy @('-DryRun')
    Check ($r.Code -eq 0 -and $r.Out -match 'not a sync') 'warns that it will overwrite a hand-made commit'
    Check ((HfSha) -eq $prev) 'dry run leaves it alone'
    $r = Deploy @('-Yes')
    Check ($r.Code -eq 0 -and (BlobOf 'space/main:backend/main.py') -eq (BlobOf 'origin/main:backend/main.py')) 'with -Yes the Space is brought back in line with main'

    # ------------------------------------- S7: other branch, sub-folder, dirty
    Section 'Run from another branch, from a sub-folder, with uncommitted work'
    G $work @('checkout', '-q', '-b', 'wip') | Out-Null
    Put $work 'wip.txt' "unfinished`n"
    Put $work 'backend/main.py' "print('my local edit')`n"
    $dirtyBefore = G $work @('status', '--porcelain')
    Change-Main 'change while wip is open' { Put $dev 'backend/other.py' "y = 2`n" }
    $r = Deploy @('-Yes') (Join-Path $work 'backend')
    SyncFetch
    Check ($r.Code -eq 0 -and (BlobOf 'space/main:backend/other.py') -eq (BlobOf 'origin/main:backend/other.py')) 'deploys fine from a sub-folder'
    Check ((G $work @('symbolic-ref', '--short', 'HEAD')) -eq 'wip') 'still on the same branch'
    Check ((G $work @('status', '--porcelain')) -eq $dirtyBefore) 'uncommitted work is exactly as it was'
    Check ((Get-Content (Join-Path $work 'backend/main.py') -Raw) -match 'my local edit') 'the locally edited file was not overwritten'

    # ------------------------------------------------ the confirmation prompt
    Section 'Without -Yes it asks first'
    Change-Main 'change for the prompt test' { Put $dev 'backend/prompt.py' "z = 3`n" }
    $prev = HfSha
    $r = Deploy @() $work 'n'
    # (Read-Host writes its prompt to the console, not stdout, so the prompt text itself
    # is not in $r.Out; that nothing was pushed proves it really did stop and ask.)
    Check ($r.Code -ne 0 -and $r.Out -match 'Cancelled\. Nothing was pushed') 'answering n cancels' $r.Out
    Check ((HfSha) -eq $prev) 'and pushes nothing (so it really stopped to ask)'
    $r = Deploy @() $work 'y'
    SyncFetch
    Check ($r.Code -eq 0 -and (BlobOf 'space/main:backend/prompt.py') -eq (BlobOf 'origin/main:backend/prompt.py')) 'answering y deploys' $r.Out

    # ------------------------------------------------------ bad situations
    Section 'Error handling'
    $r = Deploy @('-Yes', '-Ref', 'origin/nope')
    Check ($r.Code -ne 0 -and $r.Out -match 'does not exist') 'a missing ref gives a clear error'
    $r = Deploy @('-Yes', '-Remote', 'nosuchremote')
    Check ($r.Code -ne 0 -and $r.Out -match "remote 'nosuchremote' does not exist") 'a missing remote gives a clear error'
    $r = Deploy @('-Yes') $sandbox
    Check ($r.Code -ne 0 -and $r.Out -match 'inside the gene-literature-miner git repository') 'running outside a repo gives a clear error'
}
finally {
    Remove-Item Env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    if (-not $Keep) { Remove-Item -Recurse -Force $sandbox -ErrorAction SilentlyContinue }
    else { Write-Host "Sandbox kept at $sandbox" }
}

Write-Host ""
if ($script:Failures -eq 0) { Write-Host "ALL $($script:Checks) CHECKS PASSED" -ForegroundColor Green; exit 0 }
Write-Host "$($script:Failures) of $($script:Checks) CHECKS FAILED" -ForegroundColor Red
exit 1
