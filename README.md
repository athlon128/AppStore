# My Private Apps · SideStore 发布仓库

这里用于个人 iOS 应用分发：GitHub Pages 托管应用源 JSON，GitHub Releases 托管 IPA。只维护发布工具和元数据，无需服务器、Docker、数据库或管理后台。IPA 不进入 Git 历史。

“个人使用”不代表访问私密：本仓库、Pages 和 Release Asset 是公开的，知道链接的人可以下载。不要发布含密码、Token、内部数据或无权分发的 IPA。

## 地址与状态

- SideStore 源：<https://athlon128.github.io/AppStore/source.json>
- Pages 根地址：<https://athlon128.github.io/AppStore/>
- Releases：<https://github.com/athlon128/AppStore/releases>
- 源的稳定标识符：`com.athlon128.appstore`，添加源后不要更改。

初始 `apps` 为空，表示尚未发布真实 IPA。Pages 是否可用以源地址实际返回 JSON、GitHub Actions 的部署结果为准；这里不将“文件已提交”视为“部署成功”，也不宣称任何 iPhone 安装已经验证。

## Mac 环境准备

需要 `git`、Python 3.10 或更新版本、`unzip` 和 GitHub CLI `gh`。发布工具只用 Python 标准库，不需要 pip 包。可先检查：

```bash
git --version
python3 --version
unzip -v
```

安装并登录 GitHub CLI：

```bash
brew install gh
gh auth login --hostname github.com
gh auth status --hostname github.com
```

Homebrew 下载慢时，可仅为本次命令使用清华镜像：

```bash
HOMEBREW_NO_AUTO_UPDATE=1 \
HOMEBREW_API_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/api \
HOMEBREW_BOTTLE_DOMAIN=https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles \
brew install gh
```

登录账号必须拥有 `athlon128/AppStore` 的写权限；初始化 Pages 还需管理权限。上传脚本使用 `gh` 当前凭据和临时 Git credential helper，不写入 Token，不修改全局 Git 配置。

本机独立发布目录为 `/Users/tao/Documents/Code/iOS/AppStore`。其他电脑可执行：

```bash
gh repo clone athlon128/AppStore
cd AppStore
git config user.name "你的 Git 提交名称"
git config user.email "你的 GitHub noreply 邮箱"
```

## 首次发布应用

先准备一个已构建的、可用于侧载的 IPA，以及你确认过的开发者名称、应用描述和可公开访问的 HTTPS 图标地址。工具不会猜测这些信息。图标可以托管在此仓库的 `icons/` 中：提交并推送真实 PNG 后，等待 Pages 部署并确认图标 URL 返回 200。也可以使用现有的可靠 HTTPS 图标地址。

先检查 IPA（支持二进制和 XML Info.plist、含空格路径）：

```bash
cd /Users/tao/Documents/Code/iOS/AppStore
./scripts/upload-ipa.sh "$HOME/Downloads/MyApp.ipa" --inspect
```

首次发布完整命令如下。请将 IPA 路径、开发者、描述和图标 URL 替换为真实值；示例的 `MyApp.png` 不表示仓库已有该文件。

```bash
./scripts/upload-ipa.sh "$HOME/Downloads/MyApp.ipa" --init \
  --developer "真实开发者名称" \
  --description "真实应用说明" \
  --icon-url "https://athlon128.github.io/AppStore/icons/MyApp.png" \
  --notes "本次发布说明"
```

`--init` 明确允许创建 IPA 中对应的 Bundle ID。应用名默认取 IPA 的 `CFBundleDisplayName` 或 `CFBundleName`，也可以首次发布时通过 `--name "应用显示名"` 确认。开发者、描述、图标参数为必填；`--init` 不能用于已有应用。未知 Bundle ID 在普通更新模式下会停止，不会误更新其他应用。需要额外应用时显式使用 `--init`。

可在完整首次发布命令末尾添加 `--dry-run`：完成真实元数据、源、凭据、权限、Git 和 Tag 检查，但不创建 Release、不改源、不提交或推送。

## 后续更新：一条命令

```bash
./scripts/upload-ipa.sh "$HOME/Downloads/MyApp-v1.0.1.ipa"
```

需要说明时加 `--notes "修复问题，增加功能"`。已有的名称、开发者、描述和图标保持不变。

版本来自 IPA，不根据文件名判断。工具要求新的 `CFBundleShortVersionString` 大于所有已发布版本，按数字比较，支持 1～3 段数字（如 `1.10.0 > 1.9.0`）。只提高 `CFBundleVersion` 不视为新更新；请在构建应用时同时提高营销版本。

每个 Release 的 Tag 为 `<BundleID>-v<CFBundleShortVersionString>-b<CFBundleVersion>`，例如结构为 `com.example.app-v1.0.0-b7`，避免多个应用都用 `v1.0.0` 时冲突。示例不是已发布版本。IPA 上传时使用稳定文件名 `<BundleID>-<版本>-<构建>.ipa`，源始终引用实际版本 Tag 和实际 Asset 名称，不使用 `latest/download`。

发布流程会：

1. 复制输入 IPA 到临时快照，检查 ZIP 完整性，读取顶层 `Payload/*.app/Info.plist`、真实大小、Bundle ID、版本、构建号、名称及可用的最低 iOS 版本。
2. 校验源、明确的首次发布元数据、当前登录、仓库权限、`main` 分支、origin、干净工作区，以及本地和远端提交一致。
3. 拒绝已存在的 Tag / Release；创建草稿并上传 IPA，确认上传状态、字节数和 SHA-256。
4. 公开 Release；使用真实 `published_at` 和 `browser_download_url` 更新源，将新版本放在最前，保留全部历史版本（因此至少保留最近三个）。
5. 再次检查工作区和远端，原子写入并校验 `source.json`，只提交此文件，正常推送 `main`。任何失败返回非零，不强推、不自动覆盖或删除 Release。

脚本输出“发布完成”表示 Release 和 Git 推送均已完成。随后仍需等待 Pages 部署，工具不会把推送成功当成 Pages 或 iPhone 安装成功。

## 在 iPhone 中添加和刷新

已安装 SideStore 后，在 Sources 页面选择添加源并粘贴：

```text
https://athlon128.github.io/AppStore/source.json
```

也可在 iPhone 上打开已对源 URL 做百分号编码的添加链接：

```text
sidestore://source?url=https%3A%2F%2Fathlon128.github.io%2FAppStore%2Fsource.json
```

以后更换源地址时，先将整个源 URL 编码，再放进 `url=` 参数，避免 `?`、`&` 等字符改变链接含义。深链接若未被当前 SideStore 版本响应，使用 Sources 手动添加。

发布更新并等待 Pages 成功后，在 Sources 中刷新源，进入应用条目安装或更新。具体按钮名称随 SideStore 版本变化；空源不会显示应用。应用更新与签名“刷新”是两件事：更新下载安装新的 IPA，签名刷新延长已有安装的签名有效期。

## Pages 配置与验证

使用 `main` 分支根目录（`/`）发布，`.nojekyll` 用于直接发布静态文件。若需要手动设置：GitHub 仓库 **Settings → Pages → Build and deployment → Deploy from a branch → main → /(root) → Save**。

检查真实部署和 JSON，避免将 HTML 404 页当成成功：

```bash
gh api repos/athlon128/AppStore/pages
gh run list --repo athlon128/AppStore
curl --fail --location https://athlon128.github.io/AppStore/source.json | python3 -m json.tool
```

源更新后若暂未变化，先确认 Actions 中 Pages 部署成功，再刷新源；Pages/CDN 可能有短暂缓存。发布工具不绕过失败的校验，也不使用额外 Token 进行部署。

## 校验与兼容性

```bash
python3 scripts/validate_source.py source.json
bash -n scripts/upload-ipa.sh
python3 -m unittest discover -s tests -v
```

Actions 在源、脚本或测试改变时运行相同校验：JSON 语法、重复键、必需字段、官方允许的字段、唯一应用标识符、非空版本列表、重复版本、真实可解析日期、版本顺序、HTTPS 和正整数字节数；失败报告具体字段路径。该工作流只读，无发布操作和额外 Secret。

`scripts/sidestore-source.schema.json` 固定取自 SideStore 官方 `sidestore-source-types` 的提交 `02d31fa6301deac10bae11e91e1316b656598882`（核对日期：2026-10-28），保留 MIT 许可在 `scripts/sidestore-source.LICENSE`。离线校验器实现此固定 schema 使用的关键字，再增加上述发布规则；将来升级 schema 时应复核校验器和测试。

初始源格式与该 schema 一致。新增应用使用 `versions`，不添加旧的顶层 `version`/`size` 等兼容字段；版本的必需字段只有 `version`、`date`、`downloadURL`、`size`。官方此 schema 不接受 `Build`、`buildVersion` 或 `marketplaceID`，所以构建号只记录在 Release Tag、标题和 Git 提交中，不写入源。根据 IPA 的 `MinimumOSVersion` 可添加 `minOSVersion`。

测试使用临时目录里的合成 IPA ZIP、模拟的 GitHub 响应和真实本地 bare Git 仓库，验证提交、推送和失败保留行为。这些测试文件并非真实可安装应用，不会上传到 Releases。

参考：[SideStore 应用源](https://docs.sidestore.io/docs/advanced/app-sources)、[官方 schema](https://github.com/SideStore/sidestore-source-types/blob/02d31fa6301deac10bae11e91e1316b656598882/schema.json)、[AltStore 源文档](https://faq.altstore.io/developers/make-a-source)。本源优先采用上面已固定核对的 SideStore schema。

## 常见问题与失败恢复

- **未安装命令 / 未登录 / 无权限**：检查环境、`gh auth status` 和账号的仓库写权限。不要把 Token 写入脚本、JSON、命令示例或仓库。
- **Git 连接 GitHub 超时**：macOS 的系统代理不会自动被命令行 Git 使用。当前发布目录已按本机现有代理设置了仅对该仓库生效的 `http.https://github.com.proxy=http://127.0.0.1:7897`，此配置在本地 `.git/config`，不进入 Git 历史。代理停用或换端口时，相应调整本仓库配置；不再需要时执行 `git config --local --unset http.https://github.com.proxy`。其他电脑请按自己的网络配置，不要盲目复制此端口。
- **工作区不干净**：先检查 `git status`、保存并提交自己的改动。工具不会 stash、重置或覆盖。输入 IPA 建议放在 Downloads；`.gitignore` 也会忽略 `*.ipa`。
- **本地与远端不一致**：先处理 `git fetch origin main`、本地未推送提交或远端更新，确认一致后重试；工具不自动合并和强推。
- **重复 Tag / 版本过旧**：默认停止。发布真正的新版本应提高 IPA 营销版本，而非覆盖旧 IPA 或改 JSON 中的版本号。
- **图标不显示**：确认填写的 URL 公开返回真实图片，而非 GitHub 文件预览网页或需要登录的链接。
- **Release 上传成功，但源更新失败**：工具会输出非零退出码和 Release 地址。先检查 `git status`、`git log -1`，按下面对应状态恢复，不重复覆盖 IPA。

如果源未修改、工作区干净，可以用同一 IPA、原有参数加 `--resume`。例如首次发布恢复时仍需原来的 `--init` 和已确认的元数据：

```bash
./scripts/upload-ipa.sh "$HOME/Downloads/MyApp.ipa" --init \
  --developer "真实开发者名称" \
  --description "真实应用说明" \
  --icon-url "https://athlon128.github.io/AppStore/icons/MyApp.png" \
  --resume
```

后续更新恢复则是 `./scripts/upload-ipa.sh "$HOME/Downloads/MyApp-v1.0.1.ipa" --resume`。恢复会检查已有 Asset 的大小和 SHA-256，必要时下载字节比对；不会上传或覆盖。如果此前只创建了空草稿、上传没有完成，应先在 GitHub 手工检查草稿，决定清理后重新发布；工具不会擅自删除。

如果 `source.json` 已修改但未提交，先确认差异，再手工完成：

```bash
git diff -- source.json
python3 scripts/validate_source.py source.json
git add -- source.json
git commit -m "Update SideStore source after verified release"
git push origin main
```

如果源已提交但推送失败，确认最新提交正确、排除网络或远端冲突后运行 `git push origin main`，不重新运行发布命令。不确定时保留文件和 Release，先检查，不自动回滚。

## 文件限制、配额与签名

GitHub 当前要求单个 Release Asset 小于 2 GiB；单个 Release 最多 1000 个 Asset，官方未对单个 Release 的总体大小或带宽设置总上限。仍须遵守服务条款及合理使用限制。工具在上传前限制单个 IPA 大小。参见 [GitHub Releases 限制](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

GitHub Pages 发布站点不超过 1 GB，通常有每月 100 GB 软带宽限制和每小时 10 次构建软限制。本仓库只通过 Pages 发布少量 JSON、脚本及可选图标，大文件 IPA 使用 Releases；免费 Actions 额度和可用功能以账号计划及 GitHub 当前政策为准。参见 [Pages 限制](https://docs.github.com/en/pages/getting-started-with-github-pages/github-pages-limits)。

应用源只提供元数据和下载，不授予签名或安装资格。SideStore 会按 Apple 账号及其规则重新签名；免费 Apple 开发者账号通常需要每 7 天刷新签名，并受同时安装应用数（通常 3 个，含 SideStore）及 App ID 配额限制，应用扩展也可能消耗 App ID。付费计划的有效期和权限不同。应按自己安装的 SideStore 版本完成配对、设备连接及需要的 VPN 配置，并在到期前刷新。参见 [SideStore FAQ](https://docs.sidestore.io/docs/faq) 及 [Apple 开发者会员对比](https://developer.apple.com/support/compare-memberships/)。

托管到 GitHub 不会移除 App Store DRM、绕过不兼容 entitlement、修复无效 IPA 或保证安装成功。请使用你有权分发且能在目标设备侧载的 IPA，真实安装结果需在 iPhone 上验证。
