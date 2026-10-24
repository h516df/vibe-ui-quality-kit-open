#!/usr/bin/env python3
"""privacy_scan.py — 提交或发布前扫描仓库里的隐私与凭据残留。

用途：防止真实店铺/地址/手机号/本机路径/签名密钥被顺手写进代码、测试样例
（fixture）或文档，再随仓库一起公开。

用法：
    python privacy_scan.py <目录> [--json] [--quiet]
    python privacy_scan.py . --json > scan.json

退出码：
    0  没有发现问题
    1  发现问题（需要人工处理）
    2  参数或环境错误

豁免：
    行尾或上一行写 privacy-ok 的行会被跳过，用于确属示例的固定数据。

检测项：
    本机绝对路径、手机号、身份证、邮箱、银行卡号、私钥与票据、
    凭据赋值、密钥文件、疑似真实地址（省市区 + 路街 + 门牌）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

SKIP_DIRS = {
    ".git", ".hg", ".svn", "build", "out", "dist", "node_modules",
    ".gradle", "__pycache__", ".venv", "venv", ".idea", ".vscode-test",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", ".kotlin",
}
SKIP_EXT = {
    ".apk", ".aab", ".jar", ".zip", ".tar", ".gz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2",
    ".ttf", ".otf", ".so", ".dll", ".exe", ".bin", ".class", ".pyc",
    ".pdf", ".mp4", ".mp3", ".wav", ".m4a", ".epub",
}
MAX_BYTES = 2 * 1024 * 1024  # 单文件超过 2 MB 跳过（多为数据文件）

# 明确允许的示例值，不报警
ALLOW_VALUES = {
    "13800138000", "13900139000", "18888888888", "11111111111",
    "110101199001011234", "110101199001010000",  # privacy-ok fixed synthetic identity examples
    "example@example.com", "you@example.com", "someone@example.com",
    "admin@example.com", "test@example.com",
}

RE_PATH_WIN = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\(?:Users|Projects|Apps|Backup|Data|Temp|Work)\\[^\s\"'<>|*?]*", re.I)
RE_PATH_USER = re.compile(r"(?<![A-Za-z])[A-Za-z]:\\Users\\[A-Za-z0-9._-]+\\", re.I)
RE_PATH_POSIX = re.compile(r"(?<![A-Za-z0-9:/])/(?:Users|home)/[A-Za-z0-9._-]+/")
RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
RE_IDCARD = re.compile(r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)")
RE_EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+\.[\w.-]{2,}")
RE_BANK = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
RE_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
RE_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})")
RE_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)\b"
    r"\s*[:=]\s*([^\s,;\"'#]{4,})"
)
# 疑似真实地址：省/市/区/县 + 路/街/道/巷 + 门牌号
RE_ADDR_CN = re.compile(
    r"[\u4e00-\u9fa5]{2,10}(?:省|市|区|县|镇)[^\s，,。;；|]{0,20}?"
    r"(?:路|街|道|巷|胡同)[^\s，,。;；|]{0,12}?"
    r"(?:号|\d+[栋幢单元楼层室]|[A-Za-z]?-\d+)"
)
SECRET_FILENAMES = {
    "keystore.properties", ".env", ".env.local", "id_rsa", "id_ed25519",
    "credentials.json", "secrets.yml", "secrets.yaml",
}
SECRET_EXTS = {".jks", ".keystore", ".p12", ".pfx", ".pem", ".key", ".kdbx"}
FIXTURE_HINTS = ("fixture", "fixtures", "test-resources", "testdata", "test-data", "samples", "snapshots")


@dataclass
class Hit:
    category: str
    path: str
    line: int
    excerpt: str
    note: str = ""


@dataclass
class Report:
    root: str
    files_scanned: int = 0
    hits: list[Hit] = field(default_factory=list)

    def add(self, h: Hit) -> None:
        self.hits.append(h)


def is_texty(path: str) -> bool:
    ext = os.path.splitext(path)[1].lower()
    return ext not in SKIP_EXT


def iter_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.lower().startswith(".gradle")]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if not is_texty(full):
                continue
            try:
                if os.path.getsize(full) > MAX_BYTES:
                    continue
            except OSError:
                continue
            yield full


def read_lines(path: str) -> list[str] | None:
    for enc in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            with open(path, "r", encoding=enc) as fh:
                return fh.read().splitlines()
        except UnicodeDecodeError:
            continue
        except OSError:
            return None
    return None


def masked(text: str) -> str:
    text = text.strip()
    return text[:120] + ("…" if len(text) > 120 else "")


def in_fixture_dir(path: str) -> bool:
    low = path.replace("\\", "/").lower()
    return any(h in low for h in FIXTURE_HINTS)


def check_file_name(path: str, rel: str, report: Report) -> None:
    name = os.path.basename(path).lower()
    ext = os.path.splitext(name)[1]
    if name in SECRET_FILENAMES:
        report.add(Hit("密钥/凭据文件", rel, 0, name, "提交前确认：这类文件不应进仓库"))
    elif ext in SECRET_EXTS:
        report.add(Hit("签名/私钥文件", rel, 0, name, "签名材料不应进公开仓库；如有需要放到受保护位置"))


def scan_text(path: str, rel: str, lines: list[str], report: Report) -> None:
    fixture = in_fixture_dir(rel)
    for idx, line in enumerate(lines, start=1):
        low = line.lower()
        if "privacy-ok" in low:
            continue
        prev = lines[idx - 2].lower() if idx >= 2 else ""
        if "privacy-ok" in prev:
            continue

        for m in RE_PATH_WIN.finditer(line):
            report.add(Hit("本机绝对路径", rel, idx, masked(m.group(0)), "改成占位符或从环境变量读取"))
        for m in RE_PATH_USER.finditer(line):
            report.add(Hit("含用户名的本机路径", rel, idx, masked(m.group(0)), "改用 $env:USERPROFILE / ~"))
        for m in RE_PATH_POSIX.finditer(line):
            report.add(Hit("含用户名的本机路径", rel, idx, masked(m.group(0)), "改用 ~ 或环境变量"))

        # 长十六进制串（哈希、指纹）里会偶然出现 11 位数字或 16+ 位数字，先识别出来再跳过数字类检测
        hex_context = bool(re.search(r"[0-9a-fA-F]{32,}", line)) or len(line) > 2000

        for m in RE_PHONE.finditer(line):
            if hex_context:
                continue
            if m.group(0) not in ALLOW_VALUES:
                report.add(Hit("手机号", rel, idx, masked(m.group(0)), "换成 13800138000 之类的示例号"))
        for m in RE_IDCARD.finditer(line):
            if hex_context:
                continue
            if m.group(0) not in ALLOW_VALUES:
                report.add(Hit("身份证号", rel, idx, masked(m.group(0))))
        for m in RE_EMAIL.finditer(line):
            addr = m.group(0).lower()
            if addr in ALLOW_VALUES:
                continue
            if addr.endswith(("github.com", "noreply.github.com")):
                continue  # 提交里的机器人地址，不是个人信息
            if re.search(r"@v?\d+(?:\.\d+)+$", addr):
                continue  # actions/setup@v6.1.0 之类的版本号
            if re.search(r"@(example\.\w+|example)$", addr):
                continue  # 已经是示例地址
            if re.search(r"@\d+(?:\.\d+)?x\.", addr):
                continue  # @2x.png 之类的文件名
            if rel.replace("\\", "/").endswith(("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "package.json")):
                continue  # 第三方依赖元数据里的作者邮箱
            report.add(Hit("邮箱地址", rel, idx, masked(m.group(0)), "公开仓库里换 example.com"))
        for m in RE_BANK.finditer(line):
            digits = m.group(0)
            if hex_context:
                continue
            if digits.startswith(("1",)) and len(digits) == 11:
                continue  # 已按手机号处理
            if digits in {"9223372036854775807", "9223372036854775808"}:
                continue  # Int64 边界常量
            if re.search(r"figma\.com|/file/|drive\.google", line, re.I):
                continue  # 设计文件的资源 ID
            report.add(Hit("疑似银行卡/长数字串", rel, idx, masked(digits), "确认是否真实账号"))
        if RE_PRIVATE_KEY.search(line):
            report.add(Hit("私钥内容", rel, idx, masked(line)))
        for m in RE_TOKEN.finditer(line):
            report.add(Hit("疑似访问令牌", rel, idx, masked(m.group(0)), "立刻作废并换成占位符"))
        for m in RE_ASSIGN.finditer(line):
            value = m.group(2)
            if value.lower() in {"null", "none", "true", "false", "changeme", "your_password", "xxx"}:
                continue
            if value.startswith(("$", "%", "<", "{", "(", "=", "+", "-", "!")):
                continue  # 代码表达式而不是字面量
            if any(ch in value for ch in "(){}[];,"):
                continue  # 出现在表达式或类型声明里
            if value in {"String", "String?", "Long", "Int", "Boolean", "Double", "Float", "ByteArray", "Unit", "Any"}:
                continue  # Kotlin/Java 类型声明
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):
                continue  # 全大写常量名，不是口令值
            if re.fullmatch(r"[a-z][A-Za-z]*", value):
                continue  # 纯标识符（变量名、枚举），不是口令
            if any(v in value.lower() for v in ("example", "sample", "placeholder", "dummy", "test")):
                continue
            report.add(Hit("凭据赋值", rel, idx, masked(m.group(0)), "检查是否真实口令；示例值请标注 example"))
        for m in RE_ADDR_CN.finditer(line):
            if "/" in m.group(0):
                continue  # 「区/路/号」这类说明文字，不是地址
            note = "测试样例里出现真实地址时务必替换成虚构地址" if fixture else "确认是否为真实地址"
            report.add(Hit("疑似真实地址", rel, idx, masked(m.group(0)), note))


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="扫描仓库中的隐私与凭据残留")
    ap.add_argument("root", nargs="?", default=".", help="要扫描的目录")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--quiet", action="store_true", help="只输出汇总")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"目录不存在: {root}", file=sys.stderr)
        return 2

    report = Report(root=root)
    for path in iter_files(root):
        rel = os.path.relpath(path, root)
        report.files_scanned += 1
        check_file_name(path, rel, report)
        lines = read_lines(path)
        if lines:
            scan_text(path, rel, lines, report)

    if args.json:
        print(json.dumps({
            "root": report.root,
            "files_scanned": report.files_scanned,
            "total": len(report.hits),
            "hits": [
                {"category": h.category, "path": h.path, "line": h.line,
                 "excerpt": h.excerpt, "note": h.note}
                for h in report.hits
            ],
        }, ensure_ascii=False, indent=2))
    else:
        if report.hits and not args.quiet:
            by_cat: dict[str, list[Hit]] = {}
            for h in report.hits:
                by_cat.setdefault(h.category, []).append(h)
            for cat in sorted(by_cat):
                group = by_cat[cat]
                print(f"\n[{cat}] {len(group)} 处")
                for h in group[:50]:
                    loc = f"{h.path}:{h.line}" if h.line else h.path
                    extra = f"  -> {h.note}" if h.note else ""
                    print(f"  {loc}  {h.excerpt}{extra}")
                if len(group) > 50:
                    print(f"  …还有 {len(group) - 50} 处")
        print(f"\n扫描 {report.files_scanned} 个文件，发现 {len(report.hits)} 处需要注意。")
        if report.hits:
            print("处理方式：真实数据换成合成样例；本机路径改成占位符或环境变量；")
            print("确属安全示例的行，可在行尾加 privacy-ok 标注豁免。")

    return 1 if report.hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
