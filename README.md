# Origin 批量排版助手 v0.6

Windows 本地助手：批量打开 `.opju/.opj` 项目，将独立图页和布局页导出为 PNG，选择 2–12 张后生成 `.figgrid`，并自动送入 Layout Assistant。

本仓库是独立的 Origin 导出工具源码库。排版网站位于 [Resky0/layout-assistant](https://github.com/Resky0/layout-assistant)，两者通过版本化 `.figgrid` 文件和本机一次性 HTTP bridge 连接。

## 使用

1. 保存并关闭正在运行的 Origin。
2. 打开“Origin 批量排版助手”。
3. 添加项目文件或包含项目的文件夹，选择输出目录。
4. 点击“检测环境”确认 Origin 可隐藏启动，再点击“扫描并导出全部图页”。
5. 勾选 2–12 张图片、调整顺序，然后点击“生成 `.figgrid` 并一键排版”。

导出过程中会显示每个项目的当前阶段。任务取消后可在当前窗口继续；超时、无图页或启动失败的项目可单独重试。

每次运行会生成：

```text
所选输出目录/
├─ image/
├─ origin-logs/
├─ export.log
├─ selected.figgrid
└─ export-report.json
```

自动导入失败时，在 Layout Assistant 的“我的工程”页面手动导入 `selected.figgrid` 即可。

## 开发

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
python -m origin_bridge
./build.ps1
```

`build.ps1` 会先运行全部 Python 测试，再生成 `release/Origin批量排版助手/`。构建产物、虚拟环境和发布压缩包不会提交到 Git。

当前版本面向 Windows + Origin 2018 及以上版本；Origin 2021 已完成真实项目验证，其他版本会在运行前执行版本与隐藏启动检测。Origin 2017 及更早版本只能处理 `.opj`，不能打开 `.opju`。

助手仅导出项目中的独立图页和布局页，不处理工作表内嵌图。所有操作针对纯英文临时副本；脚本结束前使用 `doc -s` 清除临时项目的修改标记，不会保存或修改源项目。
