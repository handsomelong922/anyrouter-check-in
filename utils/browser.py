#!/usr/bin/env python3
"""浏览器自动化模块 - 封装 Playwright 操作

职责：
1. WAF Cookie 获取（带缓存）
2. 模拟登录流程
3. 触发自动签到
"""

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page, async_playwright

from utils.constants import (
	BROWSER_ARGS,
	CHROME_USER_AGENT,
	COOKIE_SET_WAIT_MS,
	PAGE_LOAD_WAIT_MS,
	SIGNIN_TRIGGER_WAIT_MS,
)


@dataclass
class BrowserResult:
	"""浏览器操作结果"""

	success: bool
	waf_cookies: dict[str, str]
	api_calls: list[str]
	error: str | None = None


# WAF Cookie 缓存（按域名缓存）
_waf_cookie_cache: dict[str, dict[str, str]] = {}
_cache_lock = asyncio.Lock()


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
	log_fn: Callable[[str], None] | None = None
) -> BrowserResult:
	"""使用 Playwright 获取 WAF cookies 并触发签到

	Args:
	    account_name: 账号名称（用于日志）
	    domain: 目标域名
	    login_url: 登录页面 URL
	    required_cookies: 需要获取的 WAF cookie 名称列表
	    user_session: 用户 session cookie 值
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

			# 第五步：访问首页触发签到
			log(f'[签到] {account_name}: 访问首页触发签到...')
			await page.goto(domain, wait_until='networkidle')

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
