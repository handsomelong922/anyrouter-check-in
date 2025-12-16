# Git 提交前安全检查清单

在提交到 GitHub 之前，请确保完成以下检查：

## ✅ 敏感文件检查

- [ ] `.env` 文件已被 `.gitignore` 忽略（已配置）
- [ ] `.env` 文件不存在于工作目录（已删除）
- [ ] `balance_hash.txt` 已被 `.gitignore` 忽略（已配置）
- [ ] `balance_hash.txt` 不存在于工作目录（已删除）
- [ ] `.env.local` 只包含示例配置，无真实敏感信息（已清理）

## ✅ 配置文件检查

- [ ] `.env.local` 使用占位符（如 `your_session_here`）
- [ ] `.env.template` 使用占位符
- [ ] `.env.example` 使用占位符

## ✅ 文档检查

- [ ] `README.md` 不包含真实的账号信息
- [ ] `README.md` 不包含真实的余额数据
- [ ] `README.md` 不包含真实的 session 或 api_user

## ✅ 代码检查

- [ ] 源代码中无硬编码的敏感信息
- [ ] 测试文件中无真实账号数据

## ✅ Git 状态检查

运行以下命令确认即将提交的内容：

```bash
# 查看将要提交的文件
git status

# 确认 .env 文件不在列表中
git status | grep ".env"

# 查看所有修改内容
git diff

# 查看将要提交的文件内容
git diff --cached
```

## ⚠️ 危险信号

如果看到以下内容，**不要提交**：

- ❌ `.env` 文件在 `git status` 列表中
- ❌ 真实的 session 值（长字符串如 `MTc2NTM3NjUwNX...`）
- ❌ 真实的账号余额数据
- ❌ 真实的 API User ID
- ❌ 任何 webhook URL 或 token

## 🔒 推荐的提交流程

```bash
# 1. 查看状态
git status

# 2. 只添加需要的文件
git add checkin.py utils/ .gitignore README.md .env.local .env.template run_checkin.bat setup_schedule.ps1 test_config.py

# 3. 再次确认
git status

# 4. 提交
git commit -m "feat: 全面汉化，支持本地运行，修复 JSON 解析和 AgentRouter bug"

# 5. 推送
git push origin main
```

## ✅ 已处理的安全措施

- ✅ `.env` 已添加到 `.gitignore`
- ✅ `.env` 文件已从工作目录删除
- ✅ `balance_hash.txt` 已添加到 `.gitignore`
- ✅ `balance_hash.txt` 已从工作目录删除
- ✅ `.env.local` 已替换为示例配置
- ✅ `README.md` 已移除真实余额数据

## 🎯 安全提交命令（推荐使用）

```bash
cd anyrouter-check-in

# 确认安全状态
git status

# 应该看到：
# - .env 不在列表中（被 .gitignore 忽略）
# - balance_hash.txt 不在列表中（被 .gitignore 忽略）
# - 只有代码文件和配置模板

# 如果确认无误，执行提交
git add .
git commit -m "feat: 全面汉化，新增本地运行支持，修复多个 bug"
git push
```
