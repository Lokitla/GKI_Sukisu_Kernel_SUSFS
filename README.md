# GKI_Sukisu_Kernel_SUSFS（融合版）

> 自动构建带 **SukiSU-Ultra(内置 SUSFS) + SUSFS + KPM** 的 GKI 内核。
> 本仓库在 **ShirkNeko/GKI_KernelSU_SUSFS**（Python 引擎版，隐藏栈完整）基础上融合了 **zzh20188/GKI_KernelSU_SUSFS** 的 GhostLock 安全修复链与"GKI 风格内核名/构建时间精确伪装"能力。

> ⚠️ 不支持一加 ColorOS 14/15 和非 GKI 设备。
> 默认构建目标建议：`android12 / 5.10 / sub 236 / 2025-05`（Matrix 已含此组合）。

---

## 0. 相对上游新增/改动

| 能力 | 来源 | 说明 |
|---|---|---|
| `--cve-2026-43499` | 融合 zzh `security_patch/` | GhostLock CVE-2026-43499/53163 rtmutex 修复链（幂等，按实际 SUBLEVEL 自动判断是否已含上游修复） |
| `--build-time "…UTC %Y"` | 融合 zzh | 固定构建日期 → `#1 SMP PREEMPT <时间>`；`N`/空 = 当前 UTC |
| `--custom-version` 精确化 | 重写 | 现在可生成**精确的 GKI 风格内核名**（见 §3.3），支持完整/后缀两种写法 |
| KPM / SUSFS 隐藏栈 / LZ4KD / BBG | 保留 shi | SukiSU builtin + `69_hide_stuff.patch` + KPM(5.x) + ZRAM LZ4KD + Baseband-guard 原样可用 |

---

## 1. 快速开始（GitHub Actions）

### 方式一：构建单个版本（推荐）
1. Actions → **Kernel Build** → Run workflow
2. 填 `Android 12 / Kernel 5.10 / Sub Level 236 / OS Patch 2025-05 / Revision r1`
3. 开关：ZRAM=✅、KPM=✅、BBG=✅、**CVE-2026-43499=✅**
4. （可选）`自定义版本名称` 填伪装名（见 §3.3），`自定义构建时间` 填如 `Tue Oct 21 03:03:12 UTC 2025`
5. 产物：`android12-5.10.236-2025-05-AnyKernel3.zip` / `*boot*.img` / 管理器 APK（`manager-apk` 产物）

> **默认值即推荐配置**：构建 SUSFS ✅ / SukiSU 分支=builtin（SUSFS 内置）✅ / KPM = enabled ✅ / LZ4KD ✅ / BBG ✅ / CVE 修复 ✅ / 管理器 SukiSU + Spoofed ✅ / Telegram ✅。
> 全部为勾选/下拉项，可随时关闭或切换：`SukiSU 分支`（**builtin**=SUSFS 内置源码 / main / dev）、`是否构建 SUSFS`、`KPM` 三态（关闭/开启/开启+修补镜像）、`CVE`、`LZ4KD`、`BBG`、`内核名伪装`、`构建时间伪装`、`BBR 默认`、`OnePlus 8E`（两两独立）、管理器来源（SukiSU/ReSukiSU）+ 是否 Spoofed。
> 完整矩阵 + **自动 Release** 在 **Build Kernels** 工作流（默认开），Release 会附带 Manager APK。

### 方式二：构建所有版本
Actions → **Build Kernels** → 设全局项 → 自动按 `matrix.json` 编 19 组合并打 Release。

## 2. 命令行本地构建

```bash
cd .github/workflows/scripts
pip install PyYAML

# 例：单组合（可含全部目标特性）
python build.py --android android12 --kernel 5.10 --sub-level 236 --os-patch 2025-05 \
    --revision r1 --zram --bbg --cve-2026-43499 \
    --custom-version=-android12-9-00003-gfb24cf99ad97-ab14313284 \
    --build-time "Tue Oct 21 03:03:12 UTC 2025" --dry-run

# 全矩阵 / 指定 commit / 工具命令
python build.py --all
python build.py --all --ksu-commit abc1234 --susfs-commit HEAD~1
python build.py --list-configs / --list-matrix
```

## 3. 命令行参数（全表）

| 参数 | 说明 | 默认 |
|---|---|---|
| `--android`,`-a` | android12/13/14/15 | android14 |
| `--kernel`,`-k` | 5.10/5.15/6.1/6.6 | 6.1 |
| `--sub-level`,`-s` | Sub level 或 X(LTS) | 124 |
| `--os-patch` | OS Patch 月 | 2025-02 |
| `--revision` | android12 认证 boot revision | - |
| `--ksu-version` | Stable(标准)/Dev(开发) | Stable |
| `--ksu-commit` | SukiSU commit | latest |
| `--susfs-commit` | SUSFS commit(hash/HEAD~N) | latest |
| `--zram` | ZRAM LZ4KD | False |
| `--no-kpm` | 关闭 KPM（含 defconfig 与镜像修补） | False |
| `--no-susfs` | **关闭 SUSFS（内核侧补丁/配置全停，SukiSU 自动回退 main）** | False |
| `--ksu-branch` | **SukiSU 分支 main/dev/builtin**（builtin=SUSFS 内置） | builtin |
| `--bbg` | Baseband-guard | False |
| `--op8e` | OnePlus 8E 支持（**与 BBR 相互独立**，非一加勿开） | False |
| `--bbr` | BBR 默认拥塞 | False |
| `--cve-2026-43499` | **GhostLock 修复链** | False(UI 默认勾选) |
| `--custom-version` | **伪装内核名后缀**（`-…` 或完整 `5.10.236-…`） | - |
| `--build-time` | **伪装构建日期**（`%a %b %d %H:%M:%S UTC %Y`） | 当前UTC |
| `--no-release` / `--matrix` / `--all` / `--dry-run` / `-w` | 常规 | - |

> ⚠️ 含前导 `-` 的 `--custom-version` 值请在 CLI 用 `--custom-version=-xxx`（等号形式）。

### 3.3 伪装语义（重要）

- `--custom-version="-android12-9-00003-gfb24cf99ad97-ab14313284"` → `uname -r` 精确输出 `5.10.236-android12-9-00003-gfb24cf99ad97-ab14313284`（会同时改写 setlocalversion 最终 echo 与 `CONFIG_LOCALVERSION`，并去掉 `-dirty`）。
- 也兼容传完整内核名 `--custom-version="5.10.236-android12-9-…"`（自动去掉版本号前缀）。
- 建议后缀直接抄**你设备官方 stock 内核**的 `uname -r` 尾部，隐藏效果最佳。
- `--build-time "Tue Oct 21 03:03:12 UTC 2025"` → uname -v 中 `#1 SMP PREEMPT Tue Oct 21 03:03:12 UTC 2025`（会校验星期/日期合法）。

## 4. 支持功能一览

| 功能 | 说明 |
|---|---|
| SukiSU-Ultra | 分支可选 `builtin`(默认, SUSFS 内置源码+inline hooks+magic mount) / `main` / `dev` |
| SUSFS | 全特性 spoof（UNAME/CMDLINE/KSTAT/SUS_MAP/OPEN_REDIRECT/HIDE_SYMBOLS…）+ `69_hide_stuff.patch` |
| GhostLock 修复 | CVE-2026-43499 + CVE-2026-53163 完整链（可选开关） |
| KPM | 5.x 内核 Image 修补（KernelPatch 模块支持）；6.6 不支持 |
| ZRAM LZ4KD | LZ4K/LZ4KD/842/lz4k_oplus 等（Scene 可切） |
| BBG | Baseband-guard 基带防格机 |
| 伪装 | 精确内核名 + 固定构建时间（见 §3.3） |
| 管理器 | SukiSU 构建产物自动带 Manager；需要伪装成官方 KernelSU 的 Manager 可运行 Actions → **Fetch SukiSU Manager (Spoofed)** 手动拉取 `Spoofed-Manager`/`Manager` APK |

## 5. 版本矩阵（matrix.json → 19 组合）

| Android | Kernel | Sub levels |
|---|---|---|
| 12 | 5.10 | 136/198/209/236/**X(LTS)** |
| 13 | 5.15 | 74/123/148/170/178/180 |
| 14 | 6.1 | 78/90/99/124/145 |
| 15 | 6.6 | 50/66/102 |

## 6. 紧急救援 / 兼容规则 / 构建架构

- 刷入错误内核开不了机：`fastboot flash boot <boot.img>`（详细见历史 README 段落或 Wiki）。
- 同一 GKI 主版本可向上刷子版本；LTS(X) 最新但不一定最稳。
- 引擎架构（继承 shi）：`build.py`(CLI) → `kernel_builder.py`(流水线) + `config.py`(常量/校验)；workflow 薄壳传参。补丁 = 拷贝+patch。
- 根目录新增 `security_patch/`（CVE 链，脚本由 `kernel_builder.apply_cve_2026_43499_fix()` 在编译前按需调用）。

---

*开发提示：修改引擎后先 `python3 -m py_compile *.py`；版本伪装改动请用 §2 dry-run 或 CI 中 `strings Image | grep 'Linux version'` 复核。*
