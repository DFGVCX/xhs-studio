# 参与开发

本仓库采用小步修改、自动测试和本地真实浏览器验收的方式维护。不要提交账号 Cookie、浏览器配置、签名链接、采集结果或任何第三方个人数据。

## 本地环境

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
```

## 提交前检查

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --test tests/browser_viewer_protocol.test.cjs
```

涉及浏览器画面或输入协议时，再运行 README 中的本地真实浏览器测试。真实小红书冒烟测试必须使用独立测试账号，并且默认不运行。

## 变更要求

- 保持服务只监听回环地址，不扩大网络暴露范围。
- 保持 WebDriver 单线程所有权，以及自动化和人工输入之间的状态锁。
- 新增配置项时同步补充校验、默认值、前端表单、文档和测试。
- 不通过伪装指纹、代理池或绕过验证的方式规避平台风控。

