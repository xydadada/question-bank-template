[CmdletBinding()]
param([switch]$SelfCheck)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$Root = Split-Path -Parent $PSScriptRoot
$Utf8 = [Text.UTF8Encoding]::new($false)

function Ensure-LocalFiles {
    foreach ($Pair in @(
        @('.env.example', '.env'),
        @('config.example.yaml', 'config.local.yaml')
    )) {
        $Target = Join-Path $Root $Pair[1]
        if (-not (Test-Path -LiteralPath $Target)) {
            Copy-Item -LiteralPath (Join-Path $Root $Pair[0]) -Destination $Target
        }
    }
}

function Protect-EnvFile([string]$Path) {
    $Sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    & "$env:WINDIR\System32\icacls.exe" $Path /inheritance:r `
        /grant:r "*${Sid}:F" '*S-1-5-18:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not restrict access to the key file.' }
}

function Save-Key([string]$Name, [string]$Value) {
    if ($Value -notmatch '^[A-Za-z0-9._-]{8,512}$') {
        throw 'The key must be 8–512 letters, digits, dots, underscores or hyphens.'
    }
    Ensure-LocalFiles
    $KeyFile = if ($Name.StartsWith('MINERU_')) { 'mineru-keys.env' } else { 'mimo-keys.env' }
    $Path = Join-Path $Root $KeyFile
    if (-not (Test-Path -LiteralPath $Path)) { [IO.File]::WriteAllText($Path, '', $Utf8) }
    Protect-EnvFile $Path
    $Text = [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8)
    $Pattern = '(?m)^' + [regex]::Escape($Name) + '=.*$'
    $Line = "$Name=$Value"
    if ([regex]::IsMatch($Text, $Pattern)) {
        $Text = ([regex]::new($Pattern)).Replace($Text,
            [Text.RegularExpressions.MatchEvaluator]{ param($Match) $Line }, 1)
    } else {
        if ($Text -and -not $Text.EndsWith("`n")) { $Text += "`r`n" }
        $Text += "$Line`r`n"
    }
    [IO.File]::WriteAllText($Path, $Text, $Utf8)
    Protect-EnvFile $Path
}

function New-Label($Parent, [string]$Text, [int]$X, [int]$Y, [int]$Width = 730, [int]$Height = 38) {
    $Control = [Windows.Forms.Label]::new()
    $Control.Text = $Text
    $Control.Location = [Drawing.Point]::new($X, $Y)
    $Control.Size = [Drawing.Size]::new($Width, $Height)
    $Parent.Controls.Add($Control)
    return $Control
}

function New-Button($Parent, [string]$Text, [int]$X, [int]$Y, [int]$Width = 210) {
    $Control = [Windows.Forms.Button]::new()
    $Control.Text = $Text
    $Control.Location = [Drawing.Point]::new($X, $Y)
    $Control.Size = [Drawing.Size]::new($Width, 38)
    $Parent.Controls.Add($Control)
    return $Control
}

function New-TextBox($Parent, [int]$X, [int]$Y, [int]$Width = 420, [bool]$Secret = $false) {
    $Control = [Windows.Forms.TextBox]::new()
    $Control.Location = [Drawing.Point]::new($X, $Y)
    $Control.Size = [Drawing.Size]::new($Width, 27)
    $Control.UseSystemPasswordChar = $Secret
    $Parent.Controls.Add($Control)
    return $Control
}

function Note([string]$Message) {
    $Status.Text = $Message
}

function Fail([string]$Message) {
    [Windows.Forms.MessageBox]::Show($Message, '题库设置', 'OK', 'Warning') | Out-Null
    Note($Message)
}

function Launch-Script([string]$RelativePath, [string[]]$Arguments = @()) {
    $Path = Join-Path $Root $RelativePath
    if (-not (Test-Path -LiteralPath $Path)) { throw "Missing script: $RelativePath" }
    $Quoted = @('"' + $Path + '"')
    foreach ($Argument in $Arguments) {
        if ($Argument.Contains('"')) { throw 'Invalid argument.' }
        $Quoted += '"' + $Argument + '"'
    }
    $CommandLine = '-NoProfile -NoExit -ExecutionPolicy Bypass -File ' + ($Quoted -join ' ')
    Start-Process -FilePath "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList $CommandLine -WorkingDirectory $Root | Out-Null
    Note("已打开步骤窗口：$RelativePath。完成后返回这里继续。")
}

function Open-Web([string]$Url) { Start-Process $Url | Out-Null }

$Form = [Windows.Forms.Form]::new()
$Form.Text = '题库首次设置与运行'
$Form.Size = [Drawing.Size]::new(820, 630)
$Form.StartPosition = 'CenterScreen'
$Form.MinimumSize = [Drawing.Size]::new(820, 630)
$Form.Font = [Drawing.Font]::new('Microsoft YaHei UI', 10)

$Tabs = [Windows.Forms.TabControl]::new()
$Tabs.Location = [Drawing.Point]::new(18, 18)
$Tabs.Size = [Drawing.Size]::new(770, 490)
$Form.Controls.Add($Tabs)
foreach ($Name in @('1 环境', '2 解析密钥', '3 模型与知识库', '4 导入资料', '5 ChatGPT连接')) {
    $Page = [Windows.Forms.TabPage]::new()
    $Page.Text = $Name
    $Tabs.TabPages.Add($Page)
}
$Status = [Windows.Forms.Label]::new()
$Status.Location = [Drawing.Point]::new(25, 522)
$Status.Size = [Drawing.Size]::new(755, 55)
$Status.Text = '从左到右完成首次设置。每一步的执行结果会在单独窗口中显示。'
$Form.Controls.Add($Status)

# Environment
$Page = $Tabs.TabPages[0]
New-Label $Page '首次安装需要 Windows 11、WSL2 Ubuntu、Docker Desktop、Git、Go、uv 和 Ollama。安装脚本会检查这些依赖。' 24 28 710 65 | Out-Null
$Check = New-Button $Page '检查当前环境' 25 105
$Check.Add_Click({ try { Launch-Script 'scripts\doctor.ps1' } catch { Fail($_.Exception.Message) } })
$Install = New-Button $Page '安装并启动基础服务' 250 105 240
$Install.Add_Click({ try { Launch-Script 'scripts\bootstrap.ps1' @('-StartWeKnora') } catch { Fail($_.Exception.Message) } })
$OpenWeKnora = New-Button $Page '打开 WeKnora 登录页' 25 166 240
$OpenWeKnora.Add_Click({ Open-Web 'http://127.0.0.1:8088' })
New-Label $Page '首次登录时在 WeKnora 创建自己的账号。安装脚本只创建本机运行环境，不带任何题库内容。' 25 220 710 60 | Out-Null

# Credentials
$Page = $Tabs.TabPages[1]
New-Label $Page 'PDF 等文档解析使用你自己的 MinerU 密钥。图片理解可选 MiMo 云端或本地 Ollama。密钥只保存在本机 .env。' 24 25 710 65 | Out-Null
$GetMinerU = New-Button $Page '打开 MinerU 获取密钥' 25 105 260
$GetMinerU.Add_Click({ Open-Web 'https://mineru.net/apiManage/token' })
New-Label $Page '服务' 25 172 80 27 | Out-Null
$KeyProvider = [Windows.Forms.ComboBox]::new()
$KeyProvider.DropDownStyle = 'DropDownList'
$KeyProvider.Items.AddRange(@('MinerU', 'MiMo'))
$KeyProvider.SelectedIndex = 0
$KeyProvider.Location = [Drawing.Point]::new(85, 170)
$KeyProvider.Size = [Drawing.Size]::new(125, 30)
$Page.Controls.Add($KeyProvider)
New-Label $Page '第几个密钥' 240 172 105 27 | Out-Null
$KeySlot = [Windows.Forms.NumericUpDown]::new()
$KeySlot.Minimum = 1
$KeySlot.Maximum = 32
$KeySlot.Value = 1
$KeySlot.Location = [Drawing.Point]::new(350, 170)
$KeySlot.Size = [Drawing.Size]::new(75, 30)
$Page.Controls.Add($KeySlot)
New-Label $Page '密钥' 25 225 70 27 | Out-Null
$KeyValue = New-TextBox $Page 85 223 500 $true
$SaveKey = New-Button $Page '保存这个密钥' 25 275
$SaveKey.Add_Click({
    try {
        $Slot = [int]$KeySlot.Value
        if ($KeyProvider.SelectedItem -eq 'MinerU') {
            $Name = if ($Slot -eq 1) { 'MINERU_API_TOKEN' } elseif ($Slot -eq 2) { 'MINERU_API_TOKEN_BACKUP' } else { "MINERU_API_TOKEN_$Slot" }
        } else {
            $Name = if ($Slot -eq 1) { 'MIMO_API_KEY' } else { "MIMO_API_KEY_$Slot" }
        }
        Save-Key $Name $KeyValue.Text
        $KeyValue.Clear()
        Note("已保存 $($KeyProvider.SelectedItem) 第 $Slot 个密钥。")
    } catch { $KeyValue.Clear(); Fail('保存失败：' + $_.Exception.Message) }
})
New-Label $Page '以后增加密钥，选择下一个编号即可。界面不会重新显示已经保存的密钥。' 25 334 710 45 | Out-Null

# Models
$Page = $Tabs.TabPages[2]
New-Label $Page '先选择向量模型。程序会按需下载、测量实际向量维度，再创建三个知识库。已有资料的知识库不能直接换向量模型。' 24 24 720 60 | Out-Null
$Embedding = [Windows.Forms.ComboBox]::new()
$Embedding.DropDownStyle = 'DropDownList'
$Embedding.Items.AddRange(@('qwen3-embedding:0.6b  (1024维)', 'bge-m3  (1024维)', 'nomic-embed-text  (768维)'))
$Embedding.SelectedIndex = 0
$Embedding.Location = [Drawing.Point]::new(25, 100)
$Embedding.Size = [Drawing.Size]::new(420, 30)
$Page.Controls.Add($Embedding)
$Configure = New-Button $Page '下载并配置向量模型与知识库' 25 145 330
$Configure.Add_Click({
    try {
        $Models = @(@('qwen3-embedding:0.6b', '1024'), @('bge-m3', '1024'), @('nomic-embed-text', '768'))
        $Selected = $Models[$Embedding.SelectedIndex]
        Launch-Script 'scripts\configure-weknora.ps1' @('-EmbeddingModel', $Selected[0], '-EmbeddingDimension', $Selected[1])
    } catch { Fail($_.Exception.Message) }
})
New-Label $Page '题图理解' 25 215 120 27 | Out-Null
$Vision = [Windows.Forms.ComboBox]::new()
$Vision.DropDownStyle = 'DropDownList'
$Vision.Items.AddRange(@('MiMo 云端（需密钥）', 'Ollama 本地 qwen3.5:0.8b', 'Ollama 本地 qwen3.5:2b'))
$Vision.SelectedIndex = 0
$Vision.Location = [Drawing.Point]::new(25, 251)
$Vision.Size = [Drawing.Size]::new(420, 30)
$Page.Controls.Add($Vision)
$SetVision = New-Button $Page '应用题图模型选择' 25 298 250
$SetVision.Add_Click({
    try {
        if ($Vision.SelectedIndex -eq 0) {
            Launch-Script 'scripts\set-vision.ps1' @('-Provider', 'mimo')
        } else {
            $Name = if ($Vision.SelectedIndex -eq 1) { 'qwen3.5:0.8b' } else { 'qwen3.5:2b' }
            Launch-Script 'scripts\set-vision.ps1' @('-Provider', 'ollama', '-VisionModel', $Name)
        }
    } catch { Fail($_.Exception.Message) }
})

# Import
$Page = $Tabs.TabPages[3]
New-Label $Page '选择资料文件或文件夹，先放入本机 inbox，再启动处理。默认保留原始资料；处理状态可在状态窗口查看。' 24 25 710 65 | Out-Null
$SelectFiles = New-Button $Page '选择文件或压缩包' 25 110 245
$SelectFiles.Add_Click({
    $Dialog = [Windows.Forms.OpenFileDialog]::new()
    $Dialog.Multiselect = $true
    $Dialog.Filter = 'All files (*.*)|*.*'
    if ($Dialog.ShowDialog() -eq 'OK') {
        foreach ($SelectedFile in $Dialog.FileNames) {
            try { Launch-Script 'scripts\stage-input.ps1' @('-SourcePath', $SelectedFile) }
            catch { Fail($_.Exception.Message) }
        }
    }
})
$SelectFolder = New-Button $Page '选择资料文件夹' 285 110 210
$SelectFolder.Add_Click({
    $Dialog = [Windows.Forms.FolderBrowserDialog]::new()
    if ($Dialog.ShowDialog() -eq 'OK') {
        try { Launch-Script 'scripts\stage-input.ps1' @('-SourcePath', $Dialog.SelectedPath) }
        catch { Fail($_.Exception.Message) }
    }
})
$OpenInbox = New-Button $Page '打开待处理目录' 25 170 210
$OpenInbox.Add_Click({
    $Inbox = Join-Path $Root 'inbox'
    New-Item -ItemType Directory -Force -Path $Inbox | Out-Null
    Start-Process explorer.exe -ArgumentList ('"' + $Inbox + '"') | Out-Null
})
$StartProcessing = New-Button $Page '开始处理并入库' 25 244 235
$StartProcessing.Add_Click({ try { Launch-Script 'scripts\start.ps1' @('-Processing') } catch { Fail($_.Exception.Message) } })
$StatusButton = New-Button $Page '查看运行状态' 280 244 205
$StatusButton.Add_Click({ try { Launch-Script 'scripts\status.ps1' } catch { Fail($_.Exception.Message) } })
New-Label $Page '完成第一份资料后，在 WeKnora 中查看三层知识库，并用资料中的关键词检索一次。' 25 325 700 45 | Out-Null

# ChatGPT connection
$Page = $Tabs.TabPages[4]
New-Label $Page '此步骤需要你自己的 Cloudflare 域名与 ChatGPT Workspace。先让三个知识库都有可检索内容。' 24 20 710 55 | Out-Null
$McpTools = New-Button $Page '安装 MCP 与 Tunnel 组件' 25 84 275
$McpTools.Add_Click({ try { Launch-Script 'scripts\bootstrap.ps1' @('-InstallMcpTools') } catch { Fail($_.Exception.Message) } })
$ReadOnly = New-Button $Page '设置只读检索密钥' 315 84 240
$ReadOnly.Add_Click({ try { Launch-Script 'mcp-public\configure-readonly-profile.ps1' } catch { Fail($_.Exception.Message) } })
$Password = New-Button $Page '设置连接密码' 25 140 220
$Password.Add_Click({ try { Launch-Script 'mcp-public\set-password.ps1' } catch { Fail($_.Exception.Message) } })
New-Label $Page 'Cloudflare 主机名，例如 mcp.example.com' 25 206 470 28 | Out-Null
$Hostname = New-TextBox $Page 25 239 420
$SetupTunnel = New-Button $Page '创建并连接 Tunnel' 25 287 245
$SetupTunnel.Add_Click({
    try {
        $HostNameValue = $Hostname.Text.Trim().ToLowerInvariant()
        if ($HostNameValue -notmatch '^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$' -or $HostNameValue -match '\.\.') {
            throw 'Please enter a valid Cloudflare hostname.'
        }
        Launch-Script 'mcp-public\setup-cloudflare.ps1' @('-Hostname', $HostNameValue, '-CreateDnsRoute')
    } catch { Fail($_.Exception.Message) }
})
$StartMcp = New-Button $Page '启动连接' 285 287 175
$StartMcp.Add_Click({
    try {
        $HostNameValue = $Hostname.Text.Trim().ToLowerInvariant()
        if ($HostNameValue -notmatch '^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$' -or $HostNameValue -match '\.\.') {
            throw 'Please enter your configured hostname.'
        }
        Launch-Script 'mcp-public\start-all.ps1' @('-ExternalUrl', "https://$HostNameValue")
    } catch { Fail($_.Exception.Message) }
})
$TestMcp = New-Button $Page '检测本地 MCP' 475 287 210
$TestMcp.Add_Click({ try { Launch-Script 'mcp-public\test-local.ps1' } catch { Fail($_.Exception.Message) } })
$ChatGpt = New-Button $Page '打开 ChatGPT' 25 351 285
$ChatGpt.Add_Click({ Open-Web 'https://chatgpt.com/' })
New-Label $Page '在实际使用的 Workspace 添加 https://你的主机名/mcp，选择 OAuth 并授权。' 25 399 710 36 | Out-Null

if ($SelfCheck) {
    Write-Host 'Wizard controls loaded.'
    $Form.Dispose()
    exit 0
}
[void]$Form.ShowDialog()
