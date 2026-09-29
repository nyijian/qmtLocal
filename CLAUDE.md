# CLAUDE.md

国金证券大QMT（`D:\国金证券QMT交易端`，内置 Python 3.6）的本地量化开发仓库。项目概览见 `README.md`，细节见 `docs/`。

## 两个环境，按任务选

```powershell
.venv313\Scripts\python.exe tools\market_overview.py                        # 数据分析、akshare：3.13
.venv\Scripts\python.exe -m qmt_mock.runner strategies\双均线示例.py ...      # QMT 策略开发、本地模拟、tests：3.6
```

- 从项目根目录运行。`tools/`、`tests/` 的脚本自己会把根目录加进 `sys.path`，新脚本照抄这个写法。
- 装包走清华镜像：`uv pip install --python .venv313\Scripts\python.exe -i https://pypi.tuna.tsinghua.edu.cn/simple ...`（官方 PyPI 会超时）。

## 硬性规则

1. **编码看文件在哪里运行**（docs/04-开发约定.md §2）：
   - `strategies/`、`qmt_client_scripts/`、`qmt_mock/` → GBK，首行 `# coding:gbk`。
   - `tools/`、`tests/`、`qmt_bridge/`、新的 `.md`、`requirements-data.txt` → **UTF-8 带 BOM**，CRLF 换行。
   - 用 Write 工具新建文件后要补 BOM（Write 写出来的是无 BOM 的 UTF-8）；VSCode 全局设了 GBK，没 BOM 的 UTF-8 文件会被用户存坏。
   - 改 GBK 文件时按 GBK 读写，不要用会改成 UTF-8 的方式。
2. **3.6 兼容**：`qmt_bridge/`、`qmt_client_scripts/`、`strategies/`、`qmt_mock/`、`tests/`、现有 `tools/` 不能用
   dataclasses、`:=`、`match`、`list[int]` 类型标注、`X | Y`。`.venv` 里是 pandas 0.22。只在 `.venv313` 跑的新脚本不受限，但要在文件头注明。
3. **本地不 import `xtquant`**，走 `qmt_bridge`（跟官方同签名）。全市场历史不走桥，用 `qmt_bridge.dat_reader`。
4. **不要改 `ALLOW_ORDER` 的默认值（False）**，不要替用户下单。
5. 账号等真实值只放 `.env`，新配置项同步到 `.env.example`（不写真实值）。
6. 改了 `桥接服务.py` 或 `qmt_bridge/` → 跑 `.venv\Scripts\python.exe tests\桥接自测.py`，要看到 `FAILURES: 0`。
   服务端改动要提醒用户重新粘贴进 QMT 并在「模型交易」里重新运行。

## 改完代码顺手维护文档

加脚本 → `docs/03-脚本清单.md`；踩坑 → `docs/05-踩坑记录.md`；重要取舍 → `docs/决策记录.md`（带日期）；
新规矩 → `docs/04-开发约定.md` 和本文件；做完路线图上的事 → `docs/路线图.md`。

## 环境事实

- 桥接服务：大QMT「模型交易」里运行，周期「分笔线」，TCP 127.0.0.1:58611。收盘后不响应，用 `--offline` / `dat_reader`。
- 本地日线缓存 `D:\国金证券QMT交易端\datadir\<SH|SZ>\86400\<6位代码>.DAT`，不自动更新，靠 QMT「补充数据」。
- 共享数据（akshare、因子、报告）在 OneDrive：`.env` 的 `QMTLOCAL_DATA_DIR`（本机 `C:\Users\nyiji\OneDrive\05_量化策略\data`），
  两台机器共用。新脚本读写共享数据一律用 `os.environ.get('QMTLOCAL_DATA_DIR') or <项目>\real_data` 取根目录，不要写死路径。
- QMT 接口签名参考 https://miniqmt.com/pages/api.html ；QMT 自带示例在 `D:\国金证券QMT交易端\python\`。
