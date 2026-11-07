# ParallelText

术语表驱动的中文长文本英译工具，附带本地双语听读器。

支持“扫描术语 → 统一译法 → 分批翻译 → 检查与审校 → 导出”，每个非空原文行和英文译文分别保存。分批结果可以续跑；审校建议需要明确应用。听读器支持中英对照、搜索、逐词高亮、连续朗读及跨段前后跳转，省略号等纯符号段会自动跳过，正文仍保留。

## 先试读示例

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，在项目目录执行：

```sh
uv sync --locked
uv run python Reader.py
```

自己打开 <http://127.0.0.1:8766>。内置示例是本项目原创内容，不需要模型凭据。Windows 可双击 `StartReader.vbs` 隐藏窗口启动示例服务，随后手动打开网址。

页面和搜索在本地运行；在线语音会将选中的英文段落发给 `edge-tts` 使用的服务。模型翻译也会向你配置的接口发送原文。

存在 `Work/Book/Aligned.json` 时，阅读器和 Windows 启动器默认在 8765 端口加载这份私有书库；否则在 8766 端口加载示例。命令行可用 `--port` 调整。

在线声音提前准备后面 6 个可朗读段落，后台与当前段使用独立线程。音频落盘保存在 `Work/Speech/`，重启后复用，默认按 1GB 上限滚动淘汰。

可选的 Furina 本机声音复用已有模型服务，不安装模型依赖。它不提供逐词时间戳，原在线语音路径保留。配置 `furinaUrl`、`defaultVoice: "furina-local-en"`、`startChapter` 和 `prepareChapter` 后，后台从指定章持续准备，直到缓存接近 1GB 或全文末尾。

可用内存达到0.5GB就继续生成，CPU/GPU使用率仅供显示。配置本地可信的 `furinaStartScript`、`furinaStopScript` 和 `furinaPidFile` 后，低于0.5GB时会退出模型服务、释放其进程内存；磁盘音频保留。恢复后重新加载并重试未完成的小段。没有启停脚本配置时，只能等待内存恢复，不能卸载外部服务。

重载时会预留此前观测到的模型分配量和0.5GB余量，并设置30秒冷却，避免卸载腾出内存后立刻重新装满。状态保存在 `Work/Speech/ModelState.json`，侧边栏和 `/api/cache` 可以查看。关闭阅读器会停止后台监管和预生成。Windows GPU计数器缺失时显示null。本机模型仍可能慢于实时，预生成只是提前完成计算。

声音选择器提供随项目附带的全部 Edge 云端声音，可按语言筛选，或搜索名称、口音和性别；筛选不会改变当前声音或触发生成。选择中文等其他语言的音色仍然朗读英文文本，不会翻译内容。启动优先读取本机目录，无需等待联网；执行 `uv run python Voices.py --refresh` 可更新私有的 `Work/Voices.json`，重启服务后生效。文件缺失或损坏时使用项目附带的 `Voices.json`。目录中列出的声音仍可能因服务端变动或网络问题而暂时不可用。

## 翻译自己的文本

1. 将 `Examples/Settings.json` 复制为 `Config.json`，填写自己有权限使用的翻译模型和分析模型。默认不发送推理强度参数；模型支持时才设置 `reasoningEffort`。
2. 在环境变量中设置 `OPENAI_API_KEY`。可选 `OPENAI_BASE_URL` 必须支持流式 Responses API 和严格 JSON Schema，只有 Chat Completions 的接口不能直接使用。
3. 按顺序执行：

```sh
uv run python Pipeline.py prepare --source Examples/Source.txt --work Work/Book
uv run python Pipeline.py scan --work Work/Book
uv run python Pipeline.py unify --work Work/Book
```

先检查术语表。翻译实际读取 `Work/Book/Glossary.json`，修改译法请编辑这个 JSON。

```sh
uv run python Pipeline.py translate --work Work/Book --workers 4
uv run python Quality.py check --work Work/Book --all
uv run python Quality.py review --work Work/Book --workers 4
```

检查 `Work/Book/Reviewproposals.json` 后，用 `Reviewselection.json` 记录要拒绝的建议编号，用可选的 `Reviewoverrides.json` 保存人工修订。格式见[英文说明](README.md)。审校不会自动应用建议。

```sh
uv run python Quality.py apply --work Work/Book
uv run python Pipeline.py assemble --work Work/Book
uv run python Reader.py --book Work/Book
```

不带 `--all` 时，复核范围为全部规则风险段、固定随机抽样和首尾段；带 `--all` 时逐段送模型复核。检查通过不代表每句话都翻译正确，模型提出的修改也可能有错。

## 数据与边界

- 将自己的输入、翻译、缓存放在 `Work/`，该目录和本地 `Config.json` 已被 Git 忽略。
- 输出为 `Bilingual.txt`、`English.txt` 和 `Aligned.json`。原始文件只读；非空行首尾空白会被去掉，不还原原始空行排版。
- 每本书使用独立工作目录。不同原文或分批布局不能覆盖同一目录；初译变化后，旧审校修订会被判为过期。
- 默认先严格尝试 UTF-8，再尝试 GB18030；有歧义时使用 `--encoding` 指定，并人工检查原文快照。
- 可用 `--model`、`--analysis-model`、`--config` 调整设置。也可通过 `PARALLELTEXT_AUTH_MODULE` 使用自己信任、提供 `access_token()` 的本地 Python 模块；它不会随项目上传。
- 阅读器默认只监听本机 8766 端口。换书前关闭原服务，或用 `--port` 换端口。它不适合直接部署到公网。
- 软件和原创示例使用 MIT 许可证，不包含第三方正文、私人术语或认证文件。使用软件不等于获得其他作品的翻译或传播授权。

回归检查与更完整的参数说明见 [README](README.md)。
