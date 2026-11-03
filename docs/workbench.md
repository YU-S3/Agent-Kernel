# AgentLab 本地工作台

本轮在独立 PluginLab 上修改。原实验与 SkillCompiler 目录不受影响。

## 手动接入 DeepSeek

1. 启动 `bash scripts/start-visual.sh`，打开 http://127.0.0.1:8765/。
2. 点击「模型设置」：API 地址 `https://api.deepseek.com`，模型 `deepseek-flash`（也可填账号支持的其他模型）。填写自己的 API Key。
3. 思考模式和推理强度按需要选；默认 enabled / high。最大输出 tokens 可留空，它仅约束一次生成，不是会话总预算。
4. 保存不会发出模型调用。关闭弹窗，选择「真实模型 · 日常对话」，输入任务，点击「开始新对话」才调用 API。
5. 下一条消息写入任务框后点击「继续此会话」。新对话使用最新模型配置，已有会话保持创建时的配置。

另一种方式：复制项目 `.env.example` 为 `.env`，填写 `DEEPSEEK_API_KEY=你的密钥`。普通 Key 不需要引号，也支持成对的单/双引号。保存后再打开模型设置即可刷新识别，不需要将 Key 写入 YAML。UI 保存的 endpoint 专属 Key 优先于环境变量及 `.env`。

本机 UI 保存到 WSL `~/.agentlab/model.json` / `credentials.json`；后者权限 0600。密钥不会进入浏览器返回值、Composition、事件或请求审计头。请求记录包含实际消息，属于本地私有运行数据。

## 页面怎么用

- **运行模式**：真实模型日常对话，或明确标为离线的功能示例；离线示例不理解任意自然语言任务。
- **工具与运行设置**：选择文件读取、容器 Python/Shell、子代理。工作目录使用 WSL 路径，例如 `/mnt/d/my-project`；留空创建会话专属空目录。
- **依赖图**：配置中实际存在的插件实例和绑定方向。切换工具即更新。
- **实际调用图**：此次执行发生过的调用关系；运行中的调用和最近事件显示流动箭头。
- **归属图**：Composition、实例和执行作用域的生命周期关系。
- **历史会话 / 查看最新状态**：查看磁盘保留的运行。已结束会话不会显示成仍在运行。
- **播放执行过程**：只读回放真实事件；播放、单步、暂停不会重新调用模型。
- **适配全图**：显示全部节点。可缩放、平移和拖动节点；手动视角不会被轮询覆盖。

## 预算和长会话

模型连接保存 endpoint、模型、认证、思考参数、超时及可选的单响应输出上限。累计 token 不再放在 Provider 配置中，默认仅统计实际 usage。请求数上限和单轮步骤上限位于运行策略中；默认步骤上限 32、请求数不封顶。单次运行仍有一小时生命周期限制，API 请求默认超时 600 秒。

DSH 也区分模型上下文容量、输出上限与运行策略。其 compaction 插件在上下文压力下摘要/裁剪，以继续长会话，不代表一次请求或会话上下文无限。目前本项目实现的是最近完整轮次投影（默认 20 轮 / 200,000 字符防护），不是 DSH 的 LLM 语义摘要。原始历史不删除。特别长的单轮会显式报上下文错误。

实际 HTTP 返回的 raw_usage 与每次请求时间保存于会话 `requests/`；累计已知 tokens、请求数和未知用量在 `usage.json`。中断、未知费用不会伪造成零。无自动重复付费调用或重放工具副作用。重启后用户选择会话并继续；中断工具结果标为未知，保留原记录。

Provider 实例复用连接池和 5 分钟 DNS 缓存。临时 DNS 解析失败最多重试两次，仍受原请求超时约束；这时尚未发送 HTTP，不产生模型调用。连续失败会显示具体 DNS 错误并记录零已发送请求，不伪造 raw_usage。收到响应后断流、未知传输错误和 API 拒绝不会自动重发。旧记录中只有 `TRANSPORT_UNKNOWN` 而没有具体原因的，继续保留未知，不回填猜测值。

## 实现边界与验证

真实传输层的 SSE 正文/推理、分片 tool_calls、工具回传、续聊、usage、服务重启、截断和网络异常用本地真实 HTTP 服务验证。离线回归不等于验证用户的商业 API Key、账户余额或指定模型在线可用性。接入用户自己的 Key 后首次对话才会产生商业费用。

当前通用传输实现 Chat Completions 文本与工具协议；其他供应商特有参数应注册对应 Provider 插件。当前文件工具只读；Shell/Python 网络禁用、工作目录只读，产物写 `/output`。不是开放网络的全权限编码代理。

## 官方依据

- [DSH 模型配置](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md)
- [DSH compaction](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/compaction.md)
- [DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- [DeepSeek 思考模式与工具历史](https://api-docs.deepseek.com/guides/thinking_mode/)
