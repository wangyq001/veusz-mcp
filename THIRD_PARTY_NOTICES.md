# 第三方来源与许可证说明

## 本项目

Copyright (C) 2026 wangyq001 和项目贡献者。

本项目原创代码使用 `GPL-2.0-or-later`：允许按 GNU GPL 第 2 版或任意更高版本使用、修改及再分发。完整第 2 版许可证文本位于 [LICENSE](LICENSE)。文档中的此项声明授予“或更高版本”的选择。

## Veusz

- 上游：<https://github.com/veusz/veusz>
- 版权：Jeremy Sanders 和 Veusz 贡献者；具体年份及署名以上游对应文件为准。
- 上游许可：GNU GPL 第 2 版或任意更高版本。
- 本项目通过插件和原生文档 API 与独立安装的 Veusz 交互，不附带 Veusz、Qt 或 Python 安装包，不代表上游官方项目。
- `LICENSE` 中的 GNU GPL 第 2 版正文取自上游 `COPYING`，正文版权归 Free Software Foundation 所有，未修改其条款。

## MCP Python SDK 及 Python 依赖

- MCP Python SDK：<https://github.com/modelcontextprotocol/python-sdk>
- 安装依赖由 `requirements.txt` 指定，维护者依赖快照见 `requirements-lock.txt`。
- 本仓库不将这些依赖的实现代码或二进制打包分发；依赖各自的版权和许可证保持不变，安装或再分发依赖时请遵循其随附声明。

## 翻译与绘图 Skill

- Veusz 官方翻译来源：<https://github.com/veusz/veusz/tree/master/translation>。
- 不分发本地编译的翻译 `.qm` 文件，也不分发第三方绘图 Skill。
- README 配图和 `examples/simulated_robustness.csv` 使用本项目生成的模拟数据，不含用户真实实验数据，也未复制第三方论文图。
