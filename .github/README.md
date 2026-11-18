# CreativeLab

用于研究人智协作与数字创意的 Python 实验平台，包含受试者任务、研究后台、材料版本、分组、过程记录、盲评与分析导出。

## 本机运行

使用 Python 3.12。在克隆后的项目目录执行：

```bash
python deployment/bootstrap.py
python run.py --local-only
```

受试者入口：`http://127.0.0.1:8765/p/pilot/`；研究后台：`http://127.0.0.1:8766/research/`。首次运行生成独立的 `data` 和后台密钥；密钥从本机 `data/_system/researcher.key` 读取。实际生产站点的网址、邀请口令和密钥不在本仓库中。

默认材料在 `study/materials/default.json`。已运行站点的方案草稿和发布版本存储在数据库中，应在后台编辑。

## 代码结构

```text
run.py                  启动、参数、环境检查和数据目录锁
requirements.txt        固定版本的 Python 依赖
study/
  engine.py             实验状态机、分组与任务执行
  storage.py            SQLite、密钥和参与者文件镜像
  http.py               受试者/后台路由、登录和会话保护
  research.py           后台查询、盲评、评分与分析导出
  launch.py              HTTP 服务与公网状态
  images.py              图像生成插件接口
  tunnel.py              Cloudflare 隧道与 HTTPS 自检
  deploy.py              Windows 便携安装与原数据保护
  config.py              方案校验与材料配置
  materials/default.json 初始化材料
  web/                   受试者与研究后台前端
deployment/
  manage.py              查看改动、阶段保存、GitHub 发布和代码部署
  version_policy.py      可公开路径及凭据检查
  server_release.py      服务器预检、备份、应用更新与代码回退
  bootstrap.py           Git 克隆后的依赖准备
  backup.py              含一致 SQLite 快照的私有整站备份
  server.example.json    不含真实主机/凭据的配置示例
  version-control.md     工作流、命令与恢复说明
  file_inventory.py      本机与服务器逐文件对照清单
  server_inventory.py    私有服务器文件与数据元数据扫描
tests/                   临时数据测试，不读取真实受试者记录
.githooks/               提交与推送前的私密信息检查
Project_Tools.bat         Windows 操作菜单
AGENTS.md                协作约定：阶段提交由项目所有者决定
```

## 版本与数据

日常修改、测试网站部署、Git 阶段提交分别执行。只有明确决定保留的阶段才创建提交；没有自动提交或自动部署。详见 [版本与同步说明](../deployment/version-control.md)。

`data/`、`private/`、`.runtime/`、`backups/`、`logs/`、根目录私有 `README.md`、机器配置、生成清单和二进制运行环境均不进入 Git。GitHub 保存可公开源代码；实验数据使用私有整站备份保存和迁移。

## 验证

```bash
python tests/verify_flow.py
python tests/verify_deploy.py
python tests/verify_version_control.py
python deployment/manage.py check
```

## 服务器

服务器使用 Ubuntu、Python 3.12、systemd 与 Cloudflare Tunnel。应用端口仅监听回环地址，后台通过 HTTPS 密钥登录。将配置示例复制为私有 `private/server.json`，填入自己控制的服务器及独立 SSH 密钥，保留并验证 SSH 主机指纹。服务器依赖及服务需先行配置。

本仓库不授予对任何现有生产站点、数据库或服务器的访问权限。
