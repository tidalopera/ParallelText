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
