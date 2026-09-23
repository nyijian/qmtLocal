# coding:utf-8
"""dotenv_lite 自测。

    .venv\\Scripts\\python.exe -m qmt_bridge.dotenv自测

不碰真实 .env、不碰真实环境变量——用临时文件和临时键名，跑完自己清理干净。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qmt_bridge.dotenv_lite import load_dotenv, parse_dotenv

failures = []


def check(name, cond, detail=''):
    print(('  OK   ' if cond else '  FAIL ') + name + ('' if cond else '  <- ' + str(detail)))
    if not cond:
        failures.append(name)


print('1) 纯解析（不碰 os.environ）')

sample = """
# 这是注释，应该被跳过
QMT_ACCOUNT_ID=00000000
QMT_ACCOUNT_TYPE = STOCK

带引号的值="hello world"
带单引号的值='hi there'
没有等号的行直接跳过
KEY_WITH_EMPTY=
"""
got = parse_dotenv(sample)
check('账号解出来了', got.get('QMT_ACCOUNT_ID') == '00000000', got)
check('等号两边空格被去掉', got.get('QMT_ACCOUNT_TYPE') == 'STOCK', got)
check('双引号被去掉', got.get('带引号的值') == 'hello world', got)
check('单引号被去掉', got.get('带单引号的值') == 'hi there', got)
check('空值也认', got.get('KEY_WITH_EMPTY') == '', got)
check('注释行没混进去', '# 这是注释，应该被跳过' not in got, got)
check('没有等号的行被跳过', '没有等号的行直接跳过' not in ''.join(got), got)
check('总共解出5个键（含1个空值）', len(got) == 5, got)


print()
print('2) load_dotenv：文件不存在')
check('不存在的文件返回 False', load_dotenv('这个文件肯定不存在.env') is False)


print()
print('3) load_dotenv：真的把值塞进 os.environ')

TEST_KEY = '_DOTENV_SELFTEST_KEY_'
os.environ.pop(TEST_KEY, None)   # 保证测试前是干净的

tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.env', delete=False, encoding='utf-8')
tmp.write('%s=第一次的值\n' % TEST_KEY)
tmp.close()

try:
    ret = load_dotenv(tmp.name)
    check('load_dotenv 返回 True', ret is True)
    check('值被塞进 os.environ', os.environ.get(TEST_KEY) == '第一次的值', os.environ.get(TEST_KEY))

    print()
    print('4) 默认不覆盖已存在的环境变量')
    os.environ[TEST_KEY] = '手动设置的值'
    with open(tmp.name, 'w', encoding='utf-8') as f:
        f.write('%s=文件里的新值\n' % TEST_KEY)
    load_dotenv(tmp.name)
    check('override=False 时不覆盖已有值', os.environ.get(TEST_KEY) == '手动设置的值', os.environ.get(TEST_KEY))

    print()
    print('5) override=True 时才覆盖')
    load_dotenv(tmp.name, override=True)
    check('override=True 时覆盖了', os.environ.get(TEST_KEY) == '文件里的新值', os.environ.get(TEST_KEY))
finally:
    os.unlink(tmp.name)
    os.environ.pop(TEST_KEY, None)


print()
print('6) 项目根目录下的 .env.example 存在，且不含真实账号')
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
example_path = os.path.join(root, '.env.example')
check('.env.example 存在', os.path.exists(example_path), example_path)
if os.path.exists(example_path):
    with open(example_path, 'r', encoding='utf-8') as f:
        content = f.read()
    check('.env.example 里没有写成一串纯数字的样子（真账号就是纯数字）',
          not any(c.isdigit() for c in content.split('QMT_ACCOUNT_ID=', 1)[-1].splitlines()[0]),
          '模板里 QMT_ACCOUNT_ID 后面不该跟着数字，应该是"你的资金账号"这种占位字样')

print()
print('FAILURES: %d' % len(failures))
sys.exit(1 if failures else 0)
