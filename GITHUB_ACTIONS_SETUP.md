# GitHub Actions 配置指南

本文档说明如何配置 GitHub Actions 实现自动签到。

## 📋 前置准备

1. Fork 本项目到你的 GitHub 账号
2. 获取账号的 session cookie（见下方说明）

## 🔑 获取 Session Cookie

### 方法一：浏览器开发者工具

1. 登录 AnyRouter 网站
2. 按 F12 打开开发者工具
3. 切换到 "Application" 或 "存储" 标签
4. 左侧找到 "Cookies" → 选择网站域名
5. 找到名为 `session` 的 cookie，复制其值

### 方法二：使用本项目脚本

```bash
# 运行一次本地签到，会自动显示 cookie 信息
uv run checkin.py
```

## ⚙️ 配置 GitHub Secrets

进入你 Fork 的仓库，依次点击：`Settings` → `Secrets and variables` → `Actions` → `New repository secret`

### 必需配置

#### ANYROUTER_ACCOUNTS

账号配置，JSON 数组格式：

```json
[
  {
    "provider": "anyrouter",
    "api_user": "你的API用户ID",
    "cookies": {
      "session": "你的session cookie值"
    },
    "name": "AnyRouter主账号"
  }
]
```

**多账号示例：**

```json
[
  {
    "provider": "anyrouter",
    "api_user": "12345",
    "cookies": {"session": "abc123..."},
    "name": "主账号"
  },
  {
    "provider": "anyrouter",
    "api_user": "67890",
    "cookies": {"session": "def456..."},
    "name": "备用账号"
  }
]
```

### 可选配置（通知）

根据需要配置以下任意通知渠道：

| Secret 名称 | 说明 | 获取方式 |
|------------|------|---------|
| `DINGDING_WEBHOOK` | 钉钉机器人 Webhook | 钉钉群设置 → 智能群助手 → 添加机器人 |
| `FEISHU_WEBHOOK` | 飞书机器人 Webhook | 飞书群设置 → 群机器人 → 添加机器人 |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot Token | 与 @BotFather 对话创建 |
| `TELEGRAM_CHAT_ID` | Telegram Chat ID | 与 @userinfobot 对话获取 |
| `WEIXIN_WEBHOOK` | 企业微信 Webhook | 企业微信群 → 添加群机器人 |
| `PUSHPLUS_TOKEN` | PushPlus Token | [pushplus.plus](http://www.pushplus.plus/) 注册获取 |
| `SERVERPUSHKEY` | Server酱 Key | [sct.ftqq.com](https://sct.ftqq.com/) 注册获取 |
| `GOTIFY_URL` | Gotify 服务器地址 | 自建 Gotify 服务器 |
| `GOTIFY_TOKEN` | Gotify Token | Gotify 管理面板创建 |
| `EMAIL_USER` | 发件邮箱地址 | 你的邮箱 |
| `EMAIL_PASS` | 邮箱授权码 | 邮箱设置中获取 |
| `EMAIL_TO` | 收件邮箱地址 | 接收通知的邮箱 |

## 🚀 启用 GitHub Actions

1. 进入仓库的 `Actions` 标签
2. 如果看到提示，点击 "I understand my workflows, go ahead and enable them"
3. 找到 "公益站 自动签到" workflow
4. 点击 "Enable workflow"

## ⏰ 运行时间

默认配置为每 6 小时运行一次（UTC 时间 00:00、06:00、12:00、18:00）

对应北京时间：08:00、14:00、20:00、02:00

### 修改运行时间

编辑 `.github/workflows/checkin.yml` 文件中的 cron 表达式：

```yaml
schedule:
  - cron: "0 */6 * * *"  # 每6小时
  # - cron: "0 0,12 * * *"  # 每天00:00和12:00（UTC）
  # - cron: "0 2 * * *"  # 每天02:00（UTC）
```

## 🧪 手动测试

配置完成后，可以手动触发一次测试：

1. 进入 `Actions` 标签
2. 选择 "公益站 自动签到" workflow
3. 点击 "Run workflow" → "Run workflow"
4. 等待执行完成，查看日志

## ❓ 常见问题

### Q: 为什么签到失败？

**A:** 检查以下几点：
1. Session cookie 是否过期（需要重新获取）
2. API_USER 是否正确（在网站个人中心查看）
3. Secrets 配置格式是否正确（JSON 格式）

### Q: 如何查看执行日志？

**A:** `Actions` 标签 → 选择具体的运行记录 → 点击 "Run check-in" 查看详细日志

### Q: Cookie 多久会过期？

**A:** 通常 30-90 天，过期后需要重新登录获取新的 cookie

### Q: 可以添加其他平台吗？

**A:** 可以！编辑 `providers.json` 添加新平台配置，然后在 `ANYROUTER_ACCOUNTS` 中添加对应账号

## 🔒 安全说明

- ✅ 所有敏感数据（cookies、tokens）都存储在 GitHub Secrets 中，加密保护
- ✅ 代码中不包含任何敏感信息
- ✅ 运行日志会自动脱敏，不会泄露完整 cookie
- ⚠️ 不要将 Secrets 内容分享给他人
- ⚠️ 定期更新 cookie（建议每月重新获取一次）

## 📞 获取帮助

遇到问题？

1. 查看 [README.md](README.md) 了解项目详情
2. 查看 [Issues](../../issues) 搜索类似问题
3. 提交新的 Issue 描述你的问题
