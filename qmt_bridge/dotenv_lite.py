# coding:utf-8
"""极简 .env 加载器：不装 `python-dotenv` 这个依赖。

py3.6 这套环境装包本来就容易踩镜像/编译的坑，`.env` 格式又简单到没必要为它
拉一个依赖——这里二三十行代码自己写更省事，功能也够用（`KEY=VALUE`、`#` 开头
当注释、值两边的引号会被去掉）。

用法
----
    from qmt_bridge.dotenv_lite import load_dotenv
    load_dotenv()                          # 默认找项目根目录下的 .env
    account = os.environ.get('QMT_ACCOUNT_ID', '')

真实账号、密码这类东西写进 `.env`，这个文件在 `.gitignore` 里，不会被提交。
换机器时把 `.env` 复制过去就行，不用在每台机器上重新敲设置环境变量的命令。
项目根目录下的 `.env.example` 是进 git 的模板，标着要填哪些键、不含真实值。

跟真的 OS 环境变量的关系：默认**不覆盖**已经存在的环境变量（`override=False`），
所以真在系统里设了 `QMT_ACCOUNT_ID` 的话，那个值优先，`.env` 只是补上没设置的。
"""
import os

_QUOTE_CHARS = ('"', "'")


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_dotenv(text):
    """纯解析，不碰 os.environ——方便单测。返回 dict，顺序不保证。"""
    out = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTE_CHARS:
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_dotenv(path=None, override=False):
    """读 `.env` 塞进 `os.environ`。文件不存在就什么都不做，返回 False——
    这是可选的本地便利，不是必需品，找不到不该报错，该报错的地方是后面
    `os.environ.get(...)` 读不到值时那一句。

    `path` 不传就用项目根目录下的 `.env`（相对这个文件的位置算，不受
    当前工作目录是哪儿影响——脚本从哪个目录被调用都能找到）。
    """
    path = path or os.path.join(_project_root(), '.env')
    if not os.path.exists(path):
        return False
    with open(path, 'r', encoding='utf-8') as f:
        parsed = parse_dotenv(f.read())
    for key, value in parsed.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return True
