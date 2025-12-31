#!/usr/bin/env python3
"""公益站（AnyRouter/AgentRouter）自动签到脚本

支持的签到策略：
- browser_waf: 浏览器获取 WAF cookies + 调用签到 API（AnyRouter）
- http_login: HTTP 访问登录页触发自动签到（AgentRouter）

特性：
- 使用常量替代魔法数字
- 函数职责单一
- 统一异步模型（httpx.AsyncClient）
- HTTP 重试机制（tenacity）
"""

import asyncio
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv
from tenacity import (
	retry,
	retry_if_exception_type,
	stop_after_attempt,
	wait_exponential,
)

from utils.browser import (
	clear_waf_cookie_cache,
	get_cached_waf_cookies,
	perform_oauth_signin_with_chrome,
	perform_real_login_signin,
	trigger_signin_via_http,
	try_direct_http_signin,
)
from utils.config import AccountConfig, AppConfig, ProviderConfig, load_accounts_config_with_db
from utils.constants import (
	CHROME_USER_AGENT,
	DATA_DIR,
	HTTP_TIMEOUT_SECONDS,
	LOG_FILE,
	MAX_CONCURRENT_ACCOUNTS,
	QUOTA_DIVISOR,
)
from utils.result import (
	SigninRecord,
	SigninResult,
	SigninStatus,
	SigninSummary,
	UserBalance,
	format_time_remaining,
	generate_balance_hash,
	get_next_signin_time,
	is_in_cooldown,
	load_balance_hash,
	load_signin_history_with_db,
	save_all_signins_to_db,
	save_balance_hash,
	save_signin_history,
	update_signin_history,
)

# 尝试强制 UTF-8 输出（尽量减少 Windows 终端中文乱码）
try:
	if hasattr(sys.stdout, 'reconfigure'):
		sys.stdout.reconfigure(encoding='utf-8', errors='replace')
	if hasattr(sys.stderr, 'reconfigure'):
		sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
	pass

# 先加载环境变量，再导入 notify 模块
load_dotenv()
from utils.notify import notify

# ============ 运行模式/环境开关 ============
# CI/Actions 默认禁用数据库：避免敏感 cookie 落库 & 避免旧 DB 覆盖 Secrets
DISABLE_DATABASE = os.getenv('DISABLE_DATABASE', '').strip().lower() in ('1', 'true', 'yes', 'on')
DISABLE_DATABASE = DISABLE_DATABASE or os.getenv('GITHUB_ACTIONS', '').strip().lower() == 'true'

# 确保 data/ 目录存在（日志/历史文件会用到）
try:
	os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
	# 不阻塞主流程，后续写文件会再报错提示
	pass

# 尝试导入数据库模块（可选依赖）
try:
	from utils.database import close_database, init_database
	HAS_DATABASE = not DISABLE_DATABASE
except ImportError:
	HAS_DATABASE = False

# ============ 日志管理 ============


class Logger:
	"""日志管理器，同时输出到控制台和文件"""

	def __init__(self, log_file: str):
		self.log_file = log_file
		self.terminal = sys.stdout

	def write(self, message: str) -> None:
		"""写入消息到控制台和文件"""
		self.terminal.write(message)
		try:
			with open(self.log_file, 'a', encoding='utf-8') as f:
				f.write(message)
		except IOError as e:
			# 日志写入失败时输出到终端（而非静默忽略）
			self.terminal.write(f'\n[日志错误] 写入日志文件失败: {e}\n')

	def flush(self) -> None:
		"""刷新输出"""
		self.terminal.flush()


def setup_logging() -> None:
	"""设置日志输出"""
	# 无论是否 --manual 都写入 task_run.log（用户希望任何运行方式都有日志）
	sys.stdout = Logger(LOG_FILE)
	sys.stderr = sys.stdout


# ============ Cookie 处理 ============


def parse_cookies(cookies_data: dict | str) -> dict[str, str]:
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict
	return {}


# ============ API 请求 ============

# 重试装饰器：网络错误时自动重试，指数退避
_http_retry = retry(
	retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
	stop=stop_after_attempt(3),
	wait=wait_exponential(multiplier=1, min=2, max=10),
	reraise=True,
)


def build_request_headers(provider: ProviderConfig, api_user: str) -> dict[str, str]:
	"""构建请求头"""
	return {
		'User-Agent': CHROME_USER_AGENT,
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Accept-Encoding': 'gzip, deflate, br, zstd',
		'Referer': f'{provider.domain}/console/token',
		'Origin': provider.domain,
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
		provider.api_user_key: api_user,
	}


@_http_retry
async def fetch_user_info(
	client: httpx.AsyncClient,
	headers: dict,
	user_info_url: str
) -> UserBalance | None:
	"""获取用户信息（异步，带重试）"""
	try:
		response = await client.get(user_info_url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)

		if response.status_code == 200:
			data = response.json()
			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / QUOTA_DIVISOR, 2)
				used_quota = round(user_data.get('used_quota', 0) / QUOTA_DIVISOR, 2)
				return UserBalance(quota=quota, used_quota=used_quota)

		print(f'[错误] 获取用户信息失败: HTTP {response.status_code}')
		return None
	except (httpx.ConnectError, httpx.TimeoutException):
		raise  # 让 tenacity 处理重试
	except Exception as e:
		print(f'[错误] 获取用户信息异常: {str(e)[:50]}')
		return None


@_http_retry
async def execute_manual_signin(
	client: httpx.AsyncClient,
	account_name: str,
	provider: ProviderConfig,
	headers: dict
) -> bool:
	"""执行手动签到请求（异步，带重试）"""
	print(f'[网络] {account_name}: 正在执行签到')

	checkin_headers = headers.copy()
	checkin_headers.update({
		'Content-Type': 'application/json',
		'X-Requested-With': 'XMLHttpRequest'
	})

	sign_in_url = f'{provider.domain}{provider.sign_in_path}'

	try:
		response = await client.post(sign_in_url, headers=checkin_headers, timeout=HTTP_TIMEOUT_SECONDS)

		if response.status_code != 200:
			print(f'[失败] {account_name}: 签到失败 - HTTP {response.status_code}')
			return False

		result = response.json()
		print(f'[调试] {account_name}: 签到API响应: {result}')

		# 检查各种成功标识
		if result.get('ret') == 1 or result.get('code') == 0 or result.get('success'):
			msg = result.get('msg', result.get('message', ''))
			if '已签到' in msg or '已经签到' in msg:
				print(f'[信息] {account_name}: {msg}')
			else:
				print(f'[成功] {account_name}: 签到成功！{msg}')
			return True

		error_msg = result.get('msg', result.get('message', '未知错误'))
		print(f'[失败] {account_name}: 签到失败 - {error_msg}')
		return False

	except (httpx.ConnectError, httpx.TimeoutException):
		raise  # 让 tenacity 处理重试
	except Exception as e:
		print(f'[失败] {account_name}: 签到请求异常 - {str(e)[:50]}')
		return False


# ============ 签到核心逻辑 ============


async def get_waf_cookies_for_browser_waf(
	account_name: str,
	provider: ProviderConfig,
	user_cookies: dict[str, str]
) -> dict[str, str] | None:
	"""为 browser_waf 模式获取 WAF cookies（带缓存）

	browser_waf 模式使用缓存的 WAF cookies，因为多个账号可以共享同一域名的 WAF cookies。
	"""
	if not provider.waf_cookie_names:
		print(f'[信息] {account_name}: 无需 WAF cookies')
		return user_cookies

	login_url = f'{provider.domain}{provider.login_path}'

	waf_cookies = await get_cached_waf_cookies(
		domain=provider.domain,
		login_url=login_url,
		required_cookies=provider.waf_cookie_names,
		log_fn=print
	)

	if waf_cookies:
		return {**waf_cookies, **user_cookies}

	print(f'[失败] {account_name}: 无法获取 WAF cookies')
	return None


def create_signin_result(
	account_key: str,
	account_name: str,
	current_balance: float | None,
	last_balance: float | None,
	last_signin: datetime | None,
	user_info: UserBalance | None,
	api_success: bool = True,
	api_returned_success: bool = False,
	# 本次触发前/后余额（用于“本次是否拿到奖励”的准确判定）
	balance_before_run: float | None = None,
	balance_after_run: float | None = None,
) -> SigninResult:
	"""创建签到结果（无副作用）

	Args:
		account_key: 账号唯一标识
		account_name: 账号显示名称
		current_balance: 当前余额
		last_balance: 上次记录余额
		last_signin: 上次签到时间
		user_info: 用户信息
		api_success: API 请求是否成功（网络层面）
		api_returned_success: 签到 API 返回的 success 字段是否为 True
		balance_before_run: 本次触发前余额（美元）
		balance_after_run: 本次触发后余额（美元）
	"""
	if not api_success:
		return SigninResult(
			account_key=account_key,
			account_name=account_name,
			status=SigninStatus.ERROR,
			user_info=user_info,
			error='API 请求失败',
			last_signin=last_signin
		)

	# 统一使用“本次触发后余额”作为最终余额（如果没提供则回退 current_balance）
	final_balance = balance_after_run if balance_after_run is not None else current_balance

	if final_balance is None:
		return SigninResult(
			account_key=account_key,
			account_name=account_name,
			status=SigninStatus.ERROR,
			user_info=user_info,
			error='无法获取余额信息',
			last_signin=last_signin
		)

	# 关键：优先用“本次触发前/后”判断是否拿到奖励，避免用历史余额误报“本次成功”
	before_balance = balance_before_run if balance_before_run is not None else last_balance
	after_balance = final_balance

	if before_balance is None:
		# 首次运行/无历史：不强行判定成功与否，记录当前余额即可
		status = SigninStatus.FIRST_RUN
		diff = None
	else:
		diff = round(after_balance - before_balance, 2)
		status = SigninStatus.SUCCESS if diff > 0 else SigninStatus.COOLDOWN

	# 创建新的签到记录
	new_record = None
	# 只要我们确认“本次触发流程已完成”（api_returned_success=True），就更新记录，
	# 以避免历史余额长期不更新导致误报（同时也能正确控制冷却期跳过）。
	if api_returned_success:
		new_record = SigninRecord(time=datetime.now(), balance=after_balance)

	return SigninResult(
		account_key=account_key,
		account_name=account_name,
		status=status,
		balance_before=before_balance,
		balance_after=after_balance,
		balance_diff=diff,
		user_info=user_info,
		new_record=new_record,
		last_signin=last_signin
	)


async def process_single_account(
	account: AccountConfig,
	index: int,
	app_config: AppConfig,
	signin_history: dict[str, SigninRecord]
) -> SigninResult:
	"""处理单个账号的签到（无副作用）"""
	account_name = account.get_display_name(index)
	account_key = f'{account.provider}_{account.api_user}'

	print(f'\n[处理中] 开始处理 {account_name}')

	# 获取历史记录
	last_record = signin_history.get(account_key)
	last_signin = last_record.time if last_record else None
	last_balance = last_record.balance if last_record else None

	# 检查冷却期
	if last_signin:
		if is_in_cooldown(last_signin):
			next_signin = get_next_signin_time(last_signin)
			remaining = format_time_remaining(next_signin)
			print(f'[已跳过] {account_name} | 上次签到时间: {last_signin.strftime("%Y-%m-%d %H:%M:%S")} | 剩余冷却: {remaining}')
			return SigninResult(
				account_key=account_key,
				account_name=account_name,
				status=SigninStatus.SKIPPED,
				balance_before=last_balance,
				last_signin=last_signin
			)
		print(f'[可签到] {account_name}: 冷却期已过')
	else:
		print(f'[首次] {account_name}: 首次签到')

	# 获取 provider 配置
	provider = app_config.get_provider(account.provider)
	if not provider:
		print(f'[失败] {account_name}: 配置中未找到 Provider "{account.provider}"')
		return SigninResult(
			account_key=account_key,
			account_name=account_name,
			status=SigninStatus.ERROR,
			error=f'未知 Provider: {account.provider}'
		)

	print(f'[信息] {account_name}: 使用 provider "{account.provider}" ({provider.domain})')

	# 解析 cookies
	user_cookies = parse_cookies(account.cookies)
	if not user_cookies:
		print(f'[失败] {account_name}: cookies 配置格式无效')
		return SigninResult(
			account_key=account_key,
			account_name=account_name,
			status=SigninStatus.ERROR,
			error='cookies 格式无效'
		)

	# 根据 signin_method 直接调度签到策略
	signin_method = provider.signin_method
	http_signin_done = False  # 标记 HTTP 签到是否已完成
	all_cookies = user_cookies
	# 本次触发前/后余额（用于准确判定“本次是否拿到奖励”）
	run_balance_before: float | None = None
	run_balance_after: float | None = None

	print(f'[信息] {account_name}: 签到策略 = {signin_method}')

	# ========== browser_waf 模式：AnyRouter ==========
	# 策略：HTTP 优先尝试签到 API，被 WAF 拦截时回退到浏览器获取 cookies
	if signin_method == 'browser_waf':
		# 第一步：尝试 HTTP 直连签到（无需 WAF cookies）
		http_result = await try_direct_http_signin(
			account_name=account_name,
			domain=provider.domain,
			sign_in_path=provider.sign_in_path,
			user_info_path=provider.user_info_path,
			user_cookies=user_cookies,
			api_user=account.api_user,
			api_user_key=provider.api_user_key,
			log_fn=print
		)

		if http_result.success:
			# HTTP 直连成功
			http_signin_done = True
			run_balance_before = http_result.balance_before
			run_balance_after = http_result.balance_after
		elif http_result.error == 'WAF_BLOCKED':
			# 被 WAF 拦截，回退到浏览器获取 WAF cookies
			print(f'[回退] {account_name}: 被 WAF 拦截，启动浏览器获取 WAF cookies...')
			all_cookies = await get_waf_cookies_for_browser_waf(account_name, provider, user_cookies)
			if not all_cookies:
				return SigninResult(
					account_key=account_key,
					account_name=account_name,
					status=SigninStatus.ERROR,
					error='无法获取 WAF cookies'
				)
		elif http_result.error and http_result.error.startswith('SESSION_INVALID'):
			# Session 无效
			return SigninResult(
				account_key=account_key,
				account_name=account_name,
				status=SigninStatus.ERROR,
				error='Session 已过期，请更新 cookies'
			)
		else:
			# 其他错误，尝试浏览器方式
			print(f'[回退] {account_name}: HTTP 签到失败（{http_result.error}），尝试浏览器方式...')
			all_cookies = await get_waf_cookies_for_browser_waf(account_name, provider, user_cookies)
			if not all_cookies:
				return SigninResult(
					account_key=account_key,
					account_name=account_name,
					status=SigninStatus.ERROR,
					error='无法获取 WAF cookies'
				)

	# ========== http_login 模式：AgentRouter ==========
	# 策略：HTTP 访问登录页触发自动签到，OAuth 回退到浏览器
	elif signin_method == 'http_login':
		login_url = f'{provider.domain}{provider.login_path}'

		result = await trigger_signin_via_http(
			account_name=account_name,
			domain=provider.domain,
			login_url=login_url,
			user_cookies=user_cookies,
			api_user=account.api_user,
			api_user_key=provider.api_user_key,
			log_fn=print
		)

		if result.success:
			# 关键：把 HTTP 流程中获得的 WAF cookies 合并到后续请求（如果有的话）
			# 并使用触发前/后余额作为本次签到验证依据。
			if result.waf_cookies:
				all_cookies = {**result.waf_cookies, **user_cookies}
			if result.balance_before is not None and result.balance_after is not None:
				print(f'[验证] {account_name}: 额度变化: ${result.balance_before} → ${result.balance_after} ({result.balance_after - result.balance_before:+.2f})')
				run_balance_before = result.balance_before
				run_balance_after = result.balance_after
				# 余额不变通常意味着“今日已签到/冷却中”或“奖励延迟入账”
				balance_unchanged = abs(result.balance_after - result.balance_before) < 0.01
				if balance_unchanged:
					print(f'[提示] {account_name}: 额度未变化，按“冷却/已签到”处理（如你确认没签到过，再考虑手动登录一次）')
			http_signin_done = True
		elif account.has_oauth_config():
			# HTTP 失败且有 OAuth 配置，尝试浏览器方式（带 session cookie）
			print(f'[回退] {account_name}: HTTP 签到失败，尝试浏览器方式...')
			# AgentRouter等没有sign_in_path的provider需要force_oauth=True
			# 因为签到是通过OAuth登录触发的，不是简单访问页面
			needs_force_oauth = provider.sign_in_path is None
			result = await perform_oauth_signin_with_chrome(
				account_name=account_name,
				domain=provider.domain,
				login_url=login_url,
				oauth_provider=account.oauth_provider,
				user_cookies=user_cookies,  # 传递 session cookie
				log_fn=print,
				force_oauth=needs_force_oauth
			)
			if not result.success:
				return SigninResult(
					account_key=account_key,
					account_name=account_name,
					status=SigninStatus.ERROR,
					error=result.error or 'OAuth 登录失败'
				)
			# 合并 cookies（理论上 OAuth 流程会在浏览器内完成，这里仅保持一致）
			if result.waf_cookies:
				all_cookies = {**result.waf_cookies, **user_cookies}
			if result.balance_before is not None and result.balance_after is not None:
				print(f'[验证] {account_name}: 额度变化: ${result.balance_before} → ${result.balance_after} ({result.balance_after - result.balance_before:+.2f})')
				run_balance_before = result.balance_before
				run_balance_after = result.balance_after
			http_signin_done = True
		elif account.has_login_credentials():
			# 有用户名密码，尝试真正登录
			print(f'[回退] {account_name}: HTTP 签到失败，尝试用户名密码登录...')
			result = await perform_real_login_signin(
				account_name=account_name,
				domain=provider.domain,
				login_url=login_url,
				username=account.username,
				password=account.password,
				required_cookies=provider.waf_cookie_names or [],
				log_fn=print
			)
			if not result.success:
				return SigninResult(
					account_key=account_key,
					account_name=account_name,
					status=SigninStatus.ERROR,
					error=result.error or '登录失败'
				)
			all_cookies = {**result.waf_cookies, **user_cookies}
			http_signin_done = True
		else:
			# 没有回退方案
			return SigninResult(
				account_key=account_key,
				account_name=account_name,
				status=SigninStatus.ERROR,
				error=result.error or 'HTTP 签到失败且无回退方案'
			)

	else:
		# 未知的 signin_method
		return SigninResult(
			account_key=account_key,
			account_name=account_name,
			status=SigninStatus.ERROR,
			error=f'未知的签到策略: {signin_method}'
		)

	# 使用异步 HTTP 客户端（context manager 自动关闭）
	async with httpx.AsyncClient(http2=True, timeout=HTTP_TIMEOUT_SECONDS) as client:
		client.cookies.update(all_cookies)
		headers = build_request_headers(provider, account.api_user)
		user_info_url = f'{provider.domain}{provider.user_info_path}'

		# 查询当前余额
		print(f'[信息] {account_name}: 查询当前余额')
		user_info = await fetch_user_info(client, headers, user_info_url)

		if user_info:
			print(f'[当前] {user_info.display}')
			current_balance = user_info.quota
			# 若还没拿到“本次触发前余额”，至少用当前余额兜底（避免 None）
			if run_balance_before is None and not http_signin_done:
				run_balance_before = current_balance
		else:
			current_balance = None

		# 执行签到
		api_success = True
		api_returned_success = False

		if http_signin_done:
			# HTTP/浏览器方式已完成签到
			print(f'[信息] {account_name}: 签到已完成')
			api_returned_success = True
		elif signin_method == 'browser_waf' and provider.sign_in_path:
			# browser_waf 模式：本次触发前余额就是当前余额（调用签到前）
			if user_info and run_balance_before is None:
				run_balance_before = user_info.quota
			# browser_waf 模式：需要调用签到 API（使用浏览器获取的 WAF cookies）
			api_success = await execute_manual_signin(client, account_name, provider, headers)
			api_returned_success = api_success

			# 签到后再次查询余额
			if api_success:
				print(f'[信息] {account_name}: 查询签到后余额')
				user_info_after = await fetch_user_info(client, headers, user_info_url)
				if user_info_after:
					print(f'[签到后] {user_info_after.display}')
					current_balance = user_info_after.quota
					user_info = user_info_after
					run_balance_after = current_balance
		elif signin_method == 'browser_waf' and not provider.sign_in_path:
			# browser_waf 模式但 sign_in_path 为 None（如 AgentRouter）
			# 访问 user_info 时已自动触发签到，标记为成功
			print(f'[信息] {account_name}: 访问用户信息已触发自动签到')
			api_returned_success = True

		# 创建签到结果
		result = create_signin_result(
			account_key=account_key,
			account_name=account_name,
			current_balance=current_balance,
			last_balance=last_balance,
			last_signin=last_signin,
			user_info=user_info,
			api_success=api_success,
			api_returned_success=api_returned_success,
			balance_before_run=run_balance_before,
			balance_after_run=run_balance_after,
		)

		# 输出结果
		now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
		if result.status == SigninStatus.SUCCESS:
			print(f'[签到成功] {account_name} | 余额: ${result.balance_before} → ${result.balance_after} (+${result.balance_diff}) | 时间: {now_str}')
		elif result.status == SigninStatus.FIRST_RUN:
			print(f'[首次运行] {account_name} | 当前余额: ${current_balance} | 时间: {now_str}')
		elif result.status == SigninStatus.COOLDOWN:
			print(f'[已签到] {account_name}: 余额未变化（今日已签到）')
		elif result.status == SigninStatus.FAILED:
			print(f'[签到失败] {account_name}: 余额未变化或减少')

		return result


# ============ 通知生成 ============


def build_notification_content(summary: SigninSummary) -> str:
	"""构建通知内容（与日志格式一致）"""
	lines = []

	# 时间信息
	now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
	lines.append(f'[时间] 执行时间: {now_str}')
	lines.append('')

	# 账号详情（格式与日志一致）
	for result in summary.results:
		if result.status == SigninStatus.SUCCESS:
			# [签到成功] 账号名 | 余额: $X → $Y (+$Z) | 时间: YYYY-MM-DD HH:MM:SS
			line = f'[签到成功] {result.account_name}'
			if result.balance_before is not None and result.balance_after is not None:
				line += f' | 余额: ${result.balance_before} → ${result.balance_after}'
				if result.balance_diff is not None:
					line += f' (+${result.balance_diff})'
			if result.last_signin:
				line += f' | 上次: {result.last_signin.strftime("%m-%d %H:%M")}'
			lines.append(line)

		elif result.status == SigninStatus.FIRST_RUN:
			# [首次运行] 账号名 | 当前余额: $X
			line = f'[首次运行] {result.account_name}'
			if result.balance_after is not None:
				line += f' | 当前余额: ${result.balance_after}'
			lines.append(line)

		elif result.status == SigninStatus.SKIPPED:
			# [已跳过] 账号名 | 上次签到: YYYY-MM-DD HH:MM:SS | 剩余冷却: XhYm
			line = f'[已跳过] {result.account_name}'
			if result.last_signin:
				line += f' | 上次签到: {result.last_signin.strftime("%Y-%m-%d %H:%M:%S")}'
				next_signin = get_next_signin_time(result.last_signin)
				remaining = format_time_remaining(next_signin)
				line += f' | 剩余: {remaining}'
			if result.balance_before is not None:
				line += f' | 余额: ${result.balance_before}'
			lines.append(line)

		elif result.status == SigninStatus.COOLDOWN:
			# [冷却中] 账号名 | 余额未变化（今日已签到）
			line = f'[冷却中] {result.account_name}'
			if result.last_signin:
				line += f' | 上次: {result.last_signin.strftime("%m-%d %H:%M")}'
			if result.balance_after is not None:
				line += f' | 余额: ${result.balance_after}'
			lines.append(line)

		elif result.status == SigninStatus.FAILED:
			# [签到失败] 账号名 | 余额未变化或减少
			line = f'[签到失败] {result.account_name}'
			if result.balance_before is not None and result.balance_after is not None:
				line += f' | 余额: ${result.balance_before} → ${result.balance_after}'
				if result.balance_diff is not None and result.balance_diff != 0:
					line += f' ({result.balance_diff:+.2f})'
			elif result.balance_after is not None:
				line += f' | 余额: ${result.balance_after}'
			if result.last_signin:
				line += f' | 上次: {result.last_signin.strftime("%m-%d %H:%M")}'
			lines.append(line)

		elif result.status == SigninStatus.ERROR:
			# [发生错误] 账号名 | 错误: XXX
			line = f'[发生错误] {result.account_name}'
			if result.error:
				line += f' | 错误: {result.error}'
			lines.append(line)

	lines.append('')

	# 统计信息
	lines.append('[统计] 签到结果:')
	lines.append(f'  总计: {summary.total} | 成功: {summary.success} | 冷却: {summary.cooldown} | 失败: {summary.failed}')

	if summary.success > 0:
		lines.append(f'[提示] 本次有 {summary.success} 个账号成功签到')
	if summary.failed > 0:
		lines.append(f'[警告] 有 {summary.failed} 个账号签到失败')

	return '\n'.join(lines)


# ============ 主程序 ============


async def run_checkin() -> SigninSummary:
	"""执行签到流程（核心逻辑）

	使用信号量控制并发，避免同时启动过多浏览器实例。
	"""
	print('[系统] 公益站 多账号自动签到脚本已启动')
	print(f'[时间] 执行时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	# 初始化数据库（自动迁移）
	if HAS_DATABASE:
		try:
			init_database()
		except Exception as e:
			print(f'[警告] 数据库初始化失败: {e}')

	# 加载配置
	app_config = AppConfig.load_from_env()
	print(f'[信息] 已加载 {len(app_config.providers)} 个 provider 配置')

	# 加载账号配置（优先数据库）
	accounts = load_accounts_config_with_db()
	if not accounts:
		print('[失败] 无法加载账号配置，程序退出')
		sys.exit(1)

	print(f'[信息] 找到 {len(accounts)} 个账号配置')

	# 加载历史记录（优先数据库）
	signin_history = load_signin_history_with_db()
	print(f'[信息] 已加载签到历史（{len(signin_history)} 条记录）')

	last_balance_hash = load_balance_hash()

	# 清除 WAF Cookie 缓存（每次运行重新获取）
	clear_waf_cookie_cache()

	# 创建信号量控制并发
	semaphore = asyncio.Semaphore(MAX_CONCURRENT_ACCOUNTS)

	async def process_with_semaphore(account: AccountConfig, index: int) -> SigninResult:
		"""带信号量的账号处理"""
		async with semaphore:
			try:
				return await process_single_account(account, index, app_config, signin_history)
			except Exception as e:
				print(f'[失败] {account.get_display_name(index)} 处理异常: {e}')
				return SigninResult(
					account_key=f'{account.provider}_{account.api_user}',
					account_name=account.get_display_name(index),
					status=SigninStatus.ERROR,
					error=str(e)[:100]
				)

	# 并行处理所有账号
	print(f'[信息] 开始并行处理（最大并发: {MAX_CONCURRENT_ACCOUNTS}）')
	tasks = [process_with_semaphore(account, i) for i, account in enumerate(accounts)]
	results = await asyncio.gather(*tasks)

	# 汇总结果
	summary = SigninSummary()
	current_balances: dict[str, float] = {}

	for result in results:
		summary.add_result(result)
		# 余额 hash 用于“是否发送通知”的变化检测：
		# - 必须使用稳定 key（account_key），避免因排序/跳过导致误判
		# - 即使账号被跳过，也应使用上次记录余额参与计算，避免 hash 抖动
		balance: float | None = None
		if result.balance_after is not None:
			balance = result.balance_after
		elif result.user_info:
			balance = result.user_info.quota
		elif result.balance_before is not None:
			balance = result.balance_before

		if balance is not None:
			current_balances[result.account_key] = balance

	# 检查余额变化
	current_balance_hash = generate_balance_hash(current_balances) if current_balances else ''

	if current_balance_hash:
		if last_balance_hash is None:
			summary.is_first_run = True
			print('[通知] 检测到首次运行，将发送当前余额通知')
		elif current_balance_hash != last_balance_hash:
			summary.balance_changed = True
			print('[通知] 检测到余额变化，将发送通知')
		else:
			print('[信息] 未检测到余额变化')

	# 更新签到历史（返回新字典）
	new_history = update_signin_history(signin_history, summary.results)
	save_signin_history(new_history)
	print(f'[信息] 已保存签到历史（{len(new_history)} 条记录）')

	# 保存签到记录到数据库
	if HAS_DATABASE:
		saved_count = save_all_signins_to_db(summary.results)
		if saved_count > 0:
			print(f'[数据库] 已保存 {saved_count} 条签到记录到数据库')

	# 保存余额 hash
	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	return summary


async def main() -> None:
	"""主函数"""
	# 设置日志
	setup_logging()

	# 如果是定时任务模式，添加日志分隔符
	if '--manual' not in sys.argv:
		print('\n' + '=' * 60)
		print('新一轮签到开始')
		print('=' * 60)

	try:
		# 执行签到
		summary = await run_checkin()

		# 发送通知
		if summary.needs_notification:
			content = build_notification_content(summary)
			print(content)
			notify.push_message('公益站 签到提醒', content, msg_type='text')
			print('[通知] 已发送通知')
		else:
			print('[信息] 所有账号成功且无余额变化，跳过通知')

		# 设置退出码
		sys.exit(0 if summary.failed == 0 else 1)
	finally:
		# 关闭数据库连接
		if HAS_DATABASE:
			close_database()


def run_main() -> None:
	"""运行主函数的包装函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[警告] 程序被用户中断')
		sys.exit(1)
	except Exception as e:
		print(f'\n[失败] 程序执行时发生错误: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
