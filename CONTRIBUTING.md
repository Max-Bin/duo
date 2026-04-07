# 贡献指南

感谢你对 Duo 项目的兴趣！以下是参与贡献的指南。

## 开发环境

```bash
# 克隆项目
git clone https://github.com/user/duo.git
cd duo

# 一键安装
bash install.sh

# 或手动安装
uv sync
uv pip install -e .
```

## 开发流程

1. Fork 项目并创建功能分支
2. 编写代码和测试
3. 确保所有测试通过：`python -m pytest tests/ -v`
4. 确保类型检查通过：`python -m mypy src/duo/ --ignore-missing-imports`
5. 提交 Pull Request

## 代码规范

- Python 3.12+，使用 type hints
- 所有公共函数需要完整的类型注解
- 使用 `from __future__ import annotations`
- 数据模型使用 `dataclass`，不用 dict
- Transport 抽象层：不直接调用 tmux，通过 `duo.transport` 模块
- Journal 是 append-only JSONL，不修改已有条目
- 原子写入：使用 `write_json` 的 tmp+rename 模式

## 测试

- 每个新功能需要对应的测试
- 使用 `tmp_path` fixture 隔离文件操作
- 使用 `monkeypatch` 覆盖 `TASKS_DIR`，避免污染 `~/.duo`
- Mock subprocess.run 用于 git/tmux-bridge 调用
- CLI 测试使用 `click.testing.CliRunner`

## 模块结构

| 模块 | 职责 |
|------|------|
| `protocol.py` | FSM、数据模型、文件 I/O（状态真相） |
| `commander.py` | 编排逻辑、prompt 模板 |
| `transport.py` | tmux-bridge 封装 |
| `poller.py` | 自适应轮询 |
| `verifier.py` | 质量门禁 |
| `config.py` | 配置管理 |
| `cli.py` | Click CLI 入口 |

## 提交规范

使用 conventional commits：
- `feat:` 新功能
- `fix:` Bug 修复
- `test:` 测试
- `docs:` 文档
- `refactor:` 重构

## 报告 Bug

请在 Issues 中报告，包含：
- 复现步骤
- 期望行为 vs 实际行为
- 操作系统和 Python 版本
