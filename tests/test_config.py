#!/usr/bin/env python3
"""
配置文件测试脚本 - 验证配置是否正确
不会真实运行签到，只检查配置格式
"""

import os
import sys

# 添加项目根目录到 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

# 加载.env文件
load_dotenv()

from utils.config import AppConfig, load_accounts_config

def test_config():
    """测试配置是否正确"""
    print('=' * 60)
    print('AnyRouter 签到脚本 - 配置测试')
    print('=' * 60)
    print()

    # 测试Provider配置
    print('[1/3] 正在加载 Provider 配置...')
    try:
        app_config = AppConfig.load_from_env()
        print(f'[成功] 成功加载 {len(app_config.providers)} 个 provider 配置')
        for name, provider in app_config.providers.items():
            print(f'  - {name}: {provider.domain}')
        print()
    except Exception as e:
        print(f'[失败] Provider 配置加载失败: {e}')
        sys.exit(1)

    # 测试账号配置
    print('[2/3] 正在加载账号配置...')
    try:
        accounts = load_accounts_config()
        if not accounts:
            print('[失败] 未找到账号配置')
            sys.exit(1)

        print(f'[成功] 成功加载 {len(accounts)} 个账号配置')
        for i, account in enumerate(accounts):
            account_name = account.get_display_name(i)
            provider = account.provider
            api_user = account.api_user
            session_preview = '***' + str(account.cookies.get('session', ''))[-8:] if isinstance(account.cookies, dict) else '***'
            print(f'  - {account_name}:')
            print(f'      Provider: {provider}')
            print(f'      API User: {api_user}')
            print(f'      Session: {session_preview}')
        print()
    except Exception as e:
        print(f'[失败] 账号配置加载失败: {e}')
        sys.exit(1)

    # 配置摘要
    print('[3/3] 配置摘要')
    print('=' * 60)
    print(f'Provider 数量: {len(app_config.providers)}')
    print(f'账号数量: {len(accounts)}')
    print()

    print('[成功] 所有配置验证通过！')
    print()
    print('=' * 60)
    print('下一步操作：')
    print('=' * 60)
    print('1. 手动测试签到：')
    print('   uv run checkin.py')
    print()
    print('2. 设置定时任务（右键"以管理员身份运行"）：')
    print('   scripts\\setup_task.bat')
    print()
    print('注意：首次运行会启动浏览器获取 WAF cookies，这是正常的！')
    print('=' * 60)

if __name__ == '__main__':
    try:
        test_config()
    except KeyboardInterrupt:
        print('\n\n[失败] 测试被用户中断')
        sys.exit(1)
    except Exception as e:
        print(f'\n\n[失败] 测试失败: {e}')
        import traceback
        traceback.print_exc()
        sys.exit(1)
