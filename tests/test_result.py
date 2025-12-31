#!/usr/bin/env python3
"""签到结果模块测试"""

import os

# 添加项目根目录到 PATH
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from utils.result import (
	SigninRecord,
	SigninResult,
	SigninStatus,
	SigninSummary,
	UserBalance,
	_atomic_write,
	analyze_balance_change,
	format_time_remaining,
	generate_balance_hash,
	get_next_signin_time,
	is_in_cooldown,
	update_signin_history,
)


class TestUserBalance:
	"""UserBalance 测试"""

	def test_display_format(self):
		"""测试显示格式"""
		balance = UserBalance(quota=10.5, used_quota=5.25)
		assert '$10.5' in balance.display
		assert '$5.25' in balance.display


class TestSigninRecord:
	"""SigninRecord 测试"""

	def test_to_dict(self):
		"""测试转换为字典"""
		now = datetime.now()
		record = SigninRecord(time=now, balance=10.5)
		result = record.to_dict()

		assert 'time' in result
		assert result['balance'] == 10.5

	def test_from_dict_new_format(self):
		"""测试从新格式字典创建"""
		now = datetime.now()
		data = {'time': now.isoformat(), 'balance': 10.5}
		record = SigninRecord.from_dict(data)

		assert record is not None
		assert record.balance == 10.5

	def test_from_dict_old_format(self):
		"""测试从旧格式（只有时间字符串）创建"""
		now = datetime.now()
		record = SigninRecord.from_dict(now.isoformat())

		assert record is not None
		assert record.balance is None

	def test_from_dict_invalid(self):
		"""测试无效数据返回 None"""
		assert SigninRecord.from_dict('invalid') is None
		assert SigninRecord.from_dict({'invalid': 'data'}) is None


class TestSigninResult:
	"""SigninResult 测试"""

	def test_is_success(self):
		"""测试成功状态判断"""
		success = SigninResult(
			account_key='test',
			account_name='Test',
			status=SigninStatus.SUCCESS
		)
		assert success.is_success is True

		failed = SigninResult(
			account_key='test',
			account_name='Test',
			status=SigninStatus.FAILED
		)
		assert failed.is_success is False

	def test_needs_notification(self):
		"""测试通知需求判断"""
		success = SigninResult(
			account_key='test',
			account_name='Test',
			status=SigninStatus.SUCCESS
		)
		assert success.needs_notification is True

		skipped = SigninResult(
			account_key='test',
			account_name='Test',
			status=SigninStatus.SKIPPED
		)
		assert skipped.needs_notification is False


class TestSigninSummary:
	"""SigninSummary 测试"""

	def test_add_result(self):
		"""测试添加结果"""
		summary = SigninSummary()

		summary.add_result(SigninResult(
			account_key='test1',
			account_name='Test1',
			status=SigninStatus.SUCCESS
		))
		summary.add_result(SigninResult(
			account_key='test2',
			account_name='Test2',
			status=SigninStatus.FAILED
		))

		assert summary.total == 2
		assert summary.success == 1
		assert summary.failed == 1


class TestCooldownFunctions:
	"""冷却期相关函数测试"""

	def test_get_next_signin_time(self):
		"""测试计算下次签到时间"""
		now = datetime.now()
		next_time = get_next_signin_time(now)

		assert next_time is not None
		assert next_time > now

	def test_get_next_signin_time_none(self):
		"""测试无上次签到时间"""
		assert get_next_signin_time(None) is None

	def test_is_in_cooldown(self):
		"""测试冷却期检查"""
		now = datetime.now()

		# 刚刚签到，应该在冷却期内
		assert is_in_cooldown(now) is True

		# 25小时前签到，应该不在冷却期内
		old_time = now - timedelta(hours=25)
		assert is_in_cooldown(old_time) is False

		# 无签到记录
		assert is_in_cooldown(None) is False

	def test_format_time_remaining(self):
		"""测试剩余时间格式化"""
		# 无下次时间
		assert format_time_remaining(None) == '可以签到'

		# 已过时间
		past = datetime.now() - timedelta(hours=1)
		assert format_time_remaining(past) == '可以签到'

		# 未来时间
		future = datetime.now() + timedelta(hours=2, minutes=30)
		result = format_time_remaining(future)
		assert '小时' in result or '分钟' in result


class TestBalanceChange:
	"""余额变化分析测试"""

	def test_first_run(self):
		"""测试首次运行"""
		status, diff = analyze_balance_change(10.0, None, None)
		assert status == SigninStatus.FIRST_RUN
		assert diff is None

	def test_balance_increase(self):
		"""测试余额增加"""
		status, diff = analyze_balance_change(10.0, 9.0, None)
		assert status == SigninStatus.SUCCESS
		assert diff == 1.0

	def test_balance_unchanged_in_cooldown(self):
		"""测试冷却期内余额未变"""
		now = datetime.now()
		status, diff = analyze_balance_change(10.0, 10.0, now)
		assert status == SigninStatus.COOLDOWN
		assert diff == 0.0

	def test_balance_unchanged_not_in_cooldown(self):
		"""测试非冷却期余额未变

		注意：新逻辑下，余额不变返回 COOLDOWN，因为签到 API 可能已成功
		"""
		old_time = datetime.now() - timedelta(hours=25)
		status, diff = analyze_balance_change(10.0, 10.0, old_time)
		assert status == SigninStatus.COOLDOWN  # 余额不变 = 今日已签到
		assert diff == 0.0

	def test_balance_decrease(self):
		"""测试余额减少

		注意：新逻辑下，余额减少返回 COOLDOWN，因为签到 API 可能已成功
		余额减少可能是正常使用消耗，而非签到失败
		"""
		status, diff = analyze_balance_change(9.0, 10.0, None)
		assert status == SigninStatus.COOLDOWN  # 余额减少 = 正常消耗，签到可能成功
		assert diff == -1.0


class TestAtomicWrite:
	"""原子性写入测试"""

	def test_atomic_write_success(self):
		"""测试成功写入"""
		with tempfile.TemporaryDirectory() as tmpdir:
			file_path = os.path.join(tmpdir, 'test.txt')
			content = 'Hello, World!'

			_atomic_write(file_path, content)

			assert os.path.exists(file_path)
			with open(file_path, 'r', encoding='utf-8') as f:
				assert f.read() == content

	def test_atomic_write_overwrites(self):
		"""测试覆盖写入"""
		with tempfile.TemporaryDirectory() as tmpdir:
			file_path = os.path.join(tmpdir, 'test.txt')

			_atomic_write(file_path, 'First')
			_atomic_write(file_path, 'Second')

			with open(file_path, 'r', encoding='utf-8') as f:
				assert f.read() == 'Second'


class TestBalanceHash:
	"""余额 Hash 测试"""

	def test_generate_hash(self):
		"""测试生成 Hash"""
		balances = {'account_1': 10.0, 'account_2': 20.0}
		hash1 = generate_balance_hash(balances)

		assert len(hash1) == 16
		assert hash1.isalnum()

	def test_hash_consistency(self):
		"""测试 Hash 一致性"""
		balances = {'account_1': 10.0, 'account_2': 20.0}
		hash1 = generate_balance_hash(balances)
		hash2 = generate_balance_hash(balances)

		assert hash1 == hash2

	def test_hash_changes_with_data(self):
		"""测试数据变化导致 Hash 变化"""
		balances1 = {'account_1': 10.0}
		balances2 = {'account_1': 11.0}

		hash1 = generate_balance_hash(balances1)
		hash2 = generate_balance_hash(balances2)

		assert hash1 != hash2

	def test_empty_balances(self):
		"""测试空余额"""
		assert generate_balance_hash({}) == ''


class TestSigninHistory:
	"""签到历史管理测试"""

	def test_update_signin_history(self):
		"""测试更新签到历史"""
		history = {}
		now = datetime.now()
		new_record = SigninRecord(time=now, balance=10.0)

		result = SigninResult(
			account_key='test_account',
			account_name='Test',
			status=SigninStatus.SUCCESS,
			new_record=new_record
		)

		new_history = update_signin_history(history, [result])

		assert 'test_account' in new_history
		assert new_history['test_account'].balance == 10.0

	def test_update_preserves_original(self):
		"""测试更新不修改原字典"""
		now = datetime.now()
		original_record = SigninRecord(time=now, balance=5.0)
		history = {'existing': original_record}

		new_record = SigninRecord(time=now, balance=10.0)
		result = SigninResult(
			account_key='new_account',
			account_name='New',
			status=SigninStatus.SUCCESS,
			new_record=new_record
		)

		new_history = update_signin_history(history, [result])

		# 原字典不应被修改
		assert 'new_account' not in history
		# 新字典包含两个记录
		assert 'existing' in new_history
		assert 'new_account' in new_history
