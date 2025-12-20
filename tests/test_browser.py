#!/usr/bin/env python3
"""浏览器模块测试"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# 添加项目根目录到 PATH
import sys
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.browser import (
	BrowserResult,
	_create_api_logger,
	_get_waf_cookies,
	clear_waf_cookie_cache,
	get_cached_waf_cookies,
)


class TestBrowserResult:
	"""BrowserResult 测试"""

	def test_success_result(self):
		"""测试成功结果"""
		result = BrowserResult(
			success=True,
			waf_cookies={'acw_tc': 'test_value'},
			api_calls=['GET /api/user/self']
		)
		assert result.success is True
		assert 'acw_tc' in result.waf_cookies
		assert len(result.api_calls) == 1
		assert result.error is None

	def test_failure_result(self):
		"""测试失败结果"""
		result = BrowserResult(
			success=False,
			waf_cookies={},
			api_calls=[],
			error='Connection failed'
		)
		assert result.success is False
		assert result.waf_cookies == {}
		assert result.api_calls == []
		assert result.error == 'Connection failed'


class TestApiLogger:
	"""API 日志记录器测试"""

	def test_logs_api_calls(self):
		"""测试记录 API 调用"""
		api_calls = []
		logger = _create_api_logger(api_calls)

		# 模拟请求对象
		mock_request = MagicMock()
		mock_request.url = 'https://example.com/api/user/self'
		mock_request.method = 'GET'

		logger(mock_request)

		assert len(api_calls) == 1
		assert 'GET' in api_calls[0]
		assert '/api/user/self' in api_calls[0]

	def test_ignores_non_api_calls(self):
		"""测试忽略非 API 调用"""
		api_calls = []
		logger = _create_api_logger(api_calls)

		mock_request = MagicMock()
		mock_request.url = 'https://example.com/static/main.js'
		mock_request.method = 'GET'

		logger(mock_request)

		assert len(api_calls) == 0

	def test_calls_log_function(self):
		"""测试调用日志函数"""
		api_calls = []
		log_messages = []
		log_fn = lambda msg: log_messages.append(msg)

		logger = _create_api_logger(api_calls, log_fn)

		mock_request = MagicMock()
		mock_request.url = 'https://example.com/api/test'
		mock_request.method = 'POST'

		logger(mock_request)

		assert len(log_messages) == 1
		assert '[API请求]' in log_messages[0]


class TestWafCookieCache:
	"""WAF Cookie 缓存测试"""

	def test_clear_cache(self):
		"""测试清除缓存"""
		# 注意：这只测试函数不抛出异常
		clear_waf_cookie_cache()
		# 如果运行到这里说明清除成功
		assert True


class TestGetWafCookies:
	"""获取 WAF Cookies 测试"""

	@pytest.mark.asyncio
	async def test_extracts_required_cookies(self):
		"""测试提取所需 cookies"""
		# 模拟 page 对象
		mock_page = AsyncMock()
		mock_page.context.cookies = AsyncMock(return_value=[
			{'name': 'acw_tc', 'value': 'test_acw'},
			{'name': 'cdn_sec_tc', 'value': 'test_cdn'},
			{'name': 'other_cookie', 'value': 'other_value'},
		])

		required = ['acw_tc', 'cdn_sec_tc']
		result = await _get_waf_cookies(mock_page, required)

		assert 'acw_tc' in result
		assert 'cdn_sec_tc' in result
		assert 'other_cookie' not in result
		assert result['acw_tc'] == 'test_acw'

	@pytest.mark.asyncio
	async def test_handles_missing_cookies(self):
		"""测试处理缺失 cookies"""
		mock_page = AsyncMock()
		mock_page.context.cookies = AsyncMock(return_value=[
			{'name': 'acw_tc', 'value': 'test_acw'},
		])

		required = ['acw_tc', 'cdn_sec_tc']
		result = await _get_waf_cookies(mock_page, required)

		assert 'acw_tc' in result
		assert 'cdn_sec_tc' not in result

	@pytest.mark.asyncio
	async def test_handles_empty_cookies(self):
		"""测试处理空 cookies"""
		mock_page = AsyncMock()
		mock_page.context.cookies = AsyncMock(return_value=[])

		required = ['acw_tc']
		result = await _get_waf_cookies(mock_page, required)

		assert result == {}


class TestCachedWafCookies:
	"""缓存 WAF Cookies 测试"""

	@pytest.fixture(autouse=True)
	def clear_cache_before_test(self):
		"""每个测试前清除缓存"""
		clear_waf_cookie_cache()
		yield
		clear_waf_cookie_cache()

	@pytest.mark.asyncio
	async def test_returns_cached_cookies(self):
		"""测试返回缓存的 cookies"""
		domain = 'https://test.example.com'
		login_url = f'{domain}/login'
		required = ['acw_tc']

		# 模拟浏览器获取函数
		with patch('utils.browser._get_waf_cookies_from_browser') as mock_browser:
			mock_browser.return_value = {'acw_tc': 'cached_value'}

			# 第一次调用 - 应该从浏览器获取
			result1 = await get_cached_waf_cookies(domain, login_url, required)
			assert result1 == {'acw_tc': 'cached_value'}
			assert mock_browser.call_count == 1

			# 第二次调用 - 应该使用缓存
			result2 = await get_cached_waf_cookies(domain, login_url, required)
			assert result2 == {'acw_tc': 'cached_value'}
			# 不应该再次调用浏览器
			assert mock_browser.call_count == 1

	@pytest.mark.asyncio
	async def test_logs_cache_hit(self):
		"""测试记录缓存命中"""
		domain = 'https://cache-test.example.com'
		login_url = f'{domain}/login'
		required = ['acw_tc']
		log_messages = []

		with patch('utils.browser._get_waf_cookies_from_browser') as mock_browser:
			mock_browser.return_value = {'acw_tc': 'value'}

			# 第一次调用
			await get_cached_waf_cookies(domain, login_url, required, lambda m: log_messages.append(m))

			# 清除日志
			log_messages.clear()

			# 第二次调用 - 应该有缓存命中日志
			await get_cached_waf_cookies(domain, login_url, required, lambda m: log_messages.append(m))

			assert any('[缓存]' in m for m in log_messages)

	@pytest.mark.asyncio
	async def test_returns_none_on_failure(self):
		"""测试失败时返回 None"""
		domain = 'https://fail.example.com'
		login_url = f'{domain}/login'
		required = ['acw_tc', 'missing_cookie']

		with patch('utils.browser._get_waf_cookies_from_browser') as mock_browser:
			# 返回不完整的 cookies
			mock_browser.return_value = {'acw_tc': 'value'}

			result = await get_cached_waf_cookies(domain, login_url, required)
			assert result is None


class TestStealthScript:
	"""Stealth 脚本测试"""

	def test_stealth_script_exists(self):
		"""测试 stealth 脚本存在"""
		from utils.browser import STEALTH_SCRIPT

		assert STEALTH_SCRIPT is not None
		assert len(STEALTH_SCRIPT) > 0

	def test_stealth_script_has_webdriver_hide(self):
		"""测试 stealth 脚本隐藏 webdriver"""
		from utils.browser import STEALTH_SCRIPT

		assert 'webdriver' in STEALTH_SCRIPT
		assert 'undefined' in STEALTH_SCRIPT

	def test_stealth_script_has_chrome_mock(self):
		"""测试 stealth 脚本模拟 Chrome"""
		from utils.browser import STEALTH_SCRIPT

		assert 'chrome' in STEALTH_SCRIPT
		assert 'runtime' in STEALTH_SCRIPT

	def test_stealth_script_has_plugins(self):
		"""测试 stealth 脚本模拟插件"""
		from utils.browser import STEALTH_SCRIPT

		assert 'plugins' in STEALTH_SCRIPT
		assert 'Chrome PDF' in STEALTH_SCRIPT

