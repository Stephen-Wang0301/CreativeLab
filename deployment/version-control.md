# 版本、服务器同步与数据恢复

工作目录固定使用 `004CreativeLab`。修改、部署和保存版本均在这个目录内进行，无需关闭 Codex 或重新命名文件夹。

## 三件事分别控制

| 操作 | 结果 | 是否产生 Git 提交 |
| --- | --- | --- |
| 修改本机代码 | 留在工作区，随时继续调整 | 否 |
| 更新测试网站 | 上传当前代码，预检、备份并短暂重启实验服务 | 否 |
| 保存阶段版本 | 按你给出的说明创建一个本地提交 | 是，仅在你决定时 |
| 上传 GitHub | 推送已经保存的提交 | 不创建新提交 |

你可以直接告诉协作助手“更新测试网站，暂不存版”“把这一阶段保存，说明是……”或“把已保存版本上传 GitHub”。阶段由你决定，不按次数、日期或每次聊天自动划分。

Windows 可双击根目录 `Project_Tools.bat` 使用菜单。也可在项目目录运行以下命令。使用已配置 Python 3.12 的 `skillcourse` 环境。

```powershell
python deployment/manage.py status
python deployment/manage.py check
python deployment/manage.py deploy --working-tree
python deployment/manage.py save -m "预试流程确认，等待下一轮测试"
python deployment/manage.py publish
```

保存时显示改动并要求输入 `SAVE`；推送时要求输入 `PUBLISH`。发布到 GitHub 不会部署到网站。命令中的 `--confirm` 仅供已经得到你明确确认的助手执行，不能作为默认自动选项。

## 隐私边界

- 根 `README.md` 是私人使用说明，可以保留实际网址、邀请口令和后台密钥。GitHub 展示的是 `.github/README.md`。
- `data/` 保存数据库、方案草稿、发布材料、密钥和受试者记录；以服务器上的副本为准。
- `private/server.json` 保存本机部署连接参数；`.runtime/deploy_ed25519` 是专用 SSH 私钥，服务器仅登记其公钥。GitHub 凭据保留在本机凭据管理器，不传给服务器。
- `.runtime/`、`backups/`、`logs/`、平台运行环境、生成清单、实际 systemd/隧道配置均不上传 GitHub。
- Git 忽略规则加可公开路径检查；提交前检查暂存内容，推送前还检查 Git 历史。检查不会显示匹配的密码。扫描只能识别已知私密值和常见令牌格式，保存前仍需审阅改动。

Git 的忽略规则不影响已经被跟踪的文件，因此还需检查历史内容，不能只看当前目录是否删除了敏感文件。参见 [Git 忽略规则](https://git-scm.com/docs/gitignore) 与 [GitHub 敏感数据处理说明](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository)。

## 代码同步到服务器

首次配置由维护人员复制 `server.example.json` 到 `private/server.json`，设置并验证 SSH 密钥和主机指纹。实验服务的启停需要授权；此项目仅为专用部署账号配置对实验服务 `start/stop` 的有限授权，不授权其他服务或任意 sudo 命令。

部署工具先比较服务器代码哈希与本机保存的服务器基线，发现外部改动就停止。下载服务器代码到独立的私有目录进行比较：

```powershell
python deployment/manage.py server-status
python deployment/manage.py fetch-code
```

核对并合并改动后，明确接受当前服务器基线：

```powershell
python deployment/manage.py server-status --accept-baseline
```

部署支持两种来源：

```powershell
python deployment/manage.py deploy --working-tree
python deployment/manage.py deploy --ref main
```

第一种包含未提交的当前代码，供反复测试；第二种使用指定 Git 提交、分支或标签的已保存代码。两者都会记录来源、文件哈希、部署时间和回退编号，不把临时部署记录变成 Git 阶段提交。

实际流程：独立目录启动预检 → 生成服务器整站备份 → 保存部署前后的源代码 → 暂停实验服务 → 仅替换允许的源代码文件 → 启动服务 → 检查应用、受试者页、后台页及公网 HTTPS。Cloudflare 隧道及其他业务服务不随代码部署重启。更新有短暂停机，测试/招募期间应安排在无人作答时执行。

同步不上传本机 `data`，不覆盖线上方案、作答、密码和 Cloudflare 令牌。第三方依赖和机器配置也不自动替换；以后如改动 `requirements.txt`，需先单独准备匹配的服务器依赖，再部署。

## Git 回退与服务器回退

查看已保存的阶段：

```powershell
git log --oneline --decorate
```

试运行某个旧提交可用 `deploy --ref 提交编号`。无需先改本机文件，也无需删除后续历史。需要把旧代码作为新的开发起点时，先保留当前工作区，再由维护人员选择分支或反向提交；不要直接强制重置含未保存改动的工作区。

每次部署返回 `release_id` 和 `rollback_before_this_deploy`。根据编号恢复部署后的代码，或恢复那次部署之前的代码：

```powershell
python deployment/manage.py rollback 部署编号
python deployment/manage.py rollback before-部署编号
```

回退会先备份，再恢复代码并验证服务。它不把数据库回到过去，因此不会为了回退界面而删除后来新增的作答。未来涉及不兼容数据库迁移的版本，需另行设计迁移与恢复方案，不能只依赖代码回退。

## 后台内容与数据备份

后台“方案与招募”的编辑保存到服务器数据库，刷新后保留；后台“保存为新版本”管理的是实验材料版本，与这里的 Git 代码版本各自独立。

随时下载整个站点，包括两套数据、材料版本、草稿、评分、密钥和迁移凭据：

```powershell
python deployment/manage.py backup
```

文件落在本机私有 `backups/`，下载后验证 SHA-256。它适合迁移到下一台服务器或恢复到本机。GitHub 上的代码和私有备份合起来才是完整恢复路径。后续代码保存和数据备份各自按需执行，没有配置按日自动任务。

## 换电脑或服务器

克隆 GitHub 仓库后运行 `python deployment/bootstrap.py` 安装固定依赖并启用检查钩子。下载最新私有整站备份，按服务器操作说明恢复完整 `data` 和私有凭据。新服务器重新安装对应平台的 Python、cloudflared 和 systemd 服务，再在私有配置中更新连接目标。现有站点可用前保留旧服务器副本。

服务器 SSH 登录密码不存入部署配置。专用私钥丢失时，可用原来的 SSH 登录方式重新配置公钥。撤销专用同步通路时，仅从服务器 `~/.ssh/authorized_keys` 移除注释为 `creative-study-deploy` 的公钥，并移除为实验服务添加的 sudoers 文件；保留其他人的密钥和权限。
