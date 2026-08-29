# Auto Hax

自动续期 hax VPS 订阅的工具，通过浏览器自动化完成登录、验证、续期全流程，并支持 Telegram 通知。

## 工作原理

1. 使用 DrissionPage ChromiumPage 打开 hax.co.id 面板
2. 通过 Telegram Widget 实现登录（Telethon 点击 Confirm 授权）
3. 自动处理 Cloudflare Turnstile 挑战
4. 枚举账户下所有 VPS，逐一执行续期
5. 自动识别并解决算术验证码（ddddocr）和 reCAPTCHA（语音识别）
6. 通过 Telegram Bot 推送续期结果、VPS 信息及截图

## 快速开始

### 环境要求

- Python 3.13+
- Linux 系统（需安装 Xvfb 用于虚拟显示）
- Chrome / Chromium 浏览器
- ffmpeg（reCAPTCHA 语音解码需要）

### 安装依赖

```bash
# 系统依赖
sudo apt-get update && sudo apt-get install -y xvfb  ffmpeg

# Python 依赖（版本已固定）
pip install -r requirements.txt
```

### 配置

通过 `BATCH` 环境变量配置，多个账号用分号 `;` 分隔。

#### BATCH 格式

```
phone,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `phone` | ✅ | 手机号，格式 `+国家码-号码`，如 `+506-60012545` |
| `tg_bot_token` | ✅ | 用于推送通知的 Telegram Bot Token |
| `tg_chat_id` | ✅ | 接收通知的 Telegram Chat ID |
| `tg_api_id` | ✅ | 该账号对应的 Telegram API ID（ https://my.telegram.org ） |
| `tg_api_hash` | ✅ | 该账号对应的 Telegram API Hash |
| `tg_session` | ✅ | 该账号的 Telegram StringSession（运行 `setup_tg_session.py` 生成） |

#### 示例

单账号：
```
+506-60012545,7126463574:AAH...,453472000,34604959,96c51d9e7a31...,1AZWar...
```

多账号（分号分隔）：
```
+506-60012545,7126463574:AAH...,453472000,34604959,96c51d9e7a31...,1AZWar...;+503-72796921,7200000001:AAF...,+81-0000001,abc123def456...,2BVta...
```

#### 其他环境变量

| 变量 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `PROXY_URL` | ❌ | - | 代理 URL（支持 vless/vmess/trojan/ss/hy2/tuic/anytls/socks5） |
| `SOCKS_PORT` | ❌ | `10808` | 本地 SOCKS 代理端口 |
| `SCREENSHOT_DIR` | ❌ | `output/screenshots` | 截图保存目录 |
| `DEBUG_FLAG` | ❌ | `0` | 设为 `1` 输出调试日志 |

### 获取 Telegram API ID 和 API Hash

1. 访问 https://my.telegram.org ，用你的手机号登录（会收到 Telegram 验证码）
2. 点击 **API development tools**
3. 填写表单（App title / Short name 随便填，如 `haxauto`）
4. 提交后页面显示 **App api_id** 和 **App api_hash**

> ⚠️ api_id 和 api_hash 是固定的，创建一次后不要重复创建，否则旧的会失效。

## Telegram Session 生成

每个 TG 账号需要独立的 session string。通过 `setup_tg_session.py` 生成：

### 用法

```bash
# 第一步：发送验证码
BATCH='+506-60012545,7126463574:AAH...,453472000,34604900,96c51d9e7a31...,' \
  python3 setup_tg_session.py

# 第二步：带验证码登录（2分钟内执行）
BATCH='+506-60012545,7126463574:AAH...,453472000,96c51d9e7a31...,,' \
  python3 setup_tg_session.py <验证码>

# 有两步验证时
BATCH='...' python3 setup_tg_session.py <验证码> <密码>
```

### 输出

脚本会输出完整的 BATCH 行（第 6 字段已填入生成的 session），直接复制替换即可：

```
==================================================
复制以下内容替换 BATCH 中对应的行:
==================================================
+506-63532545,7126463574:AAH...,453472010,34604959,96c51d9e7a31...,1AZWar...
==================================================
```

## GitHub Actions 自动化

项目内置 GitHub Actions 工作流：

| 工作流 | 文件 | 用途 |
|---|---|---|
| Hax Auto Renew | `.github/workflows/hax-renew.yml` | hax.co.id 自动续期，每 12 小时执行一次 |
| Woiden Auto Renew | `.github/workflows/woiden-renew.yml` | woiden.id 自动续期，每 12 小时执行一次（与 hax 错开 6 小时） |

### 配置 Secrets

在仓库 Settings > Secrets and variables > Actions 中添加：

| Secret 名称 | 必填 | 说明 |
|---|---|---|
| `BATCH` | 是 | 账号列表，格式：`phone,tg_bot_token,tg_chat_id,tg_api_id,tg_api_hash,tg_session` |
| `PROXY_URL` | 否 | 代理 URL（sing-box 模式） |
| `SOCKS_PORT` | 否 | SOCKS 端口（默认 10808） |

### 工作流行为

- 脚本与工作流同仓库（`fscarmen3/hax-autorenew`），无需检出私有仓库
- 截图与运行日志作为 Artifact 上传，保留 7 天
- 每次执行后自动清理超过 24 小时的已完成运行记录（保留 24 小时内及所有未完成的记录）
- 每次执行后将时间戳写入工作区根目录的 `time.txt` 并提交推送到仓库

## 代理配置

通过 `PROXY_URL` 传入代理 URL，由 `gen_singbox_config.py` 自动转换为 sing-box 配置：

| 协议 | 传输层 | TLS 选项 |
|---|---|---|
| VLESS | TCP / WebSocket / gRPC / HTTPUpgrade / SPLITHTP | Reality / TLS / None |
| VMess | TCP / WebSocket / gRPC / HTTPUpgrade | TLS / None |
| Trojan | TCP / WebSocket / gRPC | TLS |
| Shadowsocks | - | - |
| Hysteria2 | - | TLS |
| TUIC | - | TLS |
| AnyTLS | - | TLS |
| SOCKS5 | - | - |

## 项目结构

```
.
├── .github/workflows/
│   ├── hax-renew.yml             # hax.co.id 自动续期工作流
│   └── woiden-renew.yml          # woiden.id 自动续期工作流
├── hax-renew.py                  # hax.co.id 主程序：登录、续期、验证码、通知
├── woiden-renew.py               # woiden.id 主程序：登录、续期、验证码、通知（与 hax 同面板，仅域名不同）
├── setup_tg_session.py           # TG Session 生成脚本
├── gen_singbox_config.py         # 代理 URL 解析与 sing-box 配置生成
└── README.md
```

## 注意事项

- 脚本在 Linux 下通过 Xvfb 虚拟显示运行 Chrome，无需图形界面
- 手机号在日志和 TG 通知中会被部分遮蔽（如 `+506-63**2545`，国家代码后前2位可见）
- Cloudflare 绕过依赖 DrissionPage ChromiumPage 的 iframe 点击模式
- reCAPTCHA 解决依赖语音识别（speech_recognition + ffmpeg），部分 IP 可能被 Google 限制
- 算术验证码通过 ddddocr OCR 识别，成功率较高
- Telegram 验证码通过 Telethon 后台线程从 @HaxTG_bot 实时提取
- 每个 TG 账号的 API 凭据独立，互不影响
