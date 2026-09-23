# 让本机 AI agent 协助部署

这份清单供在**下载者自己的 Windows 电脑**上运行的 AI agent 使用。先读 [README](../README.md)、[Windows 首次设置](FIRST_RUN.md)；需要接 ChatGPT 时再读 [ChatGPT MCP](CHATGPT_MCP.md)。本仓库提供程序模板，下载者自行提供资料、账号、密钥、模型和域名。

## 边界

- 优先使用 [GitHub Release 的 Windows ZIP](https://github.com/xydadada/question-bank-template/releases/latest) 和根目录 `启动题库设置.cmd`。不要把 GitHub 自动生成的 Source code ZIP 当作含预编译 CLI 的安装包。
- 只处理下载者指定的安装目录和资料。先检查现有服务和端口，避免覆盖其他 WeKnora、Ollama 或 Cloudflare 配置；保留本机已有数据。
- 默认保留源文件；不要开启永久删除、人工删除同步或开机自启。不要把真实资料当成测试样本。
- 密钥、OAuth 密码和登录由下载者在本机界面输入。不要向聊天索取明文，不要输出凭据值，不要提交 `.env`、`*-keys.env`、`config.local.yaml`、资料、数据库或日志。
- WSL/Docker 安装、Cloudflare DNS 与公网开放、ChatGPT Workspace 授权可能改变电脑或外部账号状态。先向下载者说明准确目标和影响，再执行其同意的步骤；无法取得必要权限时停在该层，继续完成不依赖它的步骤。

## 执行顺序

1. **核对安装包。** 在 Release 的 Assets 中下载 Windows ZIP，完整解压到普通可写目录。确认 `启动题库设置.cmd`、`bin/weknora.exe` 和 `bin/weknora.sha256` 都存在。源码安装另见 README；没有预编译 CLI 时需要 Go 1.26+。
2. **检查环境。** 确认 Windows 11、WSL2 Ubuntu、Docker Desktop 的 WSL integration、Git、uv 和 Windows Ollama 可用。向导“环境”页的“检查当前环境”用于提示缺项；首次安装前的 `doctor.ps1` 还会因知识库尚未配置而报错，这不是安装包损坏。缺项按向导与官方链接安装，不擅自修改其他项目的网络或容器设置。
3. **启动基础服务。** 在向导点击“安装并启动基础服务”，等步骤窗口明确完成，再打开 WeKnora 页面。由下载者创建或登录自己的本地账号。不要以窗口出现、容器启动或 HTTP 200 代替实际入库验收。
4. **选择解析和模型。** 在“解析密钥”页选择所需云端服务，让下载者自己填写 Key；本地组合不需要对应云端 Key。在“模型与知识库”页选择预设或单独角色，点击“按选择下载组件”。下载耗时、占用空间及可能的云端费用由所选方案决定。配置三个知识库前必须让选定 Embedding 正常返回实际维度；已有知识库换 Embedding 时停下，按配置脚本提示处理索引迁移。
5. **导入小样本。** 通过“导入资料”页选择一份可丢弃、没有隐私内容的文件或文件夹，启动处理。确认父块、子块和原文三个知识库各自有内容，并用该资料的特征词执行真实检索。运行状态可从向导按钮、`scripts/status.ps1` 和 WeKnora 页面查看。解析失败、空检索或任一层未完成时，不宣称已可用。
6. **按需接入 ChatGPT。** 先读 [MCP 指南](CHATGPT_MCP.md)，为三个库创建限定 `retrieve` 的独立 WeKnora Key；准备下载者自己控制的 Cloudflare 域名并确认 DNS 变更。向导里可直接点开 Cloudflare Tunnel 控制台和 ChatGPT 插件页；“官方页面”页集中列出这些入口。使用向导安装组件、设置密码、创建 Tunnel、启动连接。本地运行 `mcp-public/test-local.ps1`，确认三层真实检索和只读工具范围。然后在**实际使用的 ChatGPT Workspace** 中添加 `https://<自己的主机名>/mcp`，完成 OAuth，并在新对话中实际调用一次搜索。账号邮箱相同也不能假定 Workspace 配置共享。

## 交付时报告

分别报告以下结果，附简短证据和剩余的人工步骤：

- 安装包与环境检查是否通过；
- WeKnora 是否启动、三个知识库是否创建；
- 所选解析、题图、分类和 Embedding 组件是否真正可用；
- 测试资料是否完成入库，父块、子块、原文是否都能命中；
- 若配置公网，MCP 本地与公网是否可达、OAuth 是否完成、ChatGPT 是否实际调用到检索工具。

不要把“向导能打开”“端口在监听”“模型已下载”或“健康检查为 200”写成完整成功。需要真实检索结果才能确认最后两层。
