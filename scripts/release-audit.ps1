$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Failures = [Collections.Generic.List[string]]::new()
$Skip = @(".git", ".runtime", ".venv", "bin", "inbox", "archives", "work", "markdown", "failed", "outputs")
if (Test-Path (Join-Path $Root ".git")) {
    $Publishable = & git -C $Root ls-files --cached --others --exclude-standard
    $Files = @($Publishable | ForEach-Object { Get-Item -LiteralPath (Join-Path $Root $_) })
} else {
    $Files = Get-ChildItem -LiteralPath $Root -Recurse -Force -File | Where-Object {
        $Relative = $_.FullName.Substring($Root.Length).TrimStart('\')
        -not ($Skip | Where-Object { $Relative -eq $_ -or $Relative.StartsWith("$_\") })
    }
}

$Forbidden = @(
    @{ Name = "absolute Windows user path"; Pattern = '(?i)[A-Z]:\\Users\\' },
    @{ Name = "hard-coded UUID"; Pattern = '(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b' },
    @{ Name = "likely live API key"; Pattern = '(?im)^(MINERU_API_TOKEN(?:_[A-Z0-9]+)?|MIMO_API_KEY(?:_[0-9]+)?)[ \t]*=[ \t]*[^\s#]{12,}' }
)

foreach ($File in $Files) {
    if ($File.Extension -notin ".py", ".ps1", ".md", ".yaml", ".yml", ".toml", ".txt", ".example", "") { continue }
    $Text = Get-Content -Raw -LiteralPath $File.FullName -ErrorAction SilentlyContinue
    if ($File.Name -eq "release-audit.ps1") { continue }
    foreach ($Rule in $Forbidden) {
        if ($Text -match $Rule.Pattern) {
            $Failures.Add("$($Rule.Name): $($File.FullName.Substring($Root.Length + 1))")
        }
    }
}

$Unexpected = $Files | Where-Object {
    $_.Name -match '^(state\.db|\.env)$' -or $_.Extension -in ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".mp4", ".mkv", ".7z", ".rar"
}
foreach ($File in $Unexpected) { $Failures.Add("private/generated artifact present: $($File.FullName.Substring($Root.Length + 1))") }

$Config = Get-Content -Raw (Join-Path $Root "config.example.yaml")
foreach ($Unsafe in "delete_videos: true", "delete_archives_after_extract: true", "delete_other_source_after_markdown: true", "permanently_delete_source_after_search: true") {
    if ($Config.Contains($Unsafe)) { $Failures.Add("unsafe example default: $Unsafe") }
}

$Tokens = $null
$ParseErrors = $null
foreach ($Script in Get-ChildItem -LiteralPath $Root -Recurse -Filter *.ps1 -File) {
    [Management.Automation.Language.Parser]::ParseFile($Script.FullName, [ref]$Tokens, [ref]$ParseErrors) | Out-Null
    foreach ($ParseError in @($ParseErrors)) { $Failures.Add("PowerShell parse error in $($Script.Name): $($ParseError.Message)") }
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
Write-Host "Release audit passed: no private data, live secrets, unsafe defaults, or parse errors detected."
