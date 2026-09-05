# 红薯工作台 · XHS Studio

一个在自己电脑上运行的小红书内容整理工具。它把真实 Chrome / Edge 浏览器嵌入工作台，在同一页面中完成浏览、登录、采集、查看进度和导出结果。

[下载最新版](https://github.com/DFGVCX/xhs-studio/releases/latest) · [反馈问题](https://github.com/DFGVCX/xhs-studio/issues) · [安全说明](SECURITY.md)

> 本项目用于个人资料整理与经过授权的内容备份。请遵守目标平台规则、著作权与隐私要求。项目不会绕过验证码、安全验证或访问限制。

## 下载即用（推荐）

适用于 Windows 10 / 11 64 位系统，不需要安装 Python，也不需要执行命令。

1. 打开 [Releases](https://github.com/DFGVCX/xhs-studio/releases/latest)，下载 `XHS-Studio-Windows-x64-版本号.zip`。
2. 将 ZIP **完整解压**到一个普通文件夹，不要直接在压缩包内运行。
3. 双击 `XHS-Studio.exe`。工作台会自动打开 `http://127.0.0.1:8765`。
4. 保持启动窗口开启；关闭该窗口即退出工作台。

电脑还需要满足两个条件：已安装 Chrome 或 Edge，并且首次启动浏览器驱动时能够联网。应用没有购买代码签名证书，Windows 首次运行可能显示 SmartScreen 提示；请只从本仓库 Release 下载，并用同页提供的 SHA256 文件校验压缩包。

## 第一次使用

1. 在「采集设置」中填写关键词，或切换到「指定链接」并粘贴小红书笔记链接。
2. 按需调整最多篇数、采集间隔、图片下载和本地保存位置。
3. 点击「开始采集」。程序会先打开小红书首页并检查登录状态。
4. 尚未登录时，在上方内嵌浏览器中扫码或手动登录，然后点击「已处理，继续」。
5. 完成后在「采集记录」下载 JSON、CSV、Markdown 或包含图片的 ZIP。

默认结果保存在程序旁边的 `Information/` 文件夹。你也可以在页面中选择其他本地目录。登录状态保存在 `runtime/profiles/`，下次打开仍可使用。

## 能做什么

- 关键词搜索采集，或批量处理指定笔记链接
- 在工作台内直接操作真实 Chrome / Edge 页面
- 地址栏自由访问任意 HTTP / HTTPS 网站，采集功能仍只处理小红书笔记
- 开始、暂停、继续、停止和失败重试
- 自动保存正文、作者、发布时间、原始链接和图片
- 已保存内容跳过、逐篇落盘和断点续采
- JSON、CSV、Markdown、图片及完整 ZIP 导出
- 高清 / 流畅画质切换，自适应浏览器画面尺寸
- 自选本地保存路径，配置与登录会话仅保留在本机

## 常见问题

### 双击后页面没有打开

先查看启动窗口里的提示，也可以手动访问 `http://127.0.0.1:8765`。如果端口被其他程序占用，请先关闭旧的工作台窗口再重试。

### 提示没有找到 Chrome 或 Edge

安装任意一个最新版 Chrome 或 Edge，关闭工作台后重新打开。程序会自动识别浏览器并准备相匹配的驱动。

### 小红书提示“安全限制 / IP 存在风险 / 300012”

这是目标网站根据当前网络环境给出的访问限制，不代表工作台安装失败。请停止任务，确认普通浏览器在同一网络下可以访问，必要时切换到可信网络后再试。工具不会自动规避平台风控。

### 为什么开始采集后要求登录

启动浏览器不等于已登录。工作台只有在进入小红书首页并检测到真实登录会话后才会继续采集。Release 不包含开发者或其他用户的账号数据。

### 可以用内嵌浏览器访问其他网站吗

可以。点击「打开浏览器」后可在地址栏输入任意有效的 HTTP / HTTPS 地址。为了安全，`file:`、`javascript:`、`data:` 等协议以及带账号密码的网址不会被接受。

### 数据会上传到服务器吗

不会。控制台只监听本机 `127.0.0.1`，配置、登录状态和采集结果都保存在本地。程序访问目标网页和图片地址时仍会正常产生网络请求。

### 如何彻底清除本机数据

退出工作台后，删除程序目录中的 `runtime/` 可清除配置和独立浏览器登录会话；删除 `Information/` 可清除默认位置的采集结果。若使用过自定义保存目录，还需自行处理该目录中的结果。

## 更新

下载新的 Release ZIP 并解压到新文件夹。若要保留登录状态和历史配置，退出旧版本后，将旧目录中的 `runtime/` 复制到新目录；采集结果可继续留在原位置，或将 `Information/` 一并复制。

## 从源码运行

开发者或希望检查源码的用户需要 Python 3.10+。Windows 双击 `start.cmd`，脚本会自动创建 `.venv` 并安装锁定依赖；也可以执行：

```powershell
.\start.ps1
```

手动安装：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe run_console.py
```

追加 `--port 8766` 可修改端口，追加 `--no-open` 可禁止启动后自动打开页面。Node 仅用于开发测试，普通运行不需要。

## 开发与测试

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/browser_viewer_protocol.test.cjs
```

构建 Windows 便携版：

```powershell
.\scripts\build_release.ps1
```

产物位于 `dist/XHS-Studio-Windows-x64-版本号.zip`。推送 `v*` 标签后，GitHub Actions 会在 Windows 环境重新构建、生成 SHA256，并发布 Release。

## 数据与安全边界

- `runtime/`：本地设置、独立浏览器会话和断点记录
- `Information/`：默认采集结果和导出文件
- 服务仅监听本机回环地址，不应通过端口映射暴露到公网
- Git 已排除登录会话、配置、采集内容、日志和虚拟环境
- 不包含代理池、指纹伪装、验证码破解、评论采集、OCR、视频转写或完整视频下载

参与开发请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。安全问题请按照 [SECURITY.md](SECURITY.md) 私密报告。

## 开源许可

项目代码使用 [MIT License](LICENSE)。界面字体来自站酷快乐体和霞鹜文楷，字体来源与对应许可位于 `static/fonts/`。
