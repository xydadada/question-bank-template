# Windows 首次设置

仓库和 Release 都公开；每个人部署的是自己电脑上的独立实例。推荐下载 [Windows Release](https://github.com/xydadada/question-bank-template/releases/latest) 的 Assets 中 `question-bank-template-windows-*.zip`。绿色 **Code → Download ZIP** 和 Release 页面自动生成的 **Source code (zip)** 属于源码包，没有预编译 CLI。

首次运行前的系统依赖可从官方入口安装：[WSL2 Ubuntu](https://learn.microsoft.com/windows/wsl/install)、[Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/)、[Git for Windows](https://git-scm.com/download/win)、[uv](https://docs.astral.sh/uv/getting-started/installation/) 和 [Ollama](https://ollama.com/download)。处理压缩包时再装 [7-Zip](https://www.7-zip.org/)；只有源码安装需要 [Go 1.26+](https://go.dev/dl/)。Docker Desktop 请启用 Ubuntu 的 WSL integration。下载、首次拉取容器和模型都需要网络及磁盘空间。

1. 优先从 GitHub Releases 下载包含预编译 WeKnora CLI 的 Windows ZIP 并解压到自己的普通目录，双击根目录的 `启动题库设置.cmd`。源码 ZIP 也能使用，但还需要 Go 1.26+ 编译 CLI。
2. 在“环境”页检查依赖。安装 WSL2 Ubuntu、Docker Desktop、Git、uv 和 Ollama 后，点击“安装并启动基础服务”。这是首次部署，可能需要较长时间下载 WeKnora 和依赖。打开 WeKnora 页面，创建本地账号。缺少下载地址时，点“打开官方下载入口”，向导会切到“官方页面”页。
3. 计划使用 MinerU 云端解析时，在“解析密钥”页点击 MinerU 按钮，登录官方页面获取自己的 Token。选 MinerU、编号 1，粘贴并保存。需要补充密钥时选更高编号。选择 MiMo 题图或分类模型时，同页保存自己的 MiMo Key。本地解析组合无需 MinerU Token。
4. 在“模型与知识库”页先选一套组合，再按需要单独选择解析器、Embedding、题图和分类模型。点击“按选择下载组件”；完成后点击“配置 WeKnora 三个知识库”。后一步会在官方 WeKnora CLI 中要求登录，并检查 Embedding 的实际输出维度。其他 Ollama 标签可通过本页的“添加其他 Ollama 模型”按钮选择。
5. 在“导入资料”页选择文件、压缩包或文件夹；每个选择都会打开复制窗口。复制完成后点击“开始处理并入库”。先用一份可丢弃的小资料，待处理完在 WeKnora 里检查内容，并运行一次真实检索。
6. 如需在 ChatGPT 使用：在 WeKnora 中为这三个知识库创建限定读取权限的专用 Key；在“ChatGPT连接”页安装组件、设置专用 Key 和 OAuth 密码。可点“打开 Cloudflare Tunnel 控制台”进入自己的账号。填入自己托管在 Cloudflare 的主机名，创建 Tunnel 与 DNS 路由，然后启动连接。用本地检测脚本确认三层检索；再点“打开 ChatGPT 插件页”，在实际使用的 Workspace 添加 `https://<你的主机名>/mcp` 并完成 OAuth。具体权限和工具面见 [ChatGPT MCP](CHATGPT_MCP.md)。

“检查当前环境”和“查看运行状态”会显示当前缺失项。每个安装或处理按钮打开一个步骤窗口；窗口内的成功或错误结果是该步骤的完成依据。向导把 MinerU 和 MiMo 密钥分别放在 Git 忽略的本地文件中；运行中的处理进程可读取新增密钥。已经导入的文件默认保留。所有账号、密钥、模型、域名、知识库及处理资料都由下载者自己提供。

向导目前是 Windows PowerShell 5.1 界面。源码安装路径保留 Go 编译能力；发布包附带与固定 WeKnora 版本一致的 CLI 和 SHA-256 清单，引导时会先验证其完整性。

如果需要本机 AI agent 代为执行，直接把 [AI 辅助部署说明](DEPLOY_WITH_AI.md) 交给它。它可以检查环境、安装程序、配置模型并做真实检索；账号登录、密钥输入、DNS 变更和 ChatGPT Workspace 授权仍需由实际拥有者完成或批准。只要本地三层检索通过，就可以先使用题库；公网 MCP 是后续可选步骤。
