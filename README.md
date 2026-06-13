# weixin-multi-ilink

Hermes Agent 网关插件：通过 iLink Bot API 同时接入多个微信账号。

基于 Hermes 内置 weixin 适配器的核心逻辑，支持动态注册多个 iLink 实例，每个实例独立运行、独立配置。

## 功能

- 同时运行多个 iLink 微信实例
- 每个实例独立的 DM 策略、授权配置、cron 投递
- 扫码配置（QR login）
- 完整的媒体支持（图片、视频、文件、语音）
- AES-128-ECB 加密 CDN 传输
- context_token 持久化（跨重启保持会话）
- 消息去重、文本批处理
- Typing 指示器

## 安装

```bash
# 克隆到插件目录
git clone https://github.com/chiibot/hermes-weixin-multi-ilink.git ~/.hermes/plugins/weixin-multi-ilink

# 安装依赖（如果还没有）
pip install aiohttp cryptography
```

在 `~/.hermes/config.yaml` 中启用插件：

```yaml
plugins:
  enabled:
    - onebot11-platform
    - weixin-multi-ilink    # 添加这一行
```

## 添加实例

```bash
cd ~/.hermes/plugins/weixin-multi-ilink
python3 setup_instance.py add <实例名>
```

例如：

```bash
python3 setup_instance.py add main     # 添加主号
python3 setup_instance.py add work     # 添加工作号
python3 setup_instance.py add rong     # 添加另一个号
```

流程：
1. 终端显示二维码
2. 用微信扫描确认
3. 选择 DM 策略（pairing / open / allowlist / disabled）
4. 自动写入 config.yaml
5. 重启 gateway：`hermes gateway restart`

## 查看已配置实例

```bash
python3 setup_instance.py list
```

## 移除实例

```bash
python3 setup_instance.py remove <实例名>
```

## config.yaml 格式

每个实例对应一个 `weixin-<名称>` 条目：

```yaml
platforms:
  weixin-main:
    enabled: true
    extra:
      account_id: "abc123@im.bot"
      token: "abc123@im.bot:060000..."
      base_url: "https://ilinkai.weixin.qq.com"
      cdn_base_url: "https://novac2c.cdn.weixin.qq.com/c2c"
      dm_policy: "pairing"
      group_policy: "disabled"

  weixin-work:
    enabled: true
    extra:
      account_id: "def456@im.bot"
      token: "def456@im.bot:060000..."
      dm_policy: "open"
```

### DM 策略

| 值 | 行为 |
|---|------|
| `open` | 所有人可私聊 |
| `pairing` | 配对模式（推荐） |
| `allowlist` | 仅白名单用户 |
| `disabled` | 禁用私聊 |

### 环境变量覆盖

每个实例支持环境变量覆盖（`<NAME>` 是大写的实例名后缀）：

```bash
WEIXIN_<NAME>_TOKEN=xxx
WEIXIN_<NAME>_ACCOUNT_ID=xxx
WEIXIN_<NAME>_ALLOWED_USERS=user1,user2
WEIXIN_<NAME>_ALLOW_ALL_USERS=true
WEIXIN_<NAME>_HOME_CHANNEL=user_id@im.wechat
```

## 架构

```
~/.hermes/plugins/weixin-multi-ilink/
├── plugin.yaml        # 插件元数据
├── __init__.py        # 入口：register(ctx)
├── adapter.py         # WeixinMultiAdapter + 动态注册
├── ilink_api.py       # iLink Bot API 客户端
├── crypto.py          # AES-128-ECB 加密
├── token_store.py     # context_token 持久化
└── setup_instance.py  # 交互式配置脚本
```

### 工作原理

1. `register(ctx)` 读取 config.yaml，找到所有 `weixin-*` 条目
2. 为每个条目调用 `ctx.register_platform()` 注册为独立平台
3. Gateway 为每个平台创建独立的 adapter 实例
4. 每个 adapter 独立运行长轮询、消息处理、响应发送

### 与内置 weixin 适配器的关系

本插件从 `gateway/platforms/weixin.py` 提取核心逻辑，但完全独立运行。如果同时启用了内置 weixin 适配器和本插件的 `weixin-*` 实例，两者互不干扰（不同的 Platform 枚举值）。

如果只想用本插件，需要禁用内置 weixin 适配器（清空 `.env` 中的 `WEIXIN_ACCOUNT_ID` 和 `WEIXIN_TOKEN`）。

## 依赖

- Python 3.10+
- aiohttp
- cryptography
- Hermes Agent（网关 + 插件系统）

## 许可

MIT
