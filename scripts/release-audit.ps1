$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Failures = [Collections.Generic.List[string]]::new()
$Skip = @(".git", ".runtime", ".venv", "bin", "inbox", "archives", "work", "markdown", "failed", "outputs")
$TextExtensions = @(".py", ".ps1", ".md", ".yaml", ".yml", ".json", ".cff", ".toml", ".txt", ".example", "")
$SensitiveExtensions = @(".pem", ".key", ".p12", ".pfx", ".token", ".sqlite", ".sqlite3", ".db")

$Forbidden = @(
    @{ Name = "absolute Windows user path"; Pattern = '(?i)[A-Z]:\\Users\\' },
    @{ Name = "hard-coded UUID"; Pattern = '(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b' },
    @{ Name = "live MinerU or MiMo key"; Pattern = '(?im)^(MINERU_API_TOKEN(?:_[A-Z0-9]+)?|MIMO_API_KEY(?:_[0-9]+)?)[ \t]*=[ \t]*[^\s#]{12,}' },
    @{ Name = "private-key block"; Pattern = '(?i)-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----' },
    @{ Name = "GitHub token"; Pattern = '(?i)\b(?:gh[pousr]_[A-Za-z0-9_]{36,}|github_pat_[A-Za-z0-9_]{40,})\b' },
    @{ Name = "AWS access key"; Pattern = '\bAKIA[0-9A-Z]{16}\b' },
    @{ Name = "Google API key"; Pattern = '\bAIza[0-9A-Za-z_-]{35}\b' },
    @{ Name = "likely OpenAI-style key"; Pattern = '(?i)\bsk-[A-Za-z0-9_-]{20,}\b' },
    @{ Name = "Cloudflare token assignment"; Pattern = '(?im)^(?:CF_API_TOKEN|CLOUDFLARE_API_TOKEN)[ \t]*=[ \t]*[^\s#]{20,}' }
    @{ Name = "structured credential value"; Pattern = '(?i)"(?:TunnelSecret|client_secret|private_key|refresh_token)"\s*:\s*"[^"\r\n]{12,}"' }
)

function Test-PublishableText([string]$RelativePath, [string]$Text, [string]$Source) {
    $Normalized = $RelativePath.Replace('\', '/')
    foreach ($Rule in $Forbidden) {
        if ($Text -match $Rule.Pattern) { $Failures.Add("$($Rule.Name) in ${Source}:$Normalized") }
    }
}

if (Test-Path (Join-Path $Root ".git")) {
    $Publishable = & git -C $Root ls-files --cached --others --exclude-standard
    if ($LASTEXITCODE -ne 0) { throw "git ls-files failed." }
    $Files = @($Publishable | ForEach-Object {
        $Candidate = Join-Path $Root $_
        if (Test-Path -LiteralPath $Candidate -PathType Leaf) { Get-Item -LiteralPath $Candidate }
    })
} else {
    $Files = Get-ChildItem -LiteralPath $Root -Recurse -Force -File | Where-Object {
        $Relative = $_.FullName.Substring($Root.Length).TrimStart('\')
        -not ($Skip | Where-Object { $Relative -eq $_ -or $Relative.StartsWith("$_\") })
    }
}

foreach ($File in $Files) {
    $Relative = $File.FullName.Substring($Root.Length + 1).Replace('\', '/')
    $LowerName = $File.Name.ToLowerInvariant()
    if (($LowerName -match '^\.env(?:\.|$)' -and $LowerName -ne ".env.example") -or
        $File.Extension.ToLowerInvariant() -in $SensitiveExtensions -or
        $LowerName -match '\.secrets\.') {
        $Failures.Add("sensitive file type is publishable: $Relative")
    }
    if ($File.Extension.ToLowerInvariant() -notin $TextExtensions) { continue }
    $Text = Get-Content -Raw -LiteralPath $File.FullName -ErrorAction SilentlyContinue
    Test-PublishableText $Relative $Text "working tree"
}

$Unexpected = $Files | Where-Object {
    $_.Name -match '^(state\.db|\.env)$' -or $_.Extension.ToLowerInvariant() -in
        ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".mp4", ".mkv", ".7z", ".rar"
}
foreach ($File in $Unexpected) {
    $Failures.Add("private/generated artifact present: $($File.FullName.Substring($Root.Length + 1))")
}

$Config = Get-Content -Raw (Join-Path $Root "config.example.yaml")
foreach ($Unsafe in "delete_videos: true", "delete_archives_after_extract: true", "delete_other_source_after_markdown: true", "permanently_delete_source_after_search: true") {
    if ($Config.Contains($Unsafe)) { $Failures.Add("unsafe example default: $Unsafe") }
}

foreach ($Script in $Files | Where-Object { $_.Extension -eq ".ps1" }) {
    $Tokens = $null
    $ParseErrors = $null
    [Management.Automation.Language.Parser]::ParseFile($Script.FullName, [ref]$Tokens, [ref]$ParseErrors) | Out-Null
    foreach ($ParseError in @($ParseErrors)) {
        $Failures.Add("PowerShell parse error in $($Script.FullName.Substring($Root.Length + 1)): $($ParseError.Message)")
    }
}

foreach ($Workflow in $Files | Where-Object { $_.FullName -match '[\\/]\.github[\\/]workflows[\\/].+\.ya?ml$' }) {
    $WorkflowText = Get-Content -Raw -LiteralPath $Workflow.FullName
    foreach ($Match in [regex]::Matches($WorkflowText, '(?m)^\s*uses:\s*\S+@([^\s#]+)')) {
        if ($Match.Groups[1].Value -notmatch '^[0-9a-f]{40}$') {
            $Failures.Add("mutable GitHub Action reference: $($Workflow.Name) -> $($Match.Groups[1].Value)")
        }
    }
}

if (Test-Path (Join-Path $Root ".git")) {
    $Commits = @(& git -C $Root rev-list --all)
    foreach ($Commit in $Commits) {
        $Paths = @(& git -C $Root ls-tree -r --name-only $Commit)
        foreach ($Path in $Paths) {
            $Normalized = $Path.Replace('\', '/')
            $Extension = [IO.Path]::GetExtension($Path).ToLowerInvariant()
            $Name = [IO.Path]::GetFileName($Path).ToLowerInvariant()
            if (($Name -match '^\.env(?:\.|$)' -and $Name -ne ".env.example") -or
                $Extension -in $SensitiveExtensions -or $Name -match '\.secrets\.') {
                $Failures.Add("sensitive path in reachable history $($Commit.Substring(0, 12)): $Normalized")
            }
            if ($Extension -notin $TextExtensions) { continue }
            $Object = "${Commit}:$Path"
            $HistoricalText = (& git -C $Root show $Object 2>$null) -join "`n"
            if ($LASTEXITCODE -eq 0) { Test-PublishableText $Normalized $HistoricalText "history $($Commit.Substring(0, 12))" }
        }
    }
    $Fsck = @(& git -C $Root fsck --no-reflogs --unreachable 2>&1)
    if ($Fsck | Where-Object { $_ -match 'unreachable|dangling' }) {
        Write-Warning "Local .git contains unreachable objects. They are not pushed by normal Git operations; use a fresh clone for release archives."
    }
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (Test-Path $Python) {
    & $Python -m py_compile (Join-Path $Root "ingest.py")
    if ($LASTEXITCODE -ne 0) { $Failures.Add("ingest.py did not compile") }
}

if ($Failures.Count) {
    $Failures | Sort-Object -Unique | ForEach-Object { Write-Error $_ }
    throw "Release audit failed with $($Failures.Count) finding(s)."
}
Write-Host "Release audit passed: publishable files and all reachable Git history contain no detected private data, live secrets, unsafe defaults, mutable Actions, or parse errors."
