#!/usr/bin/env python3
"""浏览器自动化模块 - 封装 Playwright 操作

职责：
1. WAF Cookie 获取（带缓存）
2. 模拟登录流程
3. 触发自动签到
4. OAuth 自动登录（使用 Chrome 已登录状态）
5. HTTP 签到（无浏览器依赖，适用于 GitHub Actions）
"""

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import httpx
from playwright.async_api import BrowserContext, Page, async_playwright

from utils.constants import (
	BROWSER_ARGS,
	CHROME_USER_AGENT,
	COOKIE_SET_WAIT_MS,
	HTTP_TIMEOUT_SECONDS,
	PAGE_LOAD_WAIT_MS,
	QUOTA_DIVISOR,
	SIGNIN_TRIGGER_WAIT_MS,
)


@dataclass
class BrowserResult:
	"""浏览器操作结果"""

	success: bool
	waf_cookies: dict[str, str]
	api_calls: list[str]
	error: str | None = None
	# 仅用于“能量/余额”验证：本次触发前后额度（美元）
	# - balance_before: 触发签到前的 quota（美元）
	# - balance_after: 触发签到后的 quota（美元）
	balance_before: float | None = None
	balance_after: float | None = None


# WAF Cookie 缓存（按域名缓存）
_waf_cookie_cache: dict[str, dict[str, str]] = {}
_cache_lock = asyncio.Lock()

# 调试截图：统一放到 data/ 下，避免污染仓库根目录
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\\\|?*]+')


def _safe_filename(text: str, max_len: int = 80) -> str:
	"""将任意文本转换为安全的文件名（Windows 兼容）"""
	cleaned = _INVALID_FILENAME_CHARS.sub('_', str(text)).strip()
	if not cleaned:
		return 'unnamed'
	return cleaned[:max_len]


def _debug_screenshot_path(prefix: str, account_name: str) -> str:
	"""生成调试截图路径（写入 data/debug_screenshots/，自动创建目录）"""
	debug_dir = Path(__file__).parent.parent / 'data' / 'debug_screenshots'
	debug_dir.mkdir(parents=True, exist_ok=True)
	ts = datetime.now().strftime('%Y%m%d_%H%M%S')
	name = _safe_filename(account_name)
	return str(debug_dir / f'{prefix}_{name}_{ts}.png')


# Stealth 脚本：隐藏自动化特征
STEALTH_SCRIPT = """
// 隐藏 webdriver 标识
Object.defineProperty(navigator, 'webdriver', {
	get: () => undefined,
	configurable: true
});

// 模拟 Chrome 运行时
window.navigator.chrome = {
	runtime: {},
	loadTimes: function() {},
	csi: function() {},
	app: {}
};

// 模拟插件
Object.defineProperty(navigator, 'plugins', {
	get: () => {
		const plugins = [
			{name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer'},
			{name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
			{name: 'Native Client', filename: 'internal-nacl-plugin'}
		];
		plugins.item = (index) => plugins[index];
		plugins.namedItem = (name) => plugins.find(p => p.name === name);
		plugins.refresh = () => {};
		return plugins;
	},
	configurable: true
});

// 模拟语言
Object.defineProperty(navigator, 'languages', {
	get: () => ['zh-CN', 'zh', 'en-US', 'en'],
	configurable: true
});

// 隐藏自动化权限查询
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
	parameters.name === 'notifications' ?
		Promise.resolve({ state: Notification.permission }) :
		originalQuery(parameters)
);

// 模拟正常的 WebGL 渲染器
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
	if (parameter === 37445) return 'Intel Inc.';
	if (parameter === 37446) return 'Intel Iris OpenGL Engine';
	return getParameter.apply(this, arguments);
};
"""


async def _create_stealth_context(playwright) -> tuple[BrowserContext, str]:
	"""创建带有 stealth 配置的浏览器上下文

	Returns:
	    (browser_context, temp_dir_path)
	"""
	temp_dir = tempfile.mkdtemp()

	context = await playwright.chromium.launch_persistent_context(
		user_data_dir=temp_dir,
		headless=True,  # 使用 headless 模式，配合 stealth 脚本
		user_agent=CHROME_USER_AGENT,
		viewport={'width': 1920, 'height': 1080},
		args=BROWSER_ARGS,
		ignore_https_errors=True,
		java_script_enabled=True,
		bypass_csp=True,
	)

	# 注入 stealth 脚本
	await context.add_init_script(STEALTH_SCRIPT)

	return context, temp_dir


async def _get_waf_cookies(page: Page, required_cookies: list[str]) -> dict[str, str]:
	"""从页面获取 WAF cookies"""
	cookies = await page.context.cookies()
	waf_cookies = {}

	for cookie in cookies:
		cookie_name = cookie.get('name')
		cookie_value = cookie.get('value')
		if cookie_name in required_cookies and cookie_value is not None:
			waf_cookies[cookie_name] = cookie_value

	return waf_cookies


async def _set_session_cookie(context: BrowserContext, domain: str, session_value: str) -> None:
	"""设置用户 session cookie"""
	parsed = urlparse(domain)
	cookie_domain = parsed.netloc

	await context.add_cookies([{
		'name': 'session',
		'value': session_value,
		'domain': cookie_domain,
		'path': '/',
		'httpOnly': True,
		'secure': True,
		'sameSite': 'Lax'
	}])


async def _wait_for_page_load(page: Page) -> None:
	"""等待页面加载完成"""
	try:
		await page.wait_for_function('document.readyState === "complete"', timeout=PAGE_LOAD_WAIT_MS)
	except Exception:
		await page.wait_for_timeout(PAGE_LOAD_WAIT_MS)


def _create_api_logger(api_calls: list[str], log_fn: Callable[[str], None] | None = None):
	"""创建 API 请求记录器"""

	def log_request(request):
		if '/api/' in request.url:
			call_info = f'{request.method} {request.url}'
			api_calls.append(call_info)
			if log_fn:
				log_fn(f'[API请求] {call_info}')

	return log_request


async def _get_waf_cookies_from_browser(
	domain: str,
	login_url: str,
	required_cookies: list[str],
	log_fn: Callable[[str], None] | None = None
) -> dict[str, str]:
	"""从浏览器获取 WAF cookies（内部函数）"""
	async with async_playwright() as p:
		context = None
		temp_dir = None
		try:
			context, temp_dir = await _create_stealth_context(p)
			page = await context.new_page()

			await page.goto(login_url, wait_until='networkidle')
			await _wait_for_page_load(page)

			waf_cookies = await _get_waf_cookies(page, required_cookies)
			return waf_cookies

		finally:
			if context:
				await context.close()
			if temp_dir:
				try:
					shutil.rmtree(temp_dir, ignore_errors=True)
				except Exception:
					pass


async def get_cached_waf_cookies(
	domain: str,
	login_url: str,
	required_cookies: list[str],
	log_fn: Callable[[str], None] | None = None
) -> dict[str, str] | None:
	"""获取 WAF cookies（带缓存，同一域名只获取一次）"""
	async with _cache_lock:
		# 检查缓存
		if domain in _waf_cookie_cache:
			cached = _waf_cookie_cache[domain]
			# 验证缓存是否包含所有需要的 cookies
			if all(c in cached for c in required_cookies):
				if log_fn:
					log_fn(f'[缓存] 使用已缓存的 WAF cookies ({domain})')
				return cached

		# 缓存未命中，从浏览器获取
		if log_fn:
			log_fn(f'[浏览器] 正在获取 WAF cookies ({domain})...')

		waf_cookies = await _get_waf_cookies_from_browser(domain, login_url, required_cookies, log_fn)

		if waf_cookies and all(c in waf_cookies for c in required_cookies):
			_waf_cookie_cache[domain] = waf_cookies
			if log_fn:
				log_fn(f'[缓存] WAF cookies 已缓存 ({domain})')
			return waf_cookies

		return None


def clear_waf_cookie_cache() -> None:
	"""清除 WAF Cookie 缓存"""
	_waf_cookie_cache.clear()


async def get_waf_cookies_and_trigger_signin(
	account_name: str,
	domain: str,
	login_url: str,
	required_cookies: list[str],
	user_session: str,
	api_user: str,
	api_user_key: str = 'new-api-user',
	log_fn: Callable[[str], None] | None = None
) -> BrowserResult:
	"""使用 Playwright 获取 WAF cookies 并触发签到

	Args:
	    account_name: 账号名称（用于日志）
	    domain: 目标域名
	    login_url: 登录页面 URL
	    required_cookies: 需要获取的 WAF cookie 名称列表
	    user_session: 用户 session cookie 值
	    api_user: API 用户标识
	    api_user_key: API 用户请求头名称
	    log_fn: 日志输出函数

	Returns:
	    BrowserResult: 包含成功状态、WAF cookies 和 API 调用记录
	"""

	def log(msg: str) -> None:
		if log_fn:
			log_fn(msg)
		else:
			print(msg)

	log(f'[浏览器] {account_name}: 正在启动浏览器并模拟登录...')

	async with async_playwright() as p:
		context = None
		temp_dir = None
		try:
			context, temp_dir = await _create_stealth_context(p)
			page = await context.new_page()

			# 第一步：访问登录页面获取 WAF cookies
			log(f'[浏览器] {account_name}: 访问登录页面获取 WAF cookies...')
			await page.goto(login_url, wait_until='networkidle')
			await _wait_for_page_load(page)

			waf_cookies = await _get_waf_cookies(page, required_cookies)
			missing_cookies = [c for c in required_cookies if c not in waf_cookies]

			if missing_cookies:
				log(f'[失败] {account_name}: 缺少 WAF cookies: {missing_cookies}')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=[],
					error=f'缺少 WAF cookies: {missing_cookies}'
				)

			log(f'[成功] {account_name}: 已获取 {len(waf_cookies)} 个 WAF cookies')

			# 第二步：设置 API 请求监听
			api_calls: list[str] = []
			page.on('request', _create_api_logger(api_calls, log))

			# 第三步：模拟退出登录
			log(f'[退出] {account_name}: 清除所有 cookies（模拟退出）...')
			await context.clear_cookies()
			await page.wait_for_timeout(COOKIE_SET_WAIT_MS)

			# 第四步：重新设置 session（模拟重新登录）
			log(f'[登录] {account_name}: 重新设置 session（模拟重新登录）...')
			await _set_session_cookie(context, domain, user_session)

			# 第五步：访问首页触发签到（AgentRouter 在首页登录成功时触发签到）
			home_url = f'{domain}/'
			log(f'[签到] {account_name}: 访问首页触发签到 ({home_url})...')
			await page.goto(home_url, wait_until='networkidle')

			# 第六步：主动调用 /api/user/self 触发签到（AgentRouter 登录时自动签到）
			log(f'[签到] {account_name}: 主动调用 /api/user/self 触发签到...')
			try:
				# 注入 api_user 和 api_user_key 到 JavaScript
				user_self_result = await page.evaluate(f'''
					async () => {{
						try {{
							const response = await fetch('/api/user/self', {{
								method: 'GET',
								credentials: 'include',
								headers: {{
									'Accept': 'application/json',
									'Content-Type': 'application/json',
									'{api_user_key}': '{api_user}'
								}}
							}});
							const data = await response.json();
							return {{ success: response.ok, status: response.status, data: data }};
						}} catch (e) {{
							return {{ success: false, error: e.message }};
						}}
					}}
				''')
				if user_self_result.get('success'):
					log(f'[成功] {account_name}: /api/user/self 调用成功')
					api_calls.append(f'GET {domain}/api/user/self (browser)')
				else:
					log(f'[警告] {account_name}: /api/user/self 调用失败: {user_self_result}')
			except Exception as e:
				log(f'[警告] {account_name}: 浏览器内 API 调用失败: {str(e)[:50]}')

			log(f'[等待] {account_name}: 等待签到逻辑执行（{SIGNIN_TRIGGER_WAIT_MS // 1000}秒）...')
			await page.wait_for_timeout(SIGNIN_TRIGGER_WAIT_MS)

			# 输出 API 调用统计
			if api_calls:
				log(f'[信息] {account_name}: 捕获到 {len(api_calls)} 个 API 调用')
				for call in api_calls:
					if 'user/self' in call:
						log(f'[关键] {account_name}: 检测到 /api/user/self 调用')
			else:
				log(f'[警告] {account_name}: 未捕获到任何 API 调用')

			log(f'[成功] {account_name}: 登出重登流程完成')

			return BrowserResult(
				success=True,
				waf_cookies=waf_cookies,
				api_calls=api_calls
			)

		except Exception as e:
			error_msg = str(e)[:100]
			log(f'[失败] {account_name}: 浏览器操作失败: {error_msg}')
			return BrowserResult(
				success=False,
				waf_cookies={},
				api_calls=[],
				error=error_msg
			)

		finally:
			if context:
				await context.close()
			if temp_dir:
				try:
					shutil.rmtree(temp_dir, ignore_errors=True)
				except Exception:
					pass  # 清理失败不影响主流程


async def perform_real_login_signin(
	account_name: str,
	domain: str,
	login_url: str,
	username: str,
	password: str,
	required_cookies: list[str],
	log_fn: Callable[[str], None] | None = None
) -> BrowserResult:
	"""执行真正的登录流程触发签到

	适用于签到在登录时自动触发的 provider（如 AgentRouter）。

	Args:
	    account_name: 账号名称（用于日志）
	    domain: 目标域名
	    login_url: 登录页面 URL
	    username: 登录用户名
	    password: 登录密码
	    required_cookies: 需要获取的 WAF cookie 名称列表
	    log_fn: 日志输出函数

	Returns:
	    BrowserResult: 包含成功状态、WAF cookies 和 API 调用记录
	"""

	def log(msg: str) -> None:
		if log_fn:
			log_fn(msg)
		else:
			print(msg)

	log(f'[浏览器] {account_name}: 正在启动浏览器执行真正登录...')

	async with async_playwright() as p:
		context = None
		temp_dir = None
		try:
			context, temp_dir = await _create_stealth_context(p)
			page = await context.new_page()

			# 设置 API 请求监听
			api_calls: list[str] = []
			page.on('request', _create_api_logger(api_calls, log))

			# 第一步：访问登录页面
			log(f'[浏览器] {account_name}: 访问登录页面...')
			await page.goto(login_url, wait_until='networkidle')
			await _wait_for_page_load(page)

			# 获取 WAF cookies
			waf_cookies = await _get_waf_cookies(page, required_cookies)
			if waf_cookies:
				log(f'[成功] {account_name}: 已获取 {len(waf_cookies)} 个 WAF cookies')

			# 第二步：填写登录表单
			log(f'[登录] {account_name}: 填写登录表单...')

			# 等待登录表单加载
			try:
				await page.wait_for_selector('input[name="username"], input[type="text"]', timeout=10000)
			except Exception:
				log(f'[警告] {account_name}: 找不到用户名输入框，尝试继续...')

			# 尝试多种选择器找到输入框
			username_selectors = [
				'input[name="username"]',
				'input[type="text"]:first-of-type',
				'input[placeholder*="用户名"]',
				'input[placeholder*="username"]',
				'input[placeholder*="邮箱"]',
				'input[placeholder*="email"]',
			]
			password_selectors = [
				'input[name="password"]',
				'input[type="password"]',
				'input[placeholder*="密码"]',
				'input[placeholder*="password"]',
			]

			# 填写用户名
			username_filled = False
			for selector in username_selectors:
				try:
					element = await page.query_selector(selector)
					if element:
						await element.fill(username)
						username_filled = True
						log(f'[登录] {account_name}: 已填写用户名')
						break
				except Exception:
					continue

			if not username_filled:
				log(f'[失败] {account_name}: 无法找到用户名输入框')
				return BrowserResult(
					success=False,
					waf_cookies=waf_cookies,
					api_calls=api_calls,
					error='无法找到用户名输入框'
				)

			# 填写密码
			password_filled = False
			for selector in password_selectors:
				try:
					element = await page.query_selector(selector)
					if element:
						await element.fill(password)
						password_filled = True
						log(f'[登录] {account_name}: 已填写密码')
						break
				except Exception:
					continue

			if not password_filled:
				log(f'[失败] {account_name}: 无法找到密码输入框')
				return BrowserResult(
					success=False,
					waf_cookies=waf_cookies,
					api_calls=api_calls,
					error='无法找到密码输入框'
				)

			# 第三步：点击登录按钮
			log(f'[登录] {account_name}: 点击登录按钮...')
			submit_selectors = [
				'button[type="submit"]',
				'button:has-text("登录")',
				'button:has-text("登 录")',
				'button:has-text("Login")',
				'input[type="submit"]',
				'.login-button',
				'.submit-button',
			]

			login_clicked = False
			for selector in submit_selectors:
				try:
					element = await page.query_selector(selector)
					if element:
						await element.click()
						login_clicked = True
						log(f'[登录] {account_name}: 已点击登录按钮')
						break
				except Exception:
					continue

			if not login_clicked:
				# 尝试按回车提交
				log(f'[登录] {account_name}: 找不到登录按钮，尝试按回车提交...')
				await page.keyboard.press('Enter')

			# 第四步：等待登录完成和页面跳转
			log(f'[登录] {account_name}: 等待登录完成...')
			try:
				# 等待 URL 变化或页面跳转
				await page.wait_for_url('**/console**', timeout=15000)
				log(f'[成功] {account_name}: 登录成功，已跳转到控制台')
			except Exception:
				# 检查是否有错误提示
				current_url = page.url
				if 'login' in current_url.lower():
					# 还在登录页，可能登录失败
					error_text = await page.evaluate('''
						() => {
							const errorEl = document.querySelector('.error, .alert-error, .message-error, [class*="error"]');
							return errorEl ? errorEl.textContent.trim() : null;
						}
					''')
					if error_text:
						log(f'[失败] {account_name}: 登录失败 - {error_text[:50]}')
						return BrowserResult(
							success=False,
							waf_cookies=waf_cookies,
							api_calls=api_calls,
							error=f'登录失败: {error_text[:50]}'
						)
				log(f'[警告] {account_name}: 登录状态不确定，当前URL: {current_url}')

			# 第五步：等待签到逻辑执行
			log(f'[等待] {account_name}: 等待签到逻辑执行（{SIGNIN_TRIGGER_WAIT_MS // 1000}秒）...')
			await page.wait_for_timeout(SIGNIN_TRIGGER_WAIT_MS)

			# 输出 API 调用统计
			if api_calls:
				log(f'[信息] {account_name}: 捕获到 {len(api_calls)} 个 API 调用')
				for call in api_calls:
					if 'user/self' in call:
						log(f'[关键] {account_name}: 检测到 /api/user/self 调用（签到触发点）')

			log(f'[成功] {account_name}: 真正登录流程完成')

			return BrowserResult(
				success=True,
				waf_cookies=waf_cookies,
				api_calls=api_calls
			)

		except Exception as e:
			error_msg = str(e)[:100]
			log(f'[失败] {account_name}: 登录流程失败: {error_msg}')
			return BrowserResult(
				success=False,
				waf_cookies={},
				api_calls=[],
				error=error_msg
			)

		finally:
			if context:
				await context.close()
			if temp_dir:
				try:
					shutil.rmtree(temp_dir, ignore_errors=True)
				except Exception:
					pass


# Chrome 远程调试配置
# 说明：
# - connect_over_cdp 需要 Chrome 以 --remote-debugging-port 启动并监听本机端口
# - 9222 是最常见/兼容性最好的默认端口（很多工具默认用它）
# - 仍保留 9022 作为兼容回退（历史脚本/用户习惯）
_DEFAULT_CHROME_DEBUG_PORT = int(os.getenv('CHROME_DEBUG_PORT', '9222'))
_EXTRA_DEBUG_PORTS = os.getenv('CHROME_DEBUG_PORTS', '').strip()

def _parse_debug_ports() -> list[int]:
	ports: list[int] = []
	if _EXTRA_DEBUG_PORTS:
		for raw in _EXTRA_DEBUG_PORTS.split(','):
			raw = raw.strip()
			if not raw:
				continue
			try:
				ports.append(int(raw))
			except Exception:
				continue

	# 兼容常见端口：9222（社区常见）/9022（历史默认）
	ports = [_DEFAULT_CHROME_DEBUG_PORT] + ports + [9222, 9022]

	# 去重并保持顺序
	seen: set[int] = set()
	unique: list[int] = []
	for p in ports:
		if p not in seen:
			seen.add(p)
			unique.append(p)
	return unique

# Playwright 专用配置目录（不使用 Chrome 的，避免锁定问题）
PLAYWRIGHT_USER_DATA_DIR = Path(__file__).parent.parent / 'data' / 'browser_profile'


# Chrome 用户数据目录（Windows 默认路径）
def get_chrome_user_data_dir() -> str:
	"""获取 Chrome 用户数据目录"""
	# Windows 默认路径
	default_path = os.path.expanduser('~\\AppData\\Local\\Google\\Chrome\\User Data')
	if os.path.exists(default_path):
		return default_path
	# 可以通过环境变量覆盖
	return os.getenv('CHROME_USER_DATA_DIR', default_path)


async def _check_chrome_debug_port(port: int) -> bool:
	"""检查 Chrome 远程调试端口是否可用"""
	import socket
	try:
		sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		sock.settimeout(1)
		result = sock.connect_ex(('127.0.0.1', port))
		sock.close()
		return result == 0
	except Exception:
		return False


async def perform_oauth_signin_with_chrome(
	account_name: str,
	domain: str,
	login_url: str,
	oauth_provider: str = 'github',
	user_cookies: dict[str, str] | None = None,
	log_fn: Callable[[str], None] | None = None,
	force_oauth: bool = False
) -> BrowserResult:
	"""使用浏览器执行签到（支持 session cookie 或 OAuth）

	优先使用 session cookie 访问 /console 触发签到。
	如果 session 无效或 force_oauth=True，则通过 OAuth 流程重新登录。

	Args:
	    account_name: 账号名称（用于日志）
	    domain: 目标域名（如 https://agentrouter.org）
	    login_url: 登录页面 URL
	    oauth_provider: OAuth 提供商（github/google/linuxdo）
	    user_cookies: 用户 cookies（包含 session），如果提供则优先使用
	    log_fn: 日志输出函数

	Returns:
	    BrowserResult: 包含成功状态和 API 调用记录
	"""

	def log(msg: str) -> None:
		if log_fn:
			log_fn(msg)
		else:
			print(msg)

	log(f'[浏览器] {account_name}: 使用浏览器执行签到...')

	# 检查是否可以通过 CDP 连接（多端口探测）
	cdp_port: int | None = None
	for port in _parse_debug_ports():
		if await _check_chrome_debug_port(port):
			cdp_port = port
			break

	cdp_available = cdp_port is not None
	log(f'[调试] {account_name}: CDP端口检测结果: {cdp_available} (端口: {cdp_port if cdp_port else "无"})')

	# 确保 Playwright 配置目录存在
	PLAYWRIGHT_USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
	profile_exists = (PLAYWRIGHT_USER_DATA_DIR / 'Default').exists()

	async with async_playwright() as p:
		browser = None
		context = None
		page = None
		use_cdp = False

		try:
			if cdp_available and cdp_port is not None:
				# 方式1：通过 CDP 连接已运行的 Chrome
				chrome_debug_url = f'http://127.0.0.1:{cdp_port}'
				log(f'[CDP] {account_name}: 检测到远程调试端口 {cdp_port}，通过 CDP 连接...')
				browser = await p.chromium.connect_over_cdp(chrome_debug_url)
				contexts = browser.contexts
				if contexts:
					context = contexts[0]
					log(f'[CDP] {account_name}: 已连接到现有浏览器上下文')
				else:
					context = await browser.new_context()
					log(f'[CDP] {account_name}: 创建新的浏览器上下文')
				use_cdp = True
			else:
				# 方式2：使用 Playwright 专用配置目录
				log(f'[信息] {account_name}: 使用 Playwright 独立配置目录...')

				context = await p.chromium.launch_persistent_context(
					user_data_dir=str(PLAYWRIGHT_USER_DATA_DIR),
					headless=False,
					args=['--disable-blink-features=AutomationControlled'],
					ignore_https_errors=True,
				)

			page = await context.new_page()

			# 设置 API 请求监听
			api_calls: list[str] = []
			page.on('request', _create_api_logger(api_calls, log))

			# ========== 优先使用 session cookie 直接签到 ==========
			# 如果 force_oauth=True，跳过cookie验证直接执行OAuth登录
			# AgentRouter等需要OAuth登录触发签到的provider应该使用force_oauth=True
			if user_cookies and 'session' in user_cookies and not force_oauth:
				log(f'[浏览器] {account_name}: 设置 session cookie...')

				# 先清除旧的 session cookie，避免冲突
				from urllib.parse import urlparse
				parsed = urlparse(domain)
				hostname = parsed.netloc  # 如 agentrouter.org

				# 获取现有 cookies 并删除所有 session cookie
				existing_cookies = await context.cookies()
				cookies_to_keep = [c for c in existing_cookies if c.get('name') != 'session']
				session_cookies_removed = len(existing_cookies) - len(cookies_to_keep)
				if session_cookies_removed > 0:
					log(f'[浏览器] {account_name}: 清除 {session_cookies_removed} 个旧 session cookie')
					# 清空所有 cookies 然后重新添加非 session 的
					await context.clear_cookies()
					if cookies_to_keep:
						await context.add_cookies(cookies_to_keep)

				# 设置新的 session cookie
				cookies_to_add = []
				for name, value in user_cookies.items():
					cookies_to_add.append({
						'name': name,
						'value': value,
						'domain': hostname,  # 如 agentrouter.org
						'path': '/',
						'secure': True,  # HTTPS 站点
						'sameSite': 'Lax',  # 第一方cookie用Lax，不要用None
						'httpOnly': True,  # 和服务器设置的一致
					})
				await context.add_cookies(cookies_to_add)
				log(f'[浏览器] {account_name}: 已设置 {len(cookies_to_add)} 个 cookie（域: {hostname}）')

				# 先访问首页，让 cookie 在浏览器中生效
				log(f'[浏览器] {account_name}: 先访问首页建立会话...')
				await page.goto(domain, wait_until='networkidle')
				await _wait_for_page_load(page)

				# 现在访问 /console 页面
				console_url = f'{domain}/console'
				log(f'[浏览器] {account_name}: 访问控制台 {console_url}...')
				await page.goto(console_url, wait_until='networkidle')
				await _wait_for_page_load(page)

				# 检查是否被重定向到登录页面
				current_url = page.url
				if '/login' not in current_url.lower():
					# 成功登录，等待签到逻辑执行
					log(f'[浏览器] {account_name}: cookie 有效，当前页面: {current_url}')
					await page.wait_for_timeout(SIGNIN_TRIGGER_WAIT_MS)
					log(f'[成功] {account_name}: 控制台页面加载完成，签到已触发')
					return BrowserResult(
						success=True,
						waf_cookies={},
						api_calls=api_calls
					)
				else:
					log(f'[浏览器] {account_name}: cookie 无效，被重定向到登录页面')

				# cookie 验证失败，继续 OAuth 流程
				log(f'[警告] {account_name}: cookie 验证失败，尝试 OAuth 登录')

			# ========== OAuth 登录流程 ==========
			if force_oauth:
				log(f'[OAuth] {account_name}: 强制执行OAuth登录以触发签到bonus...')

			if not profile_exists and not cdp_available:
				log(f'[首次] {account_name}: 首次运行，需要在浏览器中登录 {oauth_provider}')
				log('[提示] 请在打开的浏览器中完成登录，后续运行将自动使用此登录状态')

			# 访问登录页面
			log(f'[浏览器] {account_name}: 访问登录页面...')
			await page.goto(login_url, wait_until='networkidle')
			await _wait_for_page_load(page)

			# 检查是否已经登录（被重定向到非登录页面）
			current_url = page.url
			if '/login' not in current_url.lower():
				log(f'[信息] {account_name}: 已登录，重定向到 {current_url}')
				log(f'[信息] {account_name}: 尝试直接调用签到 API...')

				# 尝试调用签到 API
				try:
					sign_in_url = f'{domain}/api/user/sign_in'
					response = await page.request.post(sign_in_url)
					api_calls.append(f'POST {sign_in_url} -> {response.status}')

					if response.ok:
						try:
							data = await response.json()
							if data.get('success'):
								log(f'[成功] {account_name}: 签到 API 调用成功')
								return BrowserResult(
									success=True,
									waf_cookies={},
									api_calls=api_calls
								)
							else:
								log(f'[信息] {account_name}: 签到 API 返回: {data.get("message", "未知")}')
						except Exception:
							log(f'[信息] {account_name}: 签到 API 返回非 JSON 响应')
					else:
						log(f'[信息] {account_name}: 签到 API 返回 {response.status}')
				except Exception as e:
					log(f'[信息] {account_name}: 签到 API 调用失败: {str(e)[:50]}')

				# 如果签到 API 不存在或失败，尝试注销后重新登录
				log(f'[信息] {account_name}: 尝试注销后重新登录...')
				try:
					logout_url = f'{domain}/api/user/logout'
					await page.request.get(logout_url)
					api_calls.append(f'GET {logout_url}')
					log(f'[信息] {account_name}: 已注销，重新访问登录页...')
					await page.goto(login_url, wait_until='networkidle')
					await _wait_for_page_load(page)
				except Exception:
					log(f'[警告] {account_name}: 注销失败，继续尝试登录...')

			# 第二步：点击 OAuth 登录按钮
			log(f'[登录] {account_name}: 点击 {oauth_provider} 登录按钮...')

			# 根据 OAuth 提供商选择按钮
			oauth_selectors = {
				'github': [
					# 文本匹配
					'button:has-text("GitHub")',
					'a:has-text("GitHub")',
					'button:has-text("使用 GitHub 登录")',
					'button:has-text("Sign in with GitHub")',
					'a:has-text("Sign in with GitHub")',
					# 类名匹配
					'[class*="github"]',
					'[class*="Github"]',
					# SVG 图标匹配
					'button:has(svg[class*="github"])',
					'a:has(svg[class*="github"])',
					# aria 标签
					'[aria-label*="GitHub"]',
					'[aria-label*="github"]',
					# 通用 OAuth 按钮
					'button[data-provider="github"]',
					'a[href*="github"]',
					'a[href*="/oauth/github"]',
				],
				'google': [
					'button:has-text("Google")',
					'a:has-text("Google")',
					'[class*="google"]',
					'[aria-label*="Google"]',
				],
				'linuxdo': [
					'button:has-text("LinuxDo")',
					'a:has-text("LinuxDo")',
					'button:has-text("LINUX DO")',
					'[class*="linuxdo"]',
				],
			}

			selectors = oauth_selectors.get(oauth_provider, oauth_selectors['github'])
			clicked = False

			# OAuth 提供商域名映射
			oauth_domains = {
				'github': 'github.com',
				'google': 'accounts.google.com',
				'linuxdo': 'connect.linux.do',
			}
			expected_oauth_domain = oauth_domains.get(oauth_provider, 'github.com')

			for selector in selectors:
				try:
					element = await page.query_selector(selector)
					if element:
						# 重要：AgentRouter 的 OAuth 按钮有时会打开“新标签页/弹窗”（popup），
						# 仅等待 expect_navigation 会误判“未跳转”。这里点击一次后同时检测 popup。
						log(f'[登录] {account_name}: 尝试点击 {oauth_provider} 按钮...')

						# 记录点击前的页面集合，用于检测新弹出的页面
						pages_before = list(context.pages) if context else []
						before_url = page.url

						# 尽量保证元素在视口内
						try:
							await element.scroll_into_view_if_needed()
						except Exception:
							pass

						# 点击（必要时 force）
						try:
							await element.click(timeout=5000)
						except Exception:
							try:
								await element.click(timeout=5000, force=True)
							except Exception:
								# 如果连 force 都失败，继续尝试下一个 selector
								continue

						# 给 popup/跳转一点时间
						await page.wait_for_timeout(800)

						# 检测是否有新窗口/新标签页
						chosen_page = None
						try:
							pages_after = list(context.pages) if context else []
							new_pages = [p for p in pages_after if p not in pages_before]

							# 优先选择已经跳到 OAuth 提供商域名的页面
							for p in reversed(new_pages):
								try:
									# 新页面可能先是 about:blank，再跳转到 OAuth
									await p.wait_for_url(lambda u: u and u != 'about:blank', timeout=15000)
								except Exception:
									pass
								if expected_oauth_domain in (p.url or ''):
									chosen_page = p
									break

							# 如果没找到目标域名，也至少切到最新弹出的页面
							if not chosen_page and new_pages:
								chosen_page = new_pages[-1]
						except Exception:
							chosen_page = None

						if chosen_page:
							page = chosen_page
							# 给新页面也挂上 API 监听，方便调试
							try:
								page.on('request', _create_api_logger(api_calls, log))
							except Exception:
								pass
							log(f'[登录] {account_name}: 检测到新窗口/标签页: {page.url[:80]}')
						else:
							after_url = page.url
							if after_url != before_url:
								log(f'[登录] {account_name}: 页面已跳转: {after_url[:80]}')

						clicked = True
						break
				except Exception:
					continue

			if not clicked:
				log(f'[失败] {account_name}: 找不到 {oauth_provider} 登录按钮')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error=f'找不到 {oauth_provider} 登录按钮'
				)

			# 第三步：等待 OAuth 流程完成
			log(f'[登录] {account_name}: 等待 OAuth 授权完成...')
			# 新开页面可能先是 about:blank，这里先等到出现“有效 URL”
			try:
				await page.wait_for_url(lambda url: url and url != 'about:blank', timeout=15000)
			except Exception:
				pass

			current_url = page.url
			log(f'[登录] {account_name}: 点击后 URL: {current_url[:80]}')

			# 检查是否已经跳转到 OAuth 提供商
			if expected_oauth_domain in current_url:
				log(f'[登录] {account_name}: 已跳转到 {oauth_provider} 授权页面')
				# 如果已经授权过，可能会自动跳回
				# 等待完成授权并跳回
				try:
					# 等待 URL 变化（离开 OAuth 提供商）
					await page.wait_for_url(
						lambda url: expected_oauth_domain not in url,
						timeout=60000
					)
					log(f'[登录] {account_name}: OAuth 授权流程完成')
				except Exception:
					log(f'[失败] {account_name}: OAuth 授权超时')
					return BrowserResult(
						success=False,
						waf_cookies={},
						api_calls=api_calls,
						error='OAuth 授权超时'
					)
			elif '/login' in current_url.lower():
				# 还在登录页面，可能按钮点击没生效
				log(f'[警告] {account_name}: 点击后仍在登录页面，等待更长时间...')
				# 等待看是否会跳转
				try:
					await page.wait_for_url(
						lambda url: '/login' not in url.lower(),
						timeout=15000
					)
					log(f'[登录] {account_name}: 延迟跳转成功')
				except Exception:
					log(f'[失败] {account_name}: 点击按钮后未能跳转')
					# 保存截图
					try:
						path = _debug_screenshot_path('oauth_click_failed', account_name)
						await page.screenshot(path=path, full_page=True)
						log(f'[调试] {account_name}: 截图已保存到 {path}')
					except Exception:
						pass
					return BrowserResult(
						success=False,
						waf_cookies={},
						api_calls=api_calls,
						error='点击 OAuth 按钮后未能跳转'
					)

			# 等待页面加载完成
			await _wait_for_page_load(page)
			await page.wait_for_timeout(2000)

			# 关键检查：是否真的登录成功？
			current_url = page.url
			if '/login' in current_url.lower():
				log(f'[失败] {account_name}: OAuth 登录失败，仍在登录页面')
				# 保存截图用于调试
				try:
					path = _debug_screenshot_path('oauth_failed', account_name)
					await page.screenshot(path=path, full_page=True)
					log(f'[调试] {account_name}: 截图已保存到 {path}')
				except Exception:
					pass
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error='OAuth 登录失败，仍在登录页面'
				)

			log(f'[成功] {account_name}: OAuth 登录成功，当前页面: {current_url[:60]}')

			# 第四步：尝试多种签到方式
			log(f'[签到] {account_name}: 尝试签到...')
			signin_success = False

			# 方式1：尝试调用签到 API
			for api_path in ['/api/user/sign_in', '/api/user/signin', '/api/user/checkin', '/api/user/attendance']:
				try:
					sign_in_url = f'{domain}{api_path}'
					response = await page.request.post(sign_in_url)
					api_calls.append(f'POST {sign_in_url} -> {response.status}')

					if response.ok:
						try:
							data = await response.json()
							if data.get('success'):
								log(f'[成功] {account_name}: 签到成功！(via {api_path})')
								signin_success = True
								break
							else:
								msg = data.get('message', '')
								if '已' in msg or 'already' in msg.lower():
									log(f'[信息] {account_name}: 今日已签到')
									signin_success = True
									break
						except Exception:
							pass
				except Exception:
					pass

			# 方式2：如果 API 不存在，尝试点击页面上的签到按钮
			if not signin_success:
				log(f'[签到] {account_name}: 尝试查找签到按钮...')

				# 先截图保存，方便调试
				try:
					screenshot_path = _debug_screenshot_path('debug_agentrouter', account_name)
					await page.screenshot(path=screenshot_path, full_page=True)
					log(f'[调试] {account_name}: 已保存截图到 {screenshot_path}')
				except Exception as e:
					log(f'[调试] {account_name}: 截图失败: {str(e)[:30]}')

				# 打印页面上所有可见按钮
				try:
					buttons = await page.query_selector_all('button, a[href], [role="button"]')
					button_texts = []
					for btn in buttons[:20]:  # 只取前20个
						try:
							text = await btn.inner_text()
							if text and text.strip():
								button_texts.append(text.strip()[:30])
						except Exception:
							pass
					if button_texts:
						log(f'[调试] {account_name}: 页面按钮: {", ".join(button_texts[:10])}')
				except Exception:
					pass

				signin_selectors = [
					'button:has-text("签到")',
					'a:has-text("签到")',
					'button:has-text("每日签到")',
					'button:has-text("领取")',
					'button:has-text("Check")',
					'[class*="sign"]',
					'[class*="checkin"]',
					'[data-action*="sign"]',
				]

				for selector in signin_selectors:
					try:
						element = await page.query_selector(selector)
						if element:
							# 检查按钮是否可见
							is_visible = await element.is_visible()
							if is_visible:
								await element.click()
								log(f'[成功] {account_name}: 已点击签到按钮')
								signin_success = True
								await page.wait_for_timeout(2000)
								break
					except Exception:
						continue

			# 等待一下让签到生效
			await page.wait_for_timeout(2000)

			# 输出 API 调用统计
			if api_calls:
				log(f'[信息] {account_name}: 捕获到 {len(api_calls)} 个 API 调用')

			if signin_success:
				log(f'[成功] {account_name}: OAuth 登录并签到完成')
			else:
				log(f'[信息] {account_name}: OAuth 登录完成（签到状态未确认）')

			return BrowserResult(
				success=True,
				waf_cookies={},
				api_calls=api_calls
			)

		except Exception as e:
			error_msg = str(e)[:100]
			log(f'[失败] {account_name}: OAuth 登录失败: {error_msg}')
			return BrowserResult(
				success=False,
				waf_cookies={},
				api_calls=[],
				error=error_msg
			)

		finally:
			# CDP 模式只关闭页面，断开连接但不关闭浏览器
			if use_cdp:
				if page:
					try:
						await page.close()
					except Exception:
						pass
				# CDP 模式不关闭 browser，只断开连接
			else:
				if context:
					await context.close()


# ============ HTTP 签到（无浏览器依赖） ============


async def try_direct_http_signin(
	account_name: str,
	domain: str,
	sign_in_path: str | None,
	user_info_path: str,
	user_cookies: dict[str, str],
	api_user: str,
	api_user_key: str = 'new-api-user',
	log_fn: Callable[[str], None] | None = None
) -> BrowserResult:
	"""尝试直接通过 HTTP 调用签到 API（不获取 WAF cookies）

	适用于 WAF 不严格或已有有效 cookies 的情况。
	如果被 WAF 拦截（返回非 JSON），调用方应回退到浏览器方式。

	注意：当 sign_in_path 为 None 时（如 AgentRouter），访问 user_info_path 会自动触发签到。

	Args:
	    account_name: 账号名称（用于日志）
	    domain: 目标域名
	    sign_in_path: 签到 API 路径（如 /api/user/sign_in），为 None 表示访问 user_info 自动签到
	    user_info_path: 用户信息 API 路径（如 /api/user/self）
	    user_cookies: 用户 cookies（包含 session）
	    api_user: API 用户标识
	    api_user_key: API 用户请求头名称
	    log_fn: 日志输出函数

	Returns:
	    BrowserResult: success=True 表示签到成功，error 包含失败原因
	"""

	def log(msg: str) -> None:
		if log_fn:
			log_fn(msg)
		else:
			print(msg)

	log(f'[HTTP直连] {account_name}: 尝试直接调用签到 API（无 WAF cookies）...')

	api_calls: list[str] = []

	try:
		async with httpx.AsyncClient(
			http2=True,
			timeout=HTTP_TIMEOUT_SECONDS,
			follow_redirects=True,
			verify=True
		) as client:
			# 设置 cookies
			for name, value in user_cookies.items():
				client.cookies.set(name, value, domain=urlparse(domain).netloc)

			# 构建请求头
			headers = {
				'User-Agent': CHROME_USER_AGENT,
				'Accept': 'application/json, text/plain, */*',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br',
				'Content-Type': 'application/json',
				'Origin': domain,
				'Referer': f'{domain}/console/token',
				'Connection': 'keep-alive',
				'X-Requested-With': 'XMLHttpRequest',
				api_user_key: api_user,
			}

			# 第一步：尝试获取用户信息（验证 session 是否有效）
			user_info_url = f'{domain}{user_info_path}'
			log(f'[HTTP直连] {account_name}: 验证 session 有效性...')

			user_response = await client.get(user_info_url, headers=headers)
			api_calls.append(f'GET {user_info_url} -> {user_response.status_code}')

			# 检查是否被 WAF 拦截（返回非 JSON）
			try:
				user_data = user_response.json()
			except Exception:
				log(f'[WAF拦截] {account_name}: 返回非 JSON 响应，需要获取 WAF cookies')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error='WAF_BLOCKED'  # 特殊标记，表示需要回退到浏览器
				)

			# 检查 session 是否有效
			if not user_data.get('success'):
				error_msg = user_data.get('message', 'session 无效')
				log(f'[失败] {account_name}: session 无效 - {error_msg}')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error=f'SESSION_INVALID: {error_msg}'
				)

			# 记录触发前余额（用于判定“本次是否拿到奖励”）
			balance_before = None
			try:
				quota_raw = (user_data.get('data') or {}).get('quota')
				if isinstance(quota_raw, (int, float)):
					balance_before = round(quota_raw / QUOTA_DIVISOR, 2)
			except Exception:
				balance_before = None

			log(f'[HTTP直连] {account_name}: session 有效，执行签到...')

			# 第二步：调用签到 API（或自动签到）
			if sign_in_path is None:
				# sign_in_path 为 None 时（如 AgentRouter），访问 user_info 已触发自动签到
				log(f'[成功] {account_name}: 访问用户信息已触发自动签到')
				return BrowserResult(
					success=True,
					waf_cookies={},
					api_calls=api_calls,
					balance_before=balance_before,
					balance_after=balance_before,
				)

			sign_in_url = f'{domain}{sign_in_path}'
			sign_response = await client.post(sign_in_url, headers=headers)
			api_calls.append(f'POST {sign_in_url} -> {sign_response.status_code}')

			# 检查签到响应
			try:
				sign_data = sign_response.json()
			except Exception:
				log(f'[WAF拦截] {account_name}: 签到 API 返回非 JSON 响应')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error='WAF_BLOCKED'
				)

			# 检查签到结果
			if sign_data.get('ret') == 1 or sign_data.get('code') == 0 or sign_data.get('success'):
				msg = sign_data.get('msg', sign_data.get('message', ''))
				# 再拉一次用户信息，获取触发后余额（部分站点奖励可能稍有延迟，做一次轻量确认）
				balance_after = None
				try:
					after_resp = await client.get(user_info_url, headers=headers)
					api_calls.append(f'GET {user_info_url} -> {after_resp.status_code}')
					after_json = after_resp.json()
					if after_json.get('success'):
						quota_raw = (after_json.get('data') or {}).get('quota')
						if isinstance(quota_raw, (int, float)):
							balance_after = round(quota_raw / QUOTA_DIVISOR, 2)
				except Exception:
					balance_after = None

				log(f'[成功] {account_name}: HTTP 直连签到成功！{msg}')
				return BrowserResult(
					success=True,
					waf_cookies={},
					api_calls=api_calls,
					balance_before=balance_before,
					balance_after=balance_after,
				)
			else:
				error_msg = sign_data.get('msg', sign_data.get('message', '未知错误'))
				log(f'[失败] {account_name}: 签到失败 - {error_msg}')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error=error_msg
				)

	except httpx.HTTPStatusError as e:
		error_msg = f'HTTP 错误: {e.response.status_code}'
		log(f'[失败] {account_name}: {error_msg}')
		return BrowserResult(
			success=False,
			waf_cookies={},
			api_calls=api_calls,
			error=error_msg
		)
	except Exception as e:
		error_msg = str(e)[:100]
		log(f'[失败] {account_name}: HTTP 请求失败: {error_msg}')
		return BrowserResult(
			success=False,
			waf_cookies={},
			api_calls=api_calls,
			error=error_msg
		)


async def trigger_signin_via_http(
	account_name: str,
	domain: str,
	login_url: str,
	user_cookies: dict[str, str],
	api_user: str,
	api_user_key: str = 'new-api-user',
	log_fn: Callable[[str], None] | None = None
) -> BrowserResult:
	"""使用 HTTP 请求触发签到（AgentRouter 专用）

	AgentRouter 签到机制：访问 /console 页面时，前端会调用 /api/user/self 接口，
	后端检测到登录状态后可能自动完成签到。这里用纯 HTTP 模拟这个流程。

	流程：
	1. 验证 session 有效性（调用 /api/user/self）
	2. 访问 /console 页面（模拟正常使用）
	3. 再次调用 /api/user/self（触发签到逻辑）

	Args:
	    account_name: 账号名称（用于日志）
	    domain: 目标域名（如 https://agentrouter.org）
	    login_url: 登录页面 URL（备用）
	    user_cookies: 用户 cookies（包含 session）
	    api_user: API 用户标识
	    api_user_key: API 用户请求头名称
	    log_fn: 日志输出函数

	Returns:
	    BrowserResult: success=True 表示签到流程完成
	"""

	def log(msg: str) -> None:
		if log_fn:
			log_fn(msg)
		else:
			print(msg)

	log(f'[HTTP] {account_name}: 开始 HTTP 签到流程...')

	api_calls: list[str] = []
	waf_cookies: dict[str, str] = {}

	try:
		async with httpx.AsyncClient(
			http2=True,
			timeout=HTTP_TIMEOUT_SECONDS,
			follow_redirects=True,
			verify=True
		) as client:
			async def safe_get(url: str, headers: dict[str, str], tag: str) -> httpx.Response:
				"""带重试的 GET（仅重试网络类错误）"""
				for attempt in range(3):
					try:
						return await client.get(url, headers=headers)
					except (httpx.ConnectError, httpx.TimeoutException, httpx.TransportError) as e:
						if attempt >= 2:
							raise
						wait_s = 2 ** attempt
						log(f'[重试] {account_name}: {tag} 请求失败（{type(e).__name__}），{wait_s}s 后重试...')
						await asyncio.sleep(wait_s)

			# 重要：不要把 Cookie 写死在 header 里，否则服务端下发的 Set-Cookie（如 acw_tc）
			# 无法被保存，后续请求仍会被 WAF 拦截。这里统一用 httpx 的 cookie jar。
			from urllib.parse import urlparse
			hostname = urlparse(domain).netloc

			# 构建请求头（不手动塞 Cookie）
			headers = {
				'User-Agent': CHROME_USER_AGENT,
				'Accept': 'application/json, text/plain, */*',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br',
				'Connection': 'keep-alive',
				'Referer': f'{domain}/console/token',
				api_user_key: api_user,
			}

			# 第零步：先访问登录页获取 WAF cookies（例如 acw_tc）
			# 这一步不需要 session，目的只是让 WAF 发 cookie（Max-Age 一般较短）。
			waf_boot_headers = {
				'User-Agent': CHROME_USER_AGENT,
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br',
				'Connection': 'keep-alive',
				'Referer': f'{domain}/',
			}
			log(f'[HTTP] {account_name}: 预热 WAF（访问登录页获取 acw_tc 等 cookies）...')
			waf_resp = await safe_get(login_url, waf_boot_headers, 'WAF预热')
			api_calls.append(f'GET {login_url} -> {waf_resp.status_code}')
			# 记录 WAF cookies（供后续请求复用）
			try:
				waf_cookies = {k: v for k, v in client.cookies.items() if k and v and k != 'session'}
			except Exception:
				waf_cookies = {}

			# 设置用户 cookies（包含 session）
			for name, value in user_cookies.items():
				if value is None:
					continue
				client.cookies.set(name, value, domain=hostname)

			# 第一步：验证 session 有效性
			user_info_url = f'{domain}/api/user/self'
			log(f'[HTTP] {account_name}: 验证 session 有效性...')
			response = await safe_get(user_info_url, headers, 'session验证')
			api_calls.append(f'GET {user_info_url} -> {response.status_code}')

			try:
				data = response.json()
			except Exception:
				log(f'[失败] {account_name}: API 返回非 JSON，可能被 WAF 拦截')
				return BrowserResult(
					success=False,
					waf_cookies=waf_cookies,
					api_calls=api_calls,
					error='API 返回异常'
				)

			if not data.get('success'):
				error_msg = data.get('message', 'session 无效')
				log(f'[失败] {account_name}: session 无效 - {error_msg}')
				return BrowserResult(
					success=False,
					waf_cookies=waf_cookies,
					api_calls=api_calls,
					error='session 已过期，需要重新登录'
				)

			username = data.get('data', {}).get('username', 'unknown')
			# 记录触发前余额（用于判断是否真的拿到签到奖励）
			balance_before = None
			try:
				quota_raw = (data.get('data') or {}).get('quota')
				if isinstance(quota_raw, (int, float)):
					balance_before = round(quota_raw / QUOTA_DIVISOR, 2)
			except Exception:
				balance_before = None
			log(f'[HTTP] {account_name}: session 有效（用户: {username}）')

			# 第二步：访问 /login 页面（已登录状态下访问会触发登录检测/可能触发签到逻辑）
			login_page_url = f'{domain}/login'
			log(f'[HTTP] {account_name}: 访问 {login_page_url} 触发登录检测...')
			login_headers = {
				'User-Agent': CHROME_USER_AGENT,
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br',
				'Connection': 'keep-alive',
				'Referer': f'{domain}/',
			}
			login_response = await safe_get(login_page_url, login_headers, '/login访问')
			api_calls.append(f'GET {login_page_url} -> {login_response.status_code}')
			log(f'[HTTP] {account_name}: /login 响应状态: {login_response.status_code}')

			# 第三步：访问 /console 页面（模拟正常使用）
			console_url = f'{domain}/console'
			log(f'[HTTP] {account_name}: 访问 {console_url} 模拟正常使用...')
			console_headers = {
				'User-Agent': CHROME_USER_AGENT,
				'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
				'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
				'Accept-Encoding': 'gzip, deflate, br',
				'Connection': 'keep-alive',
				'Referer': f'{domain}/',
			}
			console_response = await safe_get(console_url, console_headers, '/console访问')
			api_calls.append(f'GET {console_url} -> {console_response.status_code}')
			log(f'[HTTP] {account_name}: /console 响应状态: {console_response.status_code}')

			# 第四步：再次调用 /api/user/self（模拟前端行为，触发签到逻辑）
			log(f'[HTTP] {account_name}: 调用 {user_info_url} 触发签到逻辑...')
			final_response = await safe_get(user_info_url, headers, '最终校验')
			api_calls.append(f'GET {user_info_url} -> {final_response.status_code}')

			try:
				final_data = final_response.json()
				if final_data.get('success'):
					# 记录触发后余额（用于判断是否真的拿到签到奖励）
					balance_after = None
					try:
						quota_raw = (final_data.get('data') or {}).get('quota')
						if isinstance(quota_raw, (int, float)):
							balance_after = round(quota_raw / QUOTA_DIVISOR, 2)
					except Exception:
						balance_after = None

					# 有些站点的签到奖励可能是“异步入账”（额度不会立刻变化）。
					# 如果前后额度不变，短时间内再拉取几次 /api/user/self 做延迟确认。
					if (
						balance_before is not None
						and balance_after is not None
						and abs(balance_after - balance_before) < 0.01
					):
						for wait_s in (2, 5, 10, 20):
							try:
								log(f'[HTTP] {account_name}: 额度未变化，{wait_s}s 后重试拉取额度确认...')
								await asyncio.sleep(wait_s)
								again = await safe_get(user_info_url, headers, f'延迟确认{wait_s}s')
								api_calls.append(f'GET {user_info_url} -> {again.status_code}')
								again_data = again.json()
								if again_data.get('success'):
									quota_raw2 = (again_data.get('data') or {}).get('quota')
									if isinstance(quota_raw2, (int, float)):
										new_balance = round(quota_raw2 / QUOTA_DIVISOR, 2)
										if new_balance > balance_after + 0.01:
											log(f'[验证] {account_name}: 延迟入账检测到额度变化: ${balance_after} → ${new_balance} (+{new_balance - balance_after:.2f})')
											balance_after = new_balance
											break
							except Exception:
								# 延迟确认失败不影响主流程
								continue
					log(f'[成功] {account_name}: HTTP 签到流程完成')
					return BrowserResult(
						success=True,
						waf_cookies=waf_cookies,
						api_calls=api_calls,
						balance_before=balance_before,
						balance_after=balance_after,
					)
				else:
					error_msg = final_data.get('message', '未知错误')
					log(f'[失败] {account_name}: 最终验证失败 - {error_msg}')
					return BrowserResult(
						success=False,
						waf_cookies=waf_cookies,
						api_calls=api_calls,
						error=error_msg,
						balance_before=balance_before,
					)
			except Exception:
				log(f'[失败] {account_name}: 最终 API 返回非 JSON')
				return BrowserResult(
					success=False,
					waf_cookies={},
					api_calls=api_calls,
					error='API 返回异常'
				)

	except httpx.HTTPStatusError as e:
		error_msg = f'HTTP 错误: {e.response.status_code}'
		log(f'[失败] {account_name}: {error_msg}')
		return BrowserResult(
			success=False,
			waf_cookies=waf_cookies,
			api_calls=api_calls,
			error=error_msg
		)
	except Exception as e:
		msg = str(e).strip()
		if not msg:
			msg = repr(e)
		error_msg = f'{type(e).__name__}: {msg}'[:120]
		log(f'[失败] {account_name}: HTTP 请求失败: {error_msg}')
		return BrowserResult(
			success=False,
			waf_cookies=waf_cookies,
			api_calls=api_calls,
			error=error_msg
		)
