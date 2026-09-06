import os
import re
import datetime
import subprocess
import logging
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field

from config import (BuildConfig, KSU_REPO_CONFIG, SUSFS_REPO_CONFIG, SUKISU_PATCH_REPO_CONFIG,
                   ANYKERNEL_CONFIG, KERNEL_PATCHES_CONFIG, BBG_CONFIG, TOOLCHAIN_CONFIG,
                   LEGACY_FIXES, OP8E_PATCH_URL, KPM_PATCH_URL)

# 仓库根目录（本文件位于 <root>/.github/workflows/scripts/ 下）
REPO_ROOT = Path(__file__).resolve().parents[3]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class BuildResult:
    success: bool
    config: BuildConfig
    message: str = ""
    artifacts: list = field(default_factory=list)
    build_time: Optional[float] = None


class ShellCommand:
    def __init__(self, cwd: Optional[str] = None, env: Optional[dict] = None):
        self.cwd = cwd
        self.env = env or os.environ.copy()

    def run(self, cmd: str, check: bool = True, capture_output: bool = False,
            shell: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        logger.info(f"执行命令: {cmd}")
        try:
            return subprocess.run(cmd, shell=shell, cwd=self.cwd, env=self.env,
                                capture_output=capture_output, text=True, timeout=timeout, check=check)
        except subprocess.CalledProcessError as e:
            logger.error(f"命令执行失败: {e.stderr or str(e)}")
            raise
        except subprocess.TimeoutExpired:
            logger.error(f"命令执行超时: {cmd}")
            raise

    def run_with_callback(self, cmd: str, callback: Optional[Callable] = None) -> str:
        logger.info(f"执行命令: {cmd}")
        process = subprocess.Popen(cmd, shell=True, cwd=self.cwd, env=self.env,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        output_lines = []
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            if callback:
                callback(line)
        process.wait()
        if process.returncode != 0:
            raise RuntimeError(f"命令执行失败")
        return "\n".join(output_lines)


class KernelBuilder:
    ZRAM_CONFIG_5_10 = "CONFIG_ZSMALLOC=y\nCONFIG_ZRAM=y\nCONFIG_MODULE_SIG=n\nCONFIG_CRYPTO_LZO=y\nCONFIG_ZRAM_DEF_COMP_LZ4KD=y\n"
    ZRAM_CONFIG_COMMON = "CONFIG_CRYPTO_LZ4HC=y\nCONFIG_CRYPTO_LZ4K=y\nCONFIG_CRYPTO_LZ4KD=y\nCONFIG_CRYPTO_842=y\nCONFIG_CRYPTO_LZ4K_OPLUS=y\nCONFIG_ZRAM_WRITEBACK=y\n"

    def __init__(self, config: BuildConfig, workspace: str):
        self.config = config
        self.workspace = Path(workspace)
        self.shell = ShellCommand(cwd=workspace)
        self.env = os.environ.copy()
        self.work_dir = self.workspace / config.config_name
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.susfs_dir = self.workspace / "susfs4ksu"
        self.sukisu_patch_dir = self.workspace / "SukiSU_patch"
        self.anykernel_dir = self.workspace / "AnyKernel3"
        self.kernel_patches_dir = self.workspace / "kernel_patches"
        self.toolchain_dir = self.workspace / "toolchain"
        self.mkbootimg_dir = self.workspace / "mkbootimg"
        self._setup_env()

    def _setup_env(self):
        self.env["CONFIG"] = self.config.config_name
        self.env["CCACHE_COMPILERCHECK"] = "%compiler% -dumpmachine; %compiler% -dumpversion"
        self.env["CCACHE_NOHASHDIR"] = "true"
        self.env["CCACHE_HARDLINK"] = "true"
        self.shell.env = self.env

    def _run_cmd(self, cmd: str, **kwargs) -> subprocess.CompletedProcess:
        return self.shell.run(cmd, **kwargs)

    def _chdir(self, path: Path):
        os.chdir(path)
        self.shell.cwd = str(path)

    def _resolve_sukisu_ref(self) -> str:
        """最终传给 SukiSU setup.sh 的 checkout ref。
        优先级：kernelsu_commit > kernelsu_branch(main/dev/builtin，默认 builtin=SUSFS 内置)。
        关闭 SUSFS 时 builtin 自动回退 main（无内置源码可编）。"""
        ref = self.config.kernelsu_commit or self.config.kernelsu_branch or "builtin"
        if not self.config.enable_susfs and ref == "builtin":
            return "main"
        return ref

    def _kernel_config_text(self) -> str:
        """按开关生成追加到 gki_defconfig 的配置块（解耦：KPM / SUSFS 可独立开关）"""
        lines = [""]
        lines.append("# === KernelSU Config ===")
        lines.append("CONFIG_KSU=y")
        if self.config.use_kpm:
            lines.append("CONFIG_KPM=y")
        if self.config.enable_susfs:
            lines.append("CONFIG_KSU_SUSFS_SUS_SU=n")

        lines.append("")
        lines.append("# === TMPFS Config ===")
        lines.append("CONFIG_TMPFS_XATTR=y")
        lines.append("CONFIG_TMPFS_POSIX_ACL=y")

        lines.append("")
        lines.append("# === Network Config ===")
        lines.append("CONFIG_IP_NF_TARGET_TTL=y")
        lines.append("CONFIG_IP6_NF_TARGET_HL=y")
        lines.append("CONFIG_IP6_NF_MATCH_HL=y")

        lines.append("")
        lines.append("# === BBR Config ===")
        lines.append("CONFIG_TCP_CONG_ADVANCED=y")
        lines.append("CONFIG_TCP_CONG_BBR=y")
        lines.append("CONFIG_NET_SCH_FQ=y")
        lines.append("CONFIG_TCP_CONG_BIC=n")
        lines.append("CONFIG_TCP_CONG_WESTWOOD=n")
        lines.append("CONFIG_TCP_CONG_HTCP=n")

        if self.config.enable_susfs:
            lines.append("")
            lines.append("# === SUSFS Config ===")
            lines.append("CONFIG_KSU_SUSFS=y")
            lines.append("CONFIG_KSU_SUSFS_SUS_MAP=y")
            lines.append("CONFIG_KSU_SUSFS_SUS_MOUNT=y")
            lines.append("CONFIG_KSU_SUSFS_AUTO_ADD_SUS_KSU_DEFAULT_MOUNT=y")
            lines.append("CONFIG_KSU_SUSFS_AUTO_ADD_SUS_BIND_MOUNT=y")
            lines.append("CONFIG_KSU_SUSFS_SUS_KSTAT=y")
            lines.append("CONFIG_KSU_SUSFS_TRY_UMOUNT=y")
            lines.append("CONFIG_KSU_SUSFS_AUTO_ADD_TRY_UMOUNT_FOR_BIND_MOUNT=y")
            lines.append("CONFIG_KSU_SUSFS_SPOOF_UNAME=y")
            lines.append("CONFIG_KSU_SUSFS_ENABLE_LOG=y")
            lines.append("CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y")
            lines.append("CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y")
            lines.append("CONFIG_KSU_SUSFS_OPEN_REDIRECT=y")
            lines.append("CONFIG_KSU_SUSFS_SUS_PATH=y" if self.config.kernel_version != "6.6"
                         else "CONFIG_KSU_SUSFS_SUS_PATH=n")
        return "\n".join(lines) + "\n"

    def _apply_susfs_commit(self):
        if not self.config.susfs_commit or not self.susfs_dir.exists():
            return
        self._chdir(self.susfs_dir)
        if self.config.susfs_commit.startswith("HEAD~"):
            self._run_cmd("git fetch origin", check=False)
            self._run_cmd(f"git reset --hard {self.config.susfs_commit}", check=False)
        else:
            self._run_cmd("git fetch origin", check=False)
            self._run_cmd(f"git checkout {self.config.susfs_commit}", check=False)
        self._chdir(self.workspace)

    def clone_repositories(self):
        logger.info("=== 开始克隆仓库 ===")
        for name, repo_dir, url, branch in [
            ("SUSFS", self.susfs_dir, SUSFS_REPO_CONFIG['repo_url'], self.config.kernel_branch),
            ("SukiSU Patch", self.sukisu_patch_dir, SUKISU_PATCH_REPO_CONFIG['repo_url'], None),
            ("AnyKernel3", self.anykernel_dir, ANYKERNEL_CONFIG['repo_url'], ANYKERNEL_CONFIG['branch']),
            ("Kernel Patches", self.kernel_patches_dir, KERNEL_PATCHES_CONFIG['repo_url'], None),
        ]:
            if not repo_dir.exists():
                cmd = f"git clone {url}"
                if branch:
                    cmd += f" -b {branch}"
                logger.info(f"克隆 {name}...")
                self._run_cmd(cmd, check=False)
            else:
                logger.info(f"{name} 已存在，跳过")
        self._apply_susfs_commit()
        logger.info("=== 仓库克隆完成 ===")

    def clone_toolchain(self):
        logger.info("=== 克隆工具链 ===")
        if not self.toolchain_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/kernel/prebuilts/build-tools "
                         f"-b {TOOLCHAIN_CONFIG['build_tools_branch']} --depth 1 {self.toolchain_dir}", check=False)
        if not self.mkbootimg_dir.exists():
            self._run_cmd(f"git clone {TOOLCHAIN_CONFIG['aosp_mirror']}/platform/system/tools/mkbootimg "
                         f"-b {TOOLCHAIN_CONFIG['mkbootimg_branch']} --depth 1 {self.mkbootimg_dir}", check=False)
        self.env["AVBTOOL"] = str(self.toolchain_dir / "linux-x86/bin/avbtool")
        self.env["MKBOOTIMG"] = str(self.mkbootimg_dir / "mkbootimg.py")
        self.env["UNPACK_BOOTIMG"] = str(self.mkbootimg_dir / "unpack_bootimg.py")

        # AVB 签名密钥：优先用工作流已生成的 BOOT_SIGN_KEY_PATH（可为 secret 或现签），
        # 否则在本地 workspace 下现生成测试密钥，保证 boot.img 签名步骤不会因空 key 坏掉。
        key_dir = self.toolchain_dir / "linux-x86/bin"
        key_dir.mkdir(parents=True, exist_ok=True)
        key_path = key_dir / "testkey.pem"
        env_key_path = os.environ.get("BOOT_SIGN_KEY_PATH", "").strip()
        if env_key_path and Path(env_key_path).exists() and Path(env_key_path).stat().st_size > 0:
            self.env["BOOT_SIGN_KEY_PATH"] = env_key_path
        else:
            key_ok = key_path.exists() and key_path.stat().st_size > 0
            if not key_ok:
                secret_key = os.environ.get("BOOT_SIGN_KEY", "").strip()
                if secret_key:
                    key_path.write_text(secret_key, encoding="utf-8")
                    logger.info("已写入 BOOT_SIGN_KEY 到 AVB 测试密钥")
                else:
                    self._run_cmd(
                        f"openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 > {key_path}",
                        check=False)
                    logger.info("未提供 BOOT_SIGN_KEY，已现场生成 AVB 测试密钥")
            self.env["BOOT_SIGN_KEY_PATH"] = str(key_path)
        self.shell.env = self.env
        logger.info("=== 工具链准备完成 ===")

    def setup_repo_tool(self):
        logger.info("=== 安装 repo 工具 ===")
        repo_dir = self.workspace / "git-repo"
        repo_dir.mkdir(exist_ok=True)
        repo_path = repo_dir / "repo"
        if not repo_path.exists():
            self._run_cmd(f"curl https://storage.googleapis.com/git-repo-downloads/repo > {repo_path}", check=False)
            self._run_cmd(f"chmod a+rx {repo_path}", check=False)
        self.env["REPO"] = str(repo_path)
        self.shell.env = self.env

    def init_and_sync_kernel(self):
        logger.info("=== 初始化和同步内核源代码 ===")
        self._chdir(self.work_dir)
        formatted_branch = self.config.formatted_branch

        self._run_cmd(f"$REPO init --depth=1 -u https://android.googlesource.com/kernel/manifest "
                     f"-b common-{formatted_branch} --repo-rev=v2.16", check=False)

        remote = subprocess.run(f"git ls-remote https://android.googlesource.com/kernel/common {formatted_branch}",
                               shell=True, capture_output=True, text=True).stdout.strip()
        if "deprecated" in remote:
            manifest_path = self.work_dir / ".repo/manifests/default.xml"
            with open(manifest_path, "r") as f:
                content = f.read()
            content = content.replace(f'"{formatted_branch}"', f'"deprecated/{formatted_branch}"')
            with open(manifest_path, "w") as f:
                f.write(content)

        self.env["REMOTE_BRANCH"] = remote
        logger.info("同步内核源代码...")
        self._run_cmd("$REPO --trace sync -c -j$(nproc --all) --no-tags --fail-fast", check=False)

        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            raise RuntimeError("repo sync 失败，common 目录不存在")
        self._apply_legacy_fixes(remote)
        logger.info("=== 内核源代码同步完成 ===")

    def _apply_legacy_fixes(self, remote_branch: str = ""):
        av, kv = self.config.android_version, self.config.kernel_version
        sub = self.config.get_sub_level_int()
        is_deprecated = "deprecated" in remote_branch

        if is_deprecated and av == "android13" and kv == "5.15" and sub and sub < 123:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android13-5.15-below-123']['url']} -o fix.patch && patch -p1 < fix.patch", check=False)
            self._chdir(self.work_dir)

        if av == "android12" and kv == "5.10" and sub and sub < 136:
            common_dir = self.work_dir / "common"
            self._chdir(common_dir)
            self._run_cmd(f"curl -LSs {LEGACY_FIXES['android12-5.10-below-136']['url']} | patch -p1", check=False)
            self._chdir(self.work_dir)

    def add_kernel_supatch(self):
        if not self.config.support_op8e:
            return
        logger.info("=== 添加 OnePlus 8E 支持补丁 ===")
        drivers_dir = self.work_dir / "common/drivers"
        if not drivers_dir.exists():
            return
        self._chdir(drivers_dir)
        self._run_cmd(f"curl -LSs {OP8E_PATCH_URL} -o hmbird_patch.c", check=False)
        if (drivers_dir / "hmbird_patch.c").exists():
            with open(drivers_dir / "Makefile", "a") as f:
                f.write("obj-y += hmbird_patch.o\n")

    def add_kernelsu(self):
        logger.info("=== 添加 KernelSU ===")
        self._chdir(self.work_dir)
        ref = self._resolve_sukisu_ref()
        logger.info(f"SukiSU ref: {ref}")
        # setup.sh 会把 KernelSU 仓库 checkout 到该 ref（builtin 分支 = SUSFS 内置源码）
        self._run_cmd(f"curl -LSs {KSU_REPO_CONFIG['setup_script']} | bash -s {ref}", check=False)

    def add_bbg(self):
        if not self.config.use_bbg:
            return
        logger.info("=== 添加 Baseband-guard ===")
        common_dir = self.work_dir / "common"
        if not common_dir.exists():
            return
        self._chdir(common_dir)
        self._run_cmd(f"wget -O- {BBG_CONFIG['setup_script']} | bash", check=False)
        config_file = common_dir / "arch/arm64/configs/gki_defconfig"
        if config_file.exists():
            with open(config_file, "a") as f:
                f.write("CONFIG_BBG=y\n")
        kconfig_file = common_dir / "security/Kconfig"
        if kconfig_file.exists():
            self._add_baseband_guard_to_lsm_default(kconfig_file)

    def _add_baseband_guard_to_lsm_default(self, kconfig_file: Path):
        """把 baseband_guard 注入 security/Kconfig 中 config LSM 块的 default 行。

        只精确替换该块内的 default 行（在 selinux 后追加 ,baseband_guard），
        绝不重写其它内容 —— 与 zzh20188 build.yml 的 sed 逻辑一致，
        避免旧实现里 DOTALL 正则把整个 LSM/help 块吃坏导致 Kconfig 语法错误。
        """
        try:
            with open(kconfig_file, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError:
            return

        start = None
        for i, line in enumerate(lines):
            if re.match(r'^[ \t]*config[ \t]+LSM[ \t]*$', line):
                start = i
                break
        if start is None:
            return

        # 定位到块内（到下一个 config 为止）的所有 default 行并逐一注入 baseband_guard
        changed = False
        for i in range(start + 1, len(lines)):
            if re.match(r'^[ \t]*config[ \t]+', lines[i]):
                break
            line = lines[i]
            if re.match(r'^[ \t]*default[ \t]', line) and "baseband_guard" not in line \
                    and "selinux" in line:
                lines[i] = line.replace("selinux", "selinux,baseband_guard", 1)
                changed = True

        if changed:
            with open(kconfig_file, "w", encoding="utf-8") as f:
                f.writelines(lines)
            logger.info("security/Kconfig: config LSM default 已加入 baseband_guard")

    def apply_susfs_patches(self):
        if not self.config.enable_susfs:
            logger.info("=== 跳过 SUSFS 补丁（enable_susfs=false）===")
            return
        logger.info("=== 应用 SUSFS 补丁 ===")
        self._chdir(self.work_dir)
        common_dir = self.work_dir / "common"
        susfs_patch = self.susfs_dir / "kernel_patches" / self.config.get_susfs_patch_filename()
        if susfs_patch.exists():
            self._run_cmd(f"cp {susfs_patch} {common_dir}/", check=False)
        for src, dst in [
            (self.susfs_dir / "kernel_patches/fs", common_dir / "fs/"),
            (self.susfs_dir / "kernel_patches/include/linux", common_dir / "include/linux/"),
        ]:
            if src.exists():
                self._run_cmd(f"cp -r {src}/* {dst}", check=False)
        if susfs_patch.exists():
            patch_file = common_dir / self.config.get_susfs_patch_filename()
            if patch_file.exists():
                self._chdir(common_dir)
                self._run_cmd(f"patch -p1 --fuzz=3 < {patch_file}", check=False)
                self._chdir(self.work_dir)

    def apply_sukisu_patches(self):
        if not self.config.enable_susfs:
            logger.info("=== 跳过 SukiSU 补丁（69_hide_stuff，enable_susfs=false）===")
            return
        logger.info("=== 应用 SukiSU 补丁 ===")
        self._chdir(self.work_dir / "common")
        hooks_patch = self.sukisu_patch_dir / "69_hide_stuff.patch"
        if hooks_patch.exists():
            self._run_cmd(f"cp {hooks_patch} . && patch -p1 -F 3 < 69_hide_stuff.patch", check=False)

    def apply_zram_patches(self):
        if not self.config.use_zram:
            return
        logger.info("=== 应用 ZRAM (LZ4KD) 补丁 ===")
        self._chdir(self.work_dir / "common")
        for src in [
            (self.sukisu_patch_dir / "other/zram/lz4k/include/linux", "include/linux/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/lib", "lib/"),
            (self.sukisu_patch_dir / "other/zram/lz4k/crypto", "crypto/"),
            (self.sukisu_patch_dir / "other/zram/lz4k_oplus", "lib/"),
        ]:
            if src[0].exists():
                self._run_cmd(f"cp -r {src[0]}/* {src[1]}", check=False)
        zram_patch_dir = self.sukisu_patch_dir / f"other/zram/zram_patch/{self.config.kernel_version}"
        for patch in ["lz4kd.patch", "lz4k_oplus.patch"]:
            p = zram_patch_dir / patch
            if p.exists():
                self._run_cmd(f"patch -p1 -F 3 < {p}", check=False)

    def apply_task_mmu_fixes(self):
        if not self.config.enable_susfs:
            logger.info("=== 跳过 task_mmu 修复（enable_susfs=false）===")
            return
        logger.info("=== 应用 task_mmu.c 修复 ===")
        self._chdir(self.work_dir / "common")
        task_mmu = Path("fs/proc/task_mmu.c")
        if not task_mmu.exists():
            return

        # 读取文件内容
        with open(task_mmu, "r") as f:
            content = f.read()

        # ===== 修复 VMA_PAD_START 未定义 =====
        if "VMA_PAD_START" in content and "#define VMA_PAD_START" not in content:
            logger.info("检测到 VMA_PAD_START 未定义，正在添加宏定义...")
            # 在 #include <linux/pkeys.h> 之后添加定义
            lines = content.split('\n')
            new_lines = []
            inserted = False
            for line in lines:
                new_lines.append(line)
                if not inserted and line.strip().startswith('#include <linux/pkeys.h>'):
                    new_lines.append('')
                    new_lines.append('// VMA_PAD_START fix for SUSFS')
                    new_lines.append('#ifndef VMA_PAD_START')
                    new_lines.append('#define VMA_PAD_START(vma) ((vma)->vm_end)')
                    new_lines.append('#endif')
                    inserted = True
            if inserted:
                content = '\n'.join(new_lines)
                logger.info("已添加 VMA_PAD_START 宏定义")

        # ===== 修复 dentry 未初始化（如果还存在） =====
        if "struct dentry *dentry;" in content:
            content = content.replace("struct dentry *dentry;", "struct dentry *dentry = NULL;")
            logger.info("已修复 dentry 未初始化问题")

        # ===== 写入修改 =====
        with open(task_mmu, "w") as f:
            f.write(content)

        # ===== 原有的修复逻辑 =====
        fb = f"{self.config.android_version}-{self.config.kernel_version}"
        
        if fb == "android15-6.6" and "unsigned int nr_subpages" not in content:
            self._fix_base_c_header()
        elif fb == "android14-6.1" and "if (!vma_pages(vma))" not in content:
            self._fix_base_c_header()
            # 重新读取内容，因为可能已被修改
            with open(task_mmu, "r") as f:
                content = f.read()
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)
        elif fb in ["android12-5.10", "android13-5.10", "android13-5.15"] and "if (!vma_pages(vma))" not in content:
            with open(task_mmu, "r") as f:
                content = f.read()
            if "goto show_pad;" in content:
                content = content.replace("goto show_pad;", "return 0;")
                with open(task_mmu, "w") as f:
                    f.write(content)

    def _fix_base_c_header(self):
        base_c = self.work_dir / "common/fs/proc/base.c"
        if not base_c.exists():
            return
        with open(base_c, "r") as f:
            content = f.read()
        if "#include <linux/dma-buf.h>" not in content:
            content = content.replace("#include <linux/cpufreq_times.h>",
                                    "#include <linux/cpufreq_times.h>\n#include <linux/dma-buf.h>")
            with open(base_c, "w") as f:
                f.write(content)

    def _read_actual_sublevel(self, common_dir: Path) -> str:
        makefile = common_dir / "Makefile"
        try:
            if makefile.exists():
                m = re.search(r'^SUBLEVEL\s*=\s*(\d+)', makefile.read_text(), re.MULTILINE)
                if m:
                    return m.group(1)
        except OSError:
            pass
        return str(self.config.get_sub_level_int() or 99999)

    def apply_cve_2026_43499_fix(self):
        if not self.config.cve_2026_43499:
            return
        script = REPO_ROOT / "security_patch" / "apply_cve_2026_43499.sh"
        common_dir = self.work_dir / "common"
        if not script.exists():
            logger.warning(f"CVE 修复脚本不存在，跳过: {script}")
            return
        if not common_dir.exists():
            logger.warning("common 目录不存在，跳过 CVE 修复")
            return
        sub = self._read_actual_sublevel(common_dir)
        logger.info(f"=== 应用 GhostLock CVE-2026-43499/53163 修复链 (kernel {self.config.kernel_version}.{sub}) ===")
        self._chdir(common_dir)
        result = self._run_cmd(f"bash {script} {self.config.kernel_version} {sub} {REPO_ROOT / 'security_patch'}",
                      check=False)
        self._chdir(self.work_dir)
        if result.returncode != 0:
            raise RuntimeError(f"CVE-2026-43499 修复链应用失败 (exit {result.returncode})，请查看上方输出")

    def configure_kernel(self):
        logger.info("=== 配置内核 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return

        with open(config_file, "a") as f:
            f.write(self._kernel_config_text())

        if self.config.use_zram:
            self._configure_zram()
            self._configure_bazel()

        if self.config.set_default_bbr:
            with open(config_file, "a") as f:
                f.write("CONFIG_DEFAULT_BBR=y\n")

        build_config = self.work_dir / "common/build.config.gki"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("check_defconfig", "")
            with open(build_config, "w") as f:
                f.write(content)

    def _configure_zram(self):
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "r") as f:
            content = f.read()
        kv = self.config.kernel_version
        if kv == "5.10":
            with open(config_file, "a") as f:
                f.write(self.ZRAM_CONFIG_5_10)
        else:
            content = content.replace("CONFIG_ZRAM=m", "CONFIG_ZRAM=y")
            with open(config_file, "w") as f:
                f.write(content)
            with open(config_file, "a") as f:
                f.write("CONFIG_ZSMALLOC=y\n")
        with open(config_file, "a") as f:
            f.write(self.ZRAM_CONFIG_COMMON)

    def _configure_bazel(self):
        modules_bzl = self.work_dir / "common/modules.bzl"
        if modules_bzl.exists():
            with open(modules_bzl, "r") as f:
                content = f.read()
            modified = False
            for old in ['"drivers/block/zram/zram.ko",\n', '"drivers/block/zram/zram.ko",',
                       '"mm/zsmalloc.ko",\n', '"mm/zsmalloc.ko",']:
                if old in content:
                    content = content.replace(old, '')
                    modified = True
            if modified:
                with open(modules_bzl, "w") as f:
                    f.write(content)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        with open(config_file, "a") as f:
            f.write("CONFIG_MODULE_SIG_FORCE=n\n")

    def _resolve_build_time(self) -> str:
        """固定构建时间伪装；N/空 = 当前 UTC。格式: %a %b %d %H:%M:%S UTC %Y"""
        raw = (self.config.build_time or "").strip()
        if raw and raw.upper() != "N":
            try:
                parsed = datetime.datetime.strptime(raw, "%a %b %d %H:%M:%S UTC %Y")
                return parsed.strftime("%a %b %d %H:%M:%S UTC %Y")
            except ValueError:
                logger.warning(
                    f"build_time 格式无效({raw!r})，退回当前 UTC。期望格式如 'Tue Oct 21 03:03:12 UTC 2025'")
        return datetime.datetime.utcnow().strftime("%a %b %d %H:%M:%S UTC %Y")

    def configure_kernel_name(self):
        logger.info("=== 配置内核名称 ===")
        self._chdir(self.work_dir)
        MAX_CUSTOM_LEN = 64
        # 生成精确的内核 LOCALVERSION 后缀，如 "-android12-9-00003-gfb24cf99ad97-ab14313284"
        custom_suffix = ""
        if self.config.custom_version:
            v = self.config.custom_version.strip()
            v = re.sub(r'^[0-9]+\.[0-9]+\.[0-9]+', '', v)  # 兼容传入完整 "5.10.236-…" 形式
            if v and v[0] in "-+":
                v = v[1:]
            v = v.rstrip('-')[:MAX_CUSTOM_LEN]
            if v:
                custom_suffix = "-" + v

        setlocalversion = self.work_dir / "common/scripts/setlocalversion"
        if setlocalversion.exists():
            with open(setlocalversion, "r") as f:
                content = f.read()
            if custom_suffix:
                # 5.x 经典形态：把最终 echo "$res" 替换为固定后缀 → 精确控制内核名
                new_content = re.sub(
                    r'^(\s*)echo "\$res"',
                    lambda m: f'{m.group(1)}echo "{custom_suffix}"',
                    content, count=1, flags=re.MULTILINE)
                # 6.x 形态：整串 echo（含 KERNELVERSION/file_localversion/…）同样改写
                new_content = re.sub(
                    r'echo "\$\{KERNELVERSION\}\$\{file_localversion\}\$\{config_localversion\}\$\{LOCALVERSION\}\$\{scm_version\}"',
                    f'echo "${{KERNELVERSION}}{custom_suffix}"',
                    new_content)
                with open(setlocalversion, "w") as f:
                    f.write(new_content)
                content = new_content
                # 兜底：defconfig 的 CONFIG_LOCALVERSION 也写同一后缀
                config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
                if config_file.exists():
                    cfg = config_file.read_text()
                    cfg = re.sub(r'^CONFIG_LOCALVERSION=".*"$',
                                 f'CONFIG_LOCALVERSION="{custom_suffix}"', cfg, flags=re.MULTILINE)
                    config_file.write_text(cfg)
            if "-dirty" in content:
                content = content.replace("-dirty", "")
                with open(setlocalversion, "w") as f:
                    f.write(content)

        current_time = self._resolve_build_time()
        mkcompile_h = self.work_dir / "common/scripts/mkcompile_h"
        if mkcompile_h.exists():
            with open(mkcompile_h, "r") as f:
                content = f.read()
            content = content.replace('UTS_VERSION="$(echo $UTS_VERSION $CONFIG_FLAGS $TIMESTAMP | cut -b -$UTS_LEN)"',
                                    f'UTS_VERSION="#1 SMP PREEMPT {current_time}"')
            with open(mkcompile_h, "w") as f:
                f.write(content)

        if self.config.kernel_version in ["6.1", "6.6"]:
            init_makefile = self.work_dir / "common/init/Makefile"
            if init_makefile.exists():
                with open(init_makefile, "r") as f:
                    content = f.read()
                content = content.replace('$(preempt-flag-y) "$(build-timestamp)"', f'$(preempt-flag-y) "{current_time}"')
                with open(init_makefile, "w") as f:
                    f.write(content)

        if not (self.work_dir / "build/build.sh").exists():
            bazel_build = self.work_dir / "common/BUILD.bazel"
            if bazel_build.exists():
                with open(bazel_build, "r") as f:
                    content = f.read()
                lines = [l for l in content.split('\n') if '"protected_exports_list"' not in l or 'android/abi_gki_protected_exports_aarch64' not in l]
                with open(bazel_build, "w") as f:
                    f.write('\n'.join(lines))

            abi_path = self.work_dir / "common/android/abi_gki_protected_exports_aarch64"
            if abi_path.exists():
                import shutil
                try:
                    if abi_path.is_dir():
                        shutil.rmtree(abi_path)
                    else:
                        abi_path.unlink()
                except Exception:
                    pass

            stamp_bzl = self.work_dir / "build/kernel/kleaf/impl/stamp.bzl"
            if stamp_bzl.exists():
                with open(stamp_bzl, "r") as f:
                    content = f.read()
                content = content.replace("-maybe-dirty", "")
                with open(stamp_bzl, "w") as f:
                    f.write(content)
            # CONFIG_LOCALVERSION 已在 configure_kernel_name 中与 setlocalversion 一并写死（custom_suffix）

    def show_kernel_config(self):
        logger.info("=== 显示内核配置列表 ===")
        self._chdir(self.work_dir)
        config_file = self.work_dir / "common/arch/arm64/configs/gki_defconfig"
        
        if not config_file.exists():
            logger.warning(f"配置文件不存在: {config_file}")
            return
        
        with open(config_file, "r") as f:
            lines = f.readlines()
        
        config_lines = [line.strip() for line in lines if line.strip().startswith("CONFIG_")]
        
        key_configs = {
            "CONFIG_KSU": "KernelSU",
            "CONFIG_KPM": "KPM",
            "CONFIG_KSU_SUSFS": "SUSFS",
            "CONFIG_BBG": "Baseband-guard",
            "CONFIG_BBR": "BBR",
            "CONFIG_ZRAM": "ZRAM",
        }
        
        logger.info("关键配置状态:")
        for prefix, name in key_configs.items():
            found = [c for c in config_lines if c.startswith(prefix)]
            if found:
                status = "已启用"
            else:
                status = "未配置"
            logger.info(f"  [{status}] {name}")
            if found:
                for f in sorted(found):
                    logger.info(f"      -> {f}")
        
        # 显示 ZRAM 相关配置
        if self.config.use_zram:
            zram_configs = [c for c in config_lines if any(x in c for x in ["ZRAM", "ZSMALLOC", "LZ4", "LZ4KD", "CRYPTO_LZ4", "MODULE_SIG"])]
            if zram_configs:
                logger.info("ZRAM 相关配置:")
                for zc in sorted(zram_configs):
                    logger.info(f"  -> {zc}")
        
        logger.info("-" * 60)

    def build_kernel(self) -> bool:
        logger.info("=== 开始编译内核 ===")
        self._chdir(self.work_dir)

        build_config = self.work_dir / "common/build.config.gki.aarch64"
        if build_config.exists():
            with open(build_config, "r") as f:
                content = f.read()
            content = content.replace("BUILD_SYSTEM_DLKM=1", "BUILD_SYSTEM_DLKM=0")
            lines = [l for l in content.split('\n') if 'MODULES_ORDER=android/gki_aarch64_modules' not in l and 'KMI_SYMBOL_LIST_STRICT_MODE' not in l]
            with open(build_config, "w") as f:
                f.write('\n'.join(lines))

        try:
            if (self.work_dir / "build/build.sh").exists():
                logger.info("使用旧版构建方式...")
                result = self._run_cmd("LTO=thin BUILD_CONFIG=common/build.config.gki.aarch64 build/build.sh CC=\"/usr/bin/ccache clang\"", check=False)
            else:
                logger.info("使用 Bazel 构建方式...")
                result = self._run_cmd("tools/bazel build --disk_cache=/home/runner/.cache/bazel --config=fast --lto=thin //common:kernel_aarch64_dist", check=False)

            if result.returncode == 0:
                logger.info("=== 内核编译成功 ===")
                return True
            logger.error(f"内核编译失败: {result.stderr if result.stderr else 'Unknown error'}")
            return False
        except Exception as e:
            logger.error(f"编译过程出错: {e}")
            return False

    def patch_kpm_image(self):
        if not self.config.use_kpm or self.config.kernel_version == "6.6":
            return
        logger.info("=== 修补 Image 文件 (KPM) ===")
        self._chdir(self.work_dir)

        if self.config.android_version in ["android12", "android13"]:
            image_dir = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_dir = self.work_dir / "bazel-bin/common/kernel_aarch64"

        if not image_dir.exists():
            return
        self._chdir(image_dir)
        self._run_cmd(f"curl -LSs {KPM_PATCH_URL} -o patch && chmod 777 patch && ./patch", check=False)
        if (image_dir / "oImage").exists():
            self._run_cmd("mv oImage Image", check=False)

    def prepare_boot_images(self) -> list:
        logger.info("=== 准备启动镜像 ===")
        self._chdir(self.work_dir)
        bootimgs_dir = self.work_dir / "bootimgs"
        bootimgs_dir.mkdir(exist_ok=True)
        artifacts = []

        if self.config.android_version in ["android12", "android13"]:
            image_source = self.work_dir / f"out/{self.config.android_version}-{self.config.kernel_version}/dist"
        else:
            image_source = self.work_dir / "bazel-bin/common/kernel_aarch64"

        for image_name in ["Image", "Image.lz4"]:
            src = image_source / image_name
            if src.exists():
                self._run_cmd(f"cp {src} {bootimgs_dir}/ && cp {src} {self.work_dir}/", check=False)

        if (self.work_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        if self.config.android_version == "android12":
            self._prepare_android12_boot_images(bootimgs_dir, artifacts)
        else:
            self._prepare_boot_images_generic(bootimgs_dir, artifacts)
        return artifacts

    def _prepare_android12_boot_images(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        gki_url = f"https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-{self.config.os_patch_level}_{self.config.revision}.zip"
        fallback_url = "https://dl.google.com/android/gki/gki-certified-boot-android12-5.10-2023-01_r1.zip"
        result = subprocess.run(f"curl -sL -w '%{{http_code}}' {gki_url} -o /dev/null", shell=True, capture_output=True, text=True)
        url = gki_url if "200" in result.stdout else fallback_url
        self._run_cmd(f"curl -Lo gki-kernel.zip {url} && unzip -o gki-kernel.zip && rm gki-kernel.zip", check=False)
        boot_img_path = bootimgs_dir / "boot-5.10.img"
        if boot_img_path.exists():
            self._run_cmd(f"$UNPACK_BOOTIMG --boot_img={boot_img_path}", check=False)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=True)

    def _prepare_boot_images_generic(self, bootimgs_dir: Path, artifacts: list):
        self._chdir(bootimgs_dir)
        self._create_boot_image_variants(bootimgs_dir, artifacts, has_ramdisk=False)

    def _create_boot_image_variants(self, bootimgs_dir: Path, artifacts: list, has_ramdisk: bool = False):
        self._chdir(bootimgs_dir)
        if (bootimgs_dir / "Image").exists():
            self._run_cmd("gzip -n -k -f -9 Image", check=False)

        for kernel_file, output_file in [("Image", "boot.img"), ("Image.gz", "boot-gz.img"), ("Image.lz4", "boot-lz4.img")]:
            kernel_path = bootimgs_dir / kernel_file
            if not kernel_path.exists():
                continue
            cmd = f"$MKBOOTIMG --header_version 4 --kernel {kernel_file} --output {output_file}"
            if has_ramdisk:
                cmd += f" --ramdisk out/ramdisk --os_version 12.0.0 --os_patch_level {self.config.os_patch_level}"
            self._run_cmd(cmd, check=False)
            self._run_cmd(f"$AVBTOOL add_hash_footer --partition_name boot --partition_size $((64 * 1024 * 1024)) --image {output_file} --algorithm SHA256_RSA2048 --key $BOOT_SIGN_KEY_PATH", check=False)
            dest = self.work_dir / f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-{output_file}"
            self._run_cmd(f"cp {output_file} {dest}", check=False)
            artifacts.append(str(dest))

    def create_anykernel_zips(self) -> list:
        logger.info("=== 创建 AnyKernel3 ZIP 文件 ===")
        self._chdir(self.work_dir)
        artifacts = []
        ak3_dir = self.anykernel_dir

        for suffix in ["", "-lz4", "-gz"]:
            image_file = f"Image{suffix}"
            image_path = self.work_dir / image_file
            if not image_path.exists():
                continue
            zip_name = f"{self.config.android_version}-{self.config.kernel_version}.{self.config.sub_level}-{self.config.os_patch_level}-AnyKernel3{suffix}.zip"
            self._run_cmd(f"cp {image_path} {ak3_dir}/", check=False)
            self._chdir(ak3_dir)
            self._run_cmd(f"zip -r ../{zip_name} ./*", check=False)
            self._run_cmd(f"rm {ak3_dir}/{image_file}", check=False)
            artifacts.append(str(self.work_dir / zip_name))
            self._chdir(self.work_dir)
        return artifacts

    def build(self) -> BuildResult:
        import time
        start_time = time.time()
        logger.info("=" * 50)
        logger.info(f"开始 GKI Kernel 构建 - {self.config.config_name}")
        logger.info("=" * 50)

        try:
            self.clone_repositories()
            self.clone_toolchain()
            self.setup_repo_tool()
            self.init_and_sync_kernel()
            self.add_kernel_supatch()
            self.add_kernelsu()
            self.add_bbg()
            self.apply_susfs_patches()
            self.apply_sukisu_patches()
            self.apply_zram_patches()
            self.apply_task_mmu_fixes()
            self.apply_cve_2026_43499_fix()
            self.configure_kernel()
            self.configure_kernel_name()
            self.show_kernel_config()

            if not self.build_kernel():
                return BuildResult(success=False, config=self.config, message="内核编译失败", build_time=time.time() - start_time)

            self.patch_kpm_image()
            artifacts = []
            artifacts.extend(self.prepare_boot_images())
            artifacts.extend(self.create_anykernel_zips())

            build_time = time.time() - start_time
            logger.info(f"构建成功! 耗时: {build_time:.2f} 秒, 生成 {len(artifacts)} 个产物")
            return BuildResult(success=True, config=self.config, message="构建成功", artifacts=artifacts, build_time=build_time)
        except Exception as e:
            logger.error(f"构建过程出错: {e}")
            return BuildResult(success=False, config=self.config, message=str(e), build_time=time.time() - start_time)
