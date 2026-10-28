#!/usr/bin/env python3
"""Publish an IPA via the user's gh credentials. Runtime: Python standard library."""

import argparse
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import quote
import zipfile

from validate_source import ValidationError, https_url, load_json, validate_source

ROOT = Path(__file__).resolve().parents[1]
REPO = "athlon128/AppStore"
GH_REPO = f"github.com/{REPO}"
SOURCE_URL = "https://athlon128.github.io/AppStore/source.json"
SOURCE_ID = "com.athlon128.appstore"
MAX_ASSET_SIZE = 2 * 1024**3  # GitHub: each asset must be smaller than 2 GiB.


class PublishError(RuntimeError):
    pass


def run(args, *, cwd=ROOT):
    result = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "GH_PROMPT_DISABLED": "1", "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise PublishError(f"命令失败（退出码 {result.returncode}）：{args[0]} {args[1]}\n{detail}")
    return result.stdout.strip()


def git(*args):
    # Per-command helper: do not change the user's global Git configuration.
    return run(["git", "-c", "credential.helper=", "-c",
                "credential.https://github.com.helper=!gh auth git-credential", *args])


def api(endpoint, *, paginate=False):
    args = ["gh", "api", "--hostname", "github.com", "--method", "GET", endpoint]
    if paginate:
        args.extend(["--paginate", "--slurp"])
    value = json.loads(run(args))
    return [item for page in value for item in page] if paginate else value


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+(?:\.\d+){0,2}", value):
        raise PublishError(f"不支持的数字版本号 {value!r}；需要 1～3 段数字，如 1.2.3")
    parts = tuple(int(item) for item in value.split("."))
    return parts + (0,) * (3 - len(parts))


def ipa_metadata(path):
    if not path.is_file():
        raise PublishError(f"IPA 不存在或不是普通文件：{path}")
    size = path.stat().st_size
    if size <= 0 or size >= MAX_ASSET_SIZE:
        raise PublishError("IPA 必须非空，且小于 GitHub 单个 Release Asset 的 2 GiB 限制")
    try:
        with zipfile.ZipFile(path) as archive:
            candidates = [item for item in archive.infolist()
                          if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", item.filename)]
            if len(candidates) != 1:
                raise PublishError("IPA 必须恰好包含一个 Payload/*.app/Info.plist（扩展的 plist 不算）")
            if candidates[0].file_size > 16 * 1024**2:
                raise PublishError("Info.plist 超出合理大小（16 MiB）")
            info = plistlib.loads(archive.read(candidates[0]))
    except (zipfile.BadZipFile, plistlib.InvalidFileException, KeyError, RuntimeError, ValueError) as error:
        raise PublishError(f"无法读取 IPA：{error}") from error
    if not isinstance(info, dict):
        raise PublishError("Info.plist 顶层必须为字典")
    values = {}
    for field in ("CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion"):
        value = info.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PublishError(f"IPA 的 {field} 缺失或不是非空字符串")
        values[field] = value
    bundle = values["CFBundleIdentifier"]
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle):
        raise PublishError(f"无效 Bundle ID：{bundle!r}")
    numeric_version(values["CFBundleShortVersionString"])
    numeric_version(values["CFBundleVersion"])
    name = info.get("CFBundleDisplayName") or info.get("CFBundleName")
    if not isinstance(name, str) or not name.strip():
        raise PublishError("IPA 缺少 CFBundleDisplayName / CFBundleName")
    result = {"bundleIdentifier": bundle, "version": values["CFBundleShortVersionString"],
              "build": values["CFBundleVersion"], "name": name, "size": size}
    if "MinimumOSVersion" in info:
        numeric_version(info["MinimumOSVersion"])
        result["minOSVersion"] = info["MinimumOSVersion"]
    return result


def release_identity(meta):
    tag = f"{meta['bundleIdentifier']}-v{meta['version']}-b{meta['build']}"
    asset = f"{meta['bundleIdentifier']}-{meta['version']}-{meta['build']}.ipa"
    return tag, asset


def download_url(tag, asset):
    return f"https://github.com/{REPO}/releases/download/{quote(tag, safe='')}/{quote(asset, safe='')}"


def candidate_source(source, meta, args, date, url):
    validate_source(source)
    if source["identifier"] != SOURCE_ID or source["sourceURL"] != SOURCE_URL:
        raise PublishError("source.json 的源 identifier / sourceURL 与本发布仓库不一致")
    result = copy.deepcopy(source)
    app = next((item for item in result["apps"]
                if item["bundleIdentifier"] == meta["bundleIdentifier"]), None)
    if app is None:
        if not args.init:
            known = ", ".join(item["bundleIdentifier"] for item in source["apps"]) or "（空源）"
            raise PublishError(f"IPA Bundle ID {meta['bundleIdentifier']} 不在源中。现有应用：{known}。"
                               "确认新增应用后使用 --init，并明确提供开发者、描述和图标。")
        missing = [flag for flag, value in (("--developer", args.developer),
                   ("--description", args.description), ("--icon-url", args.icon_url))
                   if not value or not value.strip()]
        if missing:
            raise PublishError(f"首次发布需要明确提供：{' '.join(missing)}；不能从 IPA 猜测这些信息")
        https_url(args.icon_url, "--icon-url")
        app = {"name": args.name or meta["name"], "bundleIdentifier": meta["bundleIdentifier"],
               "developerName": args.developer, "localizedDescription": args.description,
               "iconURL": args.icon_url, "versions": []}
        result["apps"].append(app)
    elif args.init:
        raise PublishError("该 Bundle ID 已存在；请去掉 --init，按更新流程发布")
    elif any((args.name, args.developer, args.description, args.icon_url)):
        raise PublishError("--name/--developer/--description/--icon-url 仅用于 --init；更新会保留现有元数据")
    if app["versions"]:
        newest = max(numeric_version(item["version"]) for item in app["versions"])
        if numeric_version(meta["version"]) <= newest:
            raise PublishError("新 IPA 的 CFBundleShortVersionString 必须大于所有已发布版本；"
                               "只增加 CFBundleVersion 不会被本源当成新版本")
    version = {"version": meta["version"], "date": date,
               "localizedDescription": args.notes or f"发布 {meta['version']}（构建 {meta['build']}）",
               "downloadURL": url, "size": meta["size"]}
    if "minOSVersion" in meta:
        version["minOSVersion"] = meta["minOSVersion"]
    # Preserve every historical version, including those beyond the latest three.
    app["versions"].insert(0, version)
    validate_source(result)
    return result


def ensure_clean(base=None):
    if git("status", "--porcelain", "--untracked-files=all"):
        raise PublishError("Git 工作区不干净（含未跟踪文件）；请先保存并提交修改，再发布")
    head = git("rev-parse", "HEAD")
    if base is not None and head != base:
        raise PublishError("发布期间本地 HEAD 已变化，停止更新源")
    return head


def verify_git():
    if Path(git("rev-parse", "--show-toplevel")).resolve() != ROOT:
        raise PublishError("必须在独立 AppStore Git 仓库中执行")
    if git("branch", "--show-current") != "main":
        raise PublishError("请在 main 分支发布，Pages 配置为 main 根目录")
    allowed = {f"https://github.com/{REPO}.git", f"https://github.com/{REPO}",
               f"git@github.com:{REPO}.git", f"ssh://git@github.com/{REPO}.git"}
    for flags in (("--all",), ("--all", "--push")):
        urls = git("remote", "get-url", *flags, "origin").splitlines()
        if len(urls) != 1 or urls[0] not in allowed:
            raise PublishError(f"origin 必须唯一指向 {REPO}；拒绝向其他仓库发布")
    if not git("ls-files", "--error-unmatch", "source.json"):
        raise PublishError("source.json 必须已经提交到 Git")
    if not git("config", "user.name") or not git("config", "user.email"):
        raise PublishError("请先为本仓库设置 git config user.name 和 user.email")
    base = ensure_clean()
    git("fetch", "--no-tags", "origin", "main")
    if git("rev-parse", "refs/remotes/origin/main") != base:
        raise PublishError("本地 main 与远端不一致；请先处理拉取或未推送提交，不自动合并/强推")
    return base


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_release(release, tag, asset_name, size, digest, temp):
    if release.get("tag_name") != tag:
        raise PublishError("Release Tag 不匹配")
    if release.get("prerelease"):
        raise PublishError("本工具只发布正式版本，不能恢复 prerelease")
    assets = [item for item in release.get("assets", []) if item["name"] == asset_name]
    if len(assets) != 1:
        raise PublishError("Release 缺少唯一的预期 IPA Asset；请检查上传是否完整，不会自动覆盖")
    asset = assets[0]
    if asset.get("state") != "uploaded" or asset.get("size") != size:
        raise PublishError("Release Asset 尚未完整上传或字节大小不符")
    actual_url = asset.get("browser_download_url", "")
    expected_url = download_url(tag, asset_name)
    # GitHub gives draft assets an untagged-<id> URL before the tag is created
    # on publication. Allow that exact repository/asset shape for drafts only;
    # verify again after publishing and never put a draft URL in source.json.
    temporary_draft_url = bool(release.get("draft") and re.fullmatch(
        re.escape(f"https://github.com/{REPO}/releases/download/")
        + r"untagged-[A-Za-z0-9-]+/" + re.escape(quote(asset_name, safe="")), actual_url))
    if actual_url != expected_url and not temporary_draft_url:
        raise PublishError("Release Asset 的实际下载地址与预期版本地址不符")
    if asset.get("digest"):
        if asset["digest"] != f"sha256:{digest}":
            raise PublishError("Release IPA 的 SHA-256 与本地文件不一致，拒绝恢复/更新源")
    else:
        # Older GitHub assets may not have an API digest. Compare actual bytes.
        verify_dir = Path(tempfile.mkdtemp(prefix="verify-remote-", dir=temp))
        run(["gh", "release", "download", tag, "--repo", GH_REPO,
             "--pattern", asset_name, "--dir", verify_dir])
        if sha256(verify_dir / asset_name) != digest:
            raise PublishError("远端 IPA 校验和与本地文件不一致")
    return asset["browser_download_url"]


def atomic_source(path, value):
    handle, filename = tempfile.mkstemp(prefix=".source-", suffix=".json", dir=path.parent)
    pending = Path(filename)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        validate_source(load_json(pending))
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def publish(args):
    ipa = args.ipa.expanduser().resolve()
    if args.inspect:
        run(["unzip", "-tqq", ipa])
        print(json.dumps(ipa_metadata(ipa), ensure_ascii=False, indent=2))
        return
    if not ipa.is_file():
        raise PublishError(f"IPA 文件不存在：{ipa}")
    git_dir = Path(git("rev-parse", "--absolute-git-dir"))
    # Keep simultaneous publishers in this checkout from editing the same source.
    with (git_dir / "appstore-publish.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise PublishError("此仓库已有一个发布进程，请等待它结束") from error
        with tempfile.TemporaryDirectory(prefix="appstore-ipa-") as temporary:
            temp = Path(temporary)
            # Snapshot first: metadata, digest and uploaded bytes must be identical.
            snapshot = temp / "input.ipa"
            if ipa.stat().st_size >= MAX_ASSET_SIZE:
                raise PublishError("IPA 必须小于 2 GiB")
            shutil.copyfile(ipa, snapshot)
            meta = ipa_metadata(snapshot)
            run(["unzip", "-tqq", snapshot])
            tag, asset_name = release_identity(meta)
            staged = temp / asset_name
            snapshot.rename(staged)
            digest = sha256(staged)
            source_path = ROOT / "source.json"
            if source_path.is_symlink():
                raise PublishError("source.json 不能为符号链接")
            original_bytes = source_path.read_bytes()
            source = load_json(source_path)
            date = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            candidate_source(source, meta, args, date, download_url(tag, asset_name))
            run(["gh", "auth", "status", "--hostname", "github.com"])
            info = api(f"repos/{REPO}")
            if not info.get("permissions", {}).get("push"):
                raise PublishError("当前 gh 账号没有该仓库的 push 权限")
            if info.get("private"):
                raise PublishError("源和 Release 必须允许 SideStore 匿名下载；本工具要求公开仓库")
            base = verify_git()
            releases = api(f"repos/{REPO}/releases?per_page=100", paginate=True)
            existing = next((item for item in releases if item["tag_name"] == tag), None)
            refs = api(f"repos/{REPO}/git/matching-refs/tags/{quote(tag, safe='')}")
            tag_exists = any(item["ref"] == f"refs/tags/{tag}" for item in refs)
            if args.resume:
                if not existing:
                    raise PublishError("--resume 需要已存在的 Release，不会创建或覆盖 IPA")
            elif existing or tag_exists:
                raise PublishError(f"Tag / Release {tag} 已存在；默认拒绝重复发布或覆盖。"
                                   "仅恢复同一 IPA 的源更新时可显式使用 --resume")
            if args.dry_run:
                print(json.dumps({"mode": "dry-run（未创建 Release、提交或推送）", "ipa": meta,
                                  "tag": tag, "asset": asset_name,
                                  "downloadURL": download_url(tag, asset_name)}, ensure_ascii=False, indent=2))
                return
            release_created = False
            try:
                ensure_clean(base)
                if existing is None:
                    # Mark the attempt before calling gh: a lost response can still
                    # mean GitHub created the draft; recovery must mention it.
                    release_created = True
                    run(["gh", "release", "create", tag, "--repo", GH_REPO,
                         "--target", base, "--title", f"{meta['name']} {meta['version']} ({meta['build']})",
                         "--notes", args.notes or f"{meta['name']} {meta['version']}（构建 {meta['build']}）",
                         "--draft"])
                    run(["gh", "release", "upload", tag, staged, "--repo", GH_REPO])
                else:
                    release_created = True
                # The by-tag REST endpoint is documented for published releases.
                # List releases with authenticated push access, then use its ID
                # so a newly uploaded draft can be verified before publication.
                matches = [item for item in api(f"repos/{REPO}/releases?per_page=100", paginate=True)
                           if item["tag_name"] == tag]
                if len(matches) != 1:
                    raise PublishError("无法确认唯一的 Release，源文件未更新")
                endpoint = f"repos/{REPO}/releases/{matches[0]['id']}"
                release = api(endpoint)
                url = verify_release(release, tag, asset_name, meta["size"], digest, temp)
                ensure_clean(base)
                if release.get("draft"):
                    run(["gh", "release", "edit", tag, "--repo", GH_REPO, "--draft=false"])
                    release = api(endpoint)
                if release.get("draft") or not release.get("published_at"):
                    raise PublishError("无法确认 Release 已公开发布，源文件未更新")
                url = verify_release(release, tag, asset_name, meta["size"], digest, temp)
                result = candidate_source(source, meta, args, release["published_at"], url)
                ensure_clean(base)
                if source_path.read_bytes() != original_bytes:
                    raise PublishError("source.json 在发布期间变化，拒绝覆盖")
                # Check the remote again after upload, before touching source.json.
                git("fetch", "--no-tags", "origin", "main")
                if git("rev-parse", "refs/remotes/origin/main") != base:
                    raise PublishError("上传期间远端 main 已变化；请先同步后使用 --resume")
                atomic_source(source_path, result)
                git("add", "--", "source.json")
                git("commit", "-m", f"Release {meta['bundleIdentifier']} {meta['version']} ({meta['build']})",
                    "--", "source.json")
                git("push", "origin", "HEAD:refs/heads/main")
            except (PublishError, ValidationError, OSError, ValueError, KeyError) as error:
                if release_created:
                    print(f"\n发布未全部完成。Release 可能已创建或发布：\n"
                          f"https://github.com/{REPO}/releases/tag/{quote(tag, safe='')}\n"
                          "不会删除 Release、覆盖 IPA 或回滚用户文件。\n"
                          "先检查 git status / git log -1：如源已提交但推送失败，排除网络问题后 git push origin main；\n"
                          "如 source.json 已修改但未提交，先运行校验再手动提交/推送；\n"
                          "如源尚未修改且工作区干净，用同一 IPA、原参数加 --resume 恢复（校验远端字节）。\n"
                          "如仅创建了空草稿且上传失败，请手工检查草稿后再处理；--resume 不会补传或覆盖。\n",
                          file=sys.stderr)
                raise error
            print(f"发布完成：{meta['name']} {meta['version']}（构建 {meta['build']}）\n"
                  f"Release：https://github.com/{REPO}/releases/tag/{quote(tag, safe='')}\n"
                  f"IPA：{url}\n源：{SOURCE_URL}\n"
                  "请等待 GitHub Pages 部署成功，再在 SideStore 的 Sources 刷新并安装/更新。"
                  "本命令未验证 iPhone 安装结果。")


def main():
    parser = argparse.ArgumentParser(description="从真实 IPA 发布 SideStore 源；默认禁止覆盖已有 Tag")
    parser.add_argument("ipa", type=Path, help="IPA 路径（支持空格；请用引号包围）")
    parser.add_argument("--init", action="store_true", help="明确允许新增此 Bundle ID")
    parser.add_argument("--name", help="首次发布时的显示名称，默认使用 IPA 的名称")
    parser.add_argument("--developer", help="首次发布时确认的开发者名称")
    parser.add_argument("--description", help="首次发布时确认的应用描述")
    parser.add_argument("--icon-url", help="首次发布时确认的公开 HTTPS 图标地址")
    parser.add_argument("--notes", help="本次更新说明")
    parser.add_argument("--inspect", action="store_true", help="仅检查 IPA 并输出元数据，无网络操作")
    parser.add_argument("--dry-run", action="store_true", help="完成预检，不创建 Release、不修改源、不提交推送")
    parser.add_argument("--resume", action="store_true", help="恢复已存在 Release 的源更新，绝不上传或覆盖 IPA")
    args = parser.parse_args()
    try:
        publish(args)
    except (PublishError, ValidationError, OSError, ValueError, KeyError) as error:
        print(f"错误：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
