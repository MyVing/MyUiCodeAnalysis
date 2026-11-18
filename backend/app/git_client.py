"""Git 仓库接入：URL 归一化、克隆、同步与框架识别。

去重键是归一化后的远程地址，而不是用户输入的原始字符串：
`git@github.com:acme/app.git`、`https://github.com/acme/app` 与
`https://github.com/Acme/App/` 指向同一个仓库，必须只接入一次。

克隆目录名由归一化地址的散列后缀决定，因此同一仓库重复接入时不会
落到两个目录；目录根也是后续静态分析与运行时采集的 project.root_path。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

_SSH_SCP = re.compile(r"^(?P<user>[^@/\s]+)@(?P<host>[^:/\s]+):(?P<path>\S+)$")
_SLUG_UNSAFE = re.compile(r"[^a-z0-9._-]+")
# 归一化结果的下限：host 至少一个点或 localhost，path 至少一段
_MIN_SEGMENTS = 2

Framework = Literal["react", "next"]


class GitError(Exception):
    """Git 命令执行失败，消息可直接展示给用户。"""


def normalize_remote_url(raw: str) -> str:
    """把常见写法的远程地址归一成 `host/path`（去凭据、去 .git、小写）。

    无法识别的输入抛 ValueError，由 API 层转成 422。
    """
    value = raw.strip()
    if not value:
        raise ValueError("Git 地址不能为空")

    if "://" in value:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https", "ssh", "git"):
            raise ValueError(f"不支持的 Git 协议：{parts.scheme}")
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        path = parts.path
    else:
        match = _SSH_SCP.match(value)
        if match is None:
            raise ValueError("无法识别的 Git 地址，支持 https:// 与 git@host:path 两种写法")
        host = match.group("host")
        path = match.group("path")

    path = path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    host = host.strip().lower()
    path = path.strip("/")

    if not host or not path:
        raise ValueError("Git 地址缺少主机名或仓库路径")
    if len(path.split("/")) < _MIN_SEGMENTS:
        raise ValueError("Git 地址需要包含组织/仓库路径，例如 https://github.com/acme/app")

    return f"{host}/{path}".lower()


def repo_directory_name(normalized_url: str) -> str:
    """给归一化地址一个稳定、可读且不会冲突的目录名。"""
    digest = hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:10]
    slug = _SLUG_UNSAFE.sub("-", normalized_url.rsplit("/", 1)[-1]).strip("-.")[:48]
    return f"{slug or 'repo'}-{digest}"


def repo_display_name(normalized_url: str) -> str:
    return normalized_url.rsplit("/", 1)[-1] or normalized_url


def detect_framework(root: Path) -> Framework:
    """读 package.json 判定 React / Next；读不到就按 React 处理，不阻断接入。"""
    package_json = root / "package.json"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "react"
    if not isinstance(data, dict):
        return "react"
    dependencies: dict[str, object] = {}
    for key in ("dependencies", "devDependencies", "peerDependencies"):
        section = data.get(key)
        if isinstance(section, dict):
            dependencies.update(section)
    return "next" if "next" in dependencies else "react"


@dataclass(frozen=True)
class GitSnapshot:
    """一次克隆 / 同步后仓库的确定状态。"""

    branch: str
    commit: str


class GitClient(Protocol):
    async def clone(
        self,
        remote_url: str,
        destination: Path,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ) -> GitSnapshot: ...

    async def sync(self, root: Path) -> GitSnapshot: ...


def _clean_error(raw: str) -> str:
    cleaned = " ".join(line.strip() for line in raw.strip().splitlines() if line.strip())
    return cleaned[:400] or "Git 命令执行失败"


def inject_credentials(
    url: str,
    username: str | None = None,
    password: str | None = None,
    token: str | None = None,
) -> str:
    """把凭据注入 HTTPS 地址；只用于本次命令，绝不写回项目记录。

    用户名 + 密码优先（密码也可以是平台签发的访问令牌），否则退回 Token
    模式（`oauth2:<token>`）。用户名与密码可能含 `@`、`/`、`:` 等字符，
    必须逐段 URL 编码，否则会把地址结构拼坏。SSH 地址原样返回。
    """
    if username and password:
        user, secret = quote(username, safe=""), quote(password, safe="")
    elif token:
        user, secret = "oauth2", quote(token, safe="")
    else:
        return url

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return url
    netloc = f"{user}:{secret}@{parts.hostname}"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


class SubprocessGitClient:
    """调用本机 git CLI。交互式凭据提示一律关闭，失败必须显式返回。"""

    def __init__(self, git_command: str = "git", timeout_seconds: float = 180.0) -> None:
        self._git = git_command
        self._timeout = timeout_seconds

    async def _run(self, args: list[str], cwd: Path | None = None) -> str:
        env = {
            **os.environ,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
            "GIT_ASKPASS": "echo",
        }
        try:
            process = await asyncio.create_subprocess_exec(
                self._git,
                *args,
                cwd=str(cwd) if cwd is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as error:
            raise GitError(f"找不到 git 命令（{self._git}），请确认已安装 git") from error

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self._timeout
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            raise GitError(f"git 命令超过 {self._timeout:.0f}s 未完成，已终止") from None

        if process.returncode != 0:
            raise GitError(_clean_error(stderr.decode("utf-8", errors="replace")))
        return stdout.decode("utf-8", errors="replace")

    async def _snapshot(self, root: Path) -> GitSnapshot:
        branch = (await self._run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)).strip()
        commit = (await self._run(["rev-parse", "HEAD"], cwd=root)).strip()
        return GitSnapshot(branch=branch or "HEAD", commit=commit)

    async def clone(
        self,
        remote_url: str,
        destination: Path,
        branch: str | None = None,
        username: str | None = None,
        password: str | None = None,
        token: str | None = None,
    ) -> GitSnapshot:
        destination.parent.mkdir(parents=True, exist_ok=True)
        args = ["-c", "advice.detachedHead=false", "clone", "--depth", "1", "--single-branch"]
        if branch:
            args += ["--branch", branch]
        auth_url = inject_credentials(remote_url, username, password, token)
        args += ["--", auth_url, str(destination)]
        await self._run(args)
        return await self._snapshot(destination)

    async def sync(self, root: Path) -> GitSnapshot:
        """强制对齐远端当前分支：仓库由平台托管，本地改动没有保留价值。"""
        await self._run(["fetch", "--prune", "origin"], cwd=root)
        upstream = (
            await self._run(["rev-parse", "--abbrev-ref", "@{upstream}"], cwd=root)
        ).strip()
        if not upstream or upstream == "HEAD":
            raise GitError("仓库没有跟踪远端分支，无法更新")
        await self._run(["reset", "--hard", upstream], cwd=root)
        return await self._snapshot(root)


def remove_repo_directory(destination: Path) -> None:
    """克隆失败或并发冲突时清理半成品目录；只删自己创建的目录。"""
    if destination.exists():
        shutil.rmtree(destination, ignore_errors=True)
