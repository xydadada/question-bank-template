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

function New-LinkButton($Parent, [string]$Text, [string]$Url, [int]$X, [int]$Y, [int]$Width = 210) {
    $Button = New-Button $Parent $Text $X $Y $Width
    $Button.Tag = $Url
    $Button.Add_Click({
        param($Sender, $EventArgs)
        try { Open-Web ([string]$Sender.Tag) } catch { Fail($_.Exception.Message) }
    })
    return $Button
}

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
foreach ($Name in @('1 环境', '2 解析密钥', '3 模型与知识库', '4 导入资料', '5 ChatGPT连接', '官方页面')) {
    $Page = [Windows.Forms.TabPage]::new()
    $Page.Text = $Name
    $Tabs.TabPages.Add($Page)
}
$Status = [Windows.Forms.Label]::new()
$Status.Location = [Drawing.Point]::new(25, 522)
$Status.Size = [Drawing.Size]::new(755, 55)
$Status.Text = '按前五页完成首次设置；“官方页面”可随时打开下载与账号入口。每一步结果会在单独窗口中显示。'
$Form.Controls.Add($Status)

# Environment
$Page = $Tabs.TabPages[0]
New-Label $Page '首次安装需要 Windows 11、WSL2 Ubuntu、Docker Desktop、Git、uv 和 Ollama。源码包另需 Go；Windows Release 已附带 CLI。' 24 28 710 65 | Out-Null
$Check = New-Button $Page '检查当前环境' 25 105
$Check.Add_Click({ try { Launch-Script 'scripts\doctor.ps1' } catch { Fail($_.Exception.Message) } })
$Install = New-Button $Page '安装并启动基础服务' 250 105 240
$Install.Add_Click({ try { Launch-Script 'scripts\bootstrap.ps1' @('-StartWeKnora') } catch { Fail($_.Exception.Message) } })
$OpenWeKnora = New-Button $Page '打开 WeKnora 登录页' 25 166 240
$OpenWeKnora.Add_Click({ Open-Web 'http://127.0.0.1:8088' })
New-Label $Page '首次登录时在 WeKnora 创建自己的账号。安装脚本只创建本机运行环境，不带任何题库内容。' 25 220 710 60 | Out-Null
$DownloadLinks = New-Button $Page '打开官方下载入口' 25 290 235
$DownloadLinks.Add_Click({ $Tabs.SelectedIndex = 5 })

# Credentials
$Page = $Tabs.TabPages[1]
New-Label $Page '云端解析使用你自己的 MinerU 密钥；图片理解可选 MiMo 或本地 Ollama。向导把密钥保存到本机私有的 *-keys.env。' 24 25 710 65 | Out-Null
$GetMinerU = New-Button $Page '打开 MinerU 获取密钥' 25 105 260
$GetMinerU.Add_Click({ Open-Web 'https://mineru.net/apiManage/token' })
New-LinkButton $Page '打开 MiMo API 控制台' 'https://platform.xiaomimimo.com/' 300 105 260 | Out-Null
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
New-Label $Page '先选一个组合，再按需要覆盖各项。只下载实际选中的本地组件；已有资料的知识库换向量模型需重建索引。' 24 17 720 45 | Out-Null
function New-ModelChoice($Parent, [string]$Caption, [int]$Y, [string[]]$Options) {
    New-Label $Parent $Caption 25 $Y 135 28 | Out-Null
    $Control = [Windows.Forms.ComboBox]::new()
    $Control.DropDownStyle = 'DropDownList'
    $Control.Items.AddRange($Options)
    $Control.SelectedIndex = 0
    $Control.Location = [Drawing.Point]::new(160, $Y)
    $Control.Size = [Drawing.Size]::new(500, 30)
    $Parent.Controls.Add($Control)
    return $Control
}
$Preset = New-ModelChoice $Page '组合' 66 @('cloud 云端解析与题图', 'hybrid 云端解析＋本地题图', 'local-light 轻量本地', 'local-balanced 本地视觉解析', 'local-quality 高配本地')
$Parser = New-ModelChoice $Page '文档解析' 113 @('跟随组合', 'MinerU 云端', 'MinerU 本地 pipeline', 'MinerU 本地 VLM')
$Embedding = New-ModelChoice $Page '向量模型' 160 @('跟随组合', 'Qwen3 0.6B｜1024维', 'Qwen3 4B｜2560维', 'BGE-M3｜1024维', 'EmbeddingGemma｜768维', 'all-minilm｜384维')
$Vision = New-ModelChoice $Page '题图理解' 207 @('跟随组合', 'MiMo 云端', 'Qwen3.5 0.8B 本地', 'Qwen3.5 2B 本地', 'Qwen3.5 4B 本地', 'Qwen3-VL 2B 本地')
$Classification = New-ModelChoice $Page '疑难分类' 254 @('跟随组合', 'MiMo 云端', 'Qwen3 0.6B 本地', 'Qwen3 1.7B 本地', '关闭')
$SelectModels = New-Button $Page '按选择下载组件' 25 308 235
$SelectModels.Add_Click({
    try {
        $PresetIds = @('cloud', 'hybrid', 'local-light', 'local-balanced', 'local-quality')
        $ParserIds = @('', 'mineru-cloud-vlm', 'mineru-local-pipeline', 'mineru-local-vlm')
        $EmbeddingIds = @('', 'qwen3-embedding-0.6b', 'qwen3-embedding-4b', 'bge-m3', 'embeddinggemma', 'all-minilm')
        $EmbeddingDims = @(0, 1024, 2560, 1024, 768, 384)
        $VisionIds = @('', 'mimo-v2.5', 'qwen3.5-0.8b', 'qwen3.5-2b', 'qwen3.5-4b', 'qwen3-vl-2b')
        $ClassIds = @('', 'mimo-v2.5', 'qwen3-0.6b', 'qwen3-1.7b', 'disabled')
        Launch-Script 'scripts\setup-model-selection.ps1' @(
            '-Preset', $PresetIds[$Preset.SelectedIndex],
            '-Parser', $ParserIds[$Parser.SelectedIndex],
            '-Embedding', $EmbeddingIds[$Embedding.SelectedIndex],
            '-EmbeddingDimension', ([string]$EmbeddingDims[$Embedding.SelectedIndex]),
            '-Vision', $VisionIds[$Vision.SelectedIndex],
            '-Classification', $ClassIds[$Classification.SelectedIndex]
        )
    } catch { Fail($_.Exception.Message) }
})
$Configure = New-Button $Page '配置 WeKnora 三个知识库' 275 308 255
$Configure.Add_Click({ try { Launch-Script 'scripts\configure-weknora.ps1' } catch { Fail($_.Exception.Message) } })
$CustomModels = New-Button $Page '添加其他 Ollama 模型' 25 364 250
$CustomModels.Add_Click({
    $Custom = [Windows.Forms.Form]::new()
    $Custom.Text = '选择其他 Ollama 模型'
    $Custom.Size = [Drawing.Size]::new(510, 275)
    $Custom.StartPosition = 'CenterParent'
    $Custom.Font = $Form.Font
    New-Label $Custom '用途：ocr / vision / classification / embedding / chat' 15 15 465 25 | Out-Null
    $RoleBox = New-TextBox $Custom 15 45 155
    New-Label $Custom 'Ollama 模型标签，例如 vendor/model:tag' 185 47 300 25 | Out-Null
    $ModelBox = New-TextBox $Custom 185 74 285
    New-Label $Custom 'Embedding 的实际输出维度（其他用途留空）' 15 110 465 25 | Out-Null
    $DimensionBox = New-TextBox $Custom 15 138 155
    $ApplyCustom = New-Button $Custom '选择并下载' 15 180 190
    $ApplyCustom.Add_Click({
        try {
            $Dim = 0
            if ($DimensionBox.Text.Trim() -and -not [int]::TryParse($DimensionBox.Text.Trim(), [ref]$Dim)) {
                throw '请输入整数维度。'
            }
            Launch-Script 'scripts\select-custom-model.ps1' @(
                '-Role', $RoleBox.Text.Trim(), '-Model', $ModelBox.Text.Trim(),
                '-Dimension', ([string]$Dim)
            )
            $Custom.Close()
        } catch { Fail($_.Exception.Message) }
    })
    [void]$Custom.ShowDialog($Form)
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
New-LinkButton $Page '打开 Cloudflare Tunnel 控制台' 'https://dash.cloudflare.com/?to=/:account/tunnels' 260 140 315 | Out-Null
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
New-LinkButton $Page '打开 ChatGPT 插件页' 'https://chatgpt.com/plugins' 25 351 285 | Out-Null
New-LinkButton $Page '查看连接操作说明' 'https://developers.openai.com/plugins/deploy/connect-chatgpt' 325 351 265 | Out-Null
New-Label $Page '在实际使用的 Workspace 添加 https://你的主机名/mcp，选择 OAuth 并授权。' 25 399 710 36 | Out-Null

# Official download and account pages. Links may require login; they do not configure accounts.
$Page = $Tabs.TabPages[5]
New-Label $Page '这里仅打开官方页面。安装、注册、付款、授权和 DNS 修改都由使用者确认；本项目不会代替你登录。' 24 20 710 55 | Out-Null
New-LinkButton $Page 'WSL2 Ubuntu 安装说明' 'https://learn.microsoft.com/windows/wsl/install' 25 85 320 | Out-Null
New-LinkButton $Page 'Docker Desktop 下载' 'https://www.docker.com/products/docker-desktop/' 375 85 320 | Out-Null
New-LinkButton $Page 'Git for Windows 下载' 'https://git-scm.com/download/win' 25 138 320 | Out-Null
New-LinkButton $Page 'uv 安装说明' 'https://docs.astral.sh/uv/getting-started/installation/' 375 138 320 | Out-Null
New-LinkButton $Page 'Ollama 下载' 'https://ollama.com/download' 25 191 320 | Out-Null
New-LinkButton $Page '7-Zip 下载（压缩包可选）' 'https://www.7-zip.org/' 375 191 320 | Out-Null
New-LinkButton $Page 'MinerU Token' 'https://mineru.net/apiManage/token' 25 244 320 | Out-Null
New-LinkButton $Page 'MiMo API 控制台' 'https://platform.xiaomimimo.com/' 375 244 320 | Out-Null
New-LinkButton $Page 'Cloudflare Tunnel 控制台' 'https://dash.cloudflare.com/?to=/:account/tunnels' 25 297 320 | Out-Null
New-LinkButton $Page 'ChatGPT 插件页' 'https://chatgpt.com/plugins' 375 297 320 | Out-Null
New-LinkButton $Page 'Cloudflare Tunnel 官方说明' 'https://developers.cloudflare.com/tunnel/get-started/' 25 350 320 | Out-Null
New-LinkButton $Page 'ChatGPT MCP 官方说明' 'https://developers.openai.com/plugins/deploy/connect-chatgpt' 375 350 320 | Out-Null
New-LinkButton $Page 'Go 下载（仅源码包）' 'https://go.dev/dl/' 25 403 320 | Out-Null
New-LinkButton $Page '本项目 GitHub Releases' 'https://github.com/xydadada/question-bank-template/releases/latest' 375 403 320 | Out-Null

if ($SelfCheck) {
    Write-Host 'Wizard controls loaded.'
    $Form.Dispose()
    exit 0
}
[void]$Form.ShowDialog()
