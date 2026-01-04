#!/usr/bin/env python3
"""checkin 模块核心函数测试"""

import sys
from types import ModuleType
from unittest.mock import MagicMock

# 为了避免安装 Playwright 依赖，注入最小的 mock 模块
playwright_module = ModuleType('playwright')
async_api_module = ModuleType('playwright.async_api')
async_api_module.async_playwright = None
sys.modules['playwright'] = playwright_module
sys.modules['playwright.async_api'] = async_api_module

from checkin import get_user_info


def _mock_client_with_response(json_data: dict, status_code: int = 200):
	"""构造带固定响应的 httpx 客户端"""
	response = MagicMock()
	response.status_code = status_code
	response.json.return_value = json_data

	client = MagicMock()
	client.get.return_value = response
	return client


def test_get_user_info_accepts_code_flag():
	"""code==0 时也应视为成功"""
	client = _mock_client_with_response({'code': 0, 'data': {'quota': 1000000, 'used_quota': 500000}})
	result = get_user_info(client, {}, 'https://example.com/api/user/self')

	assert result['success'] is True
	assert result['quota'] == 2.0
	assert result['used_quota'] == 1.0


def test_get_user_info_accepts_ret_flag():
	"""ret==1 时也应视为成功"""
	client = _mock_client_with_response({'ret': 1, 'data': {'quota': 500000}})
	result = get_user_info(client, {}, 'https://example.com/api/user/self')

	assert result['success'] is True
	assert result['quota'] == 1.0
	assert result['used_quota'] == 0.0
