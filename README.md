# Windows GUI MCP Server

这是一个仅面向 Windows 的 FastMCP Server，为 AI 客户端提供鼠标、键盘、窗口聚焦、截图、Windows UI Automation 和菜单操作能力。

项目保留 `windows_gui_mcp.py` 作为兼容启动入口，具体实现按职责放在 `windows_gui/` 包中：

- `server.py`：共享 FastMCP 实例和 PyAutoGUI 安全设置。
- `mouse.py`：鼠标、滚轮、拖动和截图。
- `keyboard.py`：文本输入、按键和快捷键。
- `windows.py`：窗口枚举、聚焦及聚焦后的输入操作。
- `uia.py`：UI Automation 控件、菜单和保存对话框操作。

## 环境要求

- Windows 10 或 Windows 11
- Python 3.10 或更高版本
- 可交互的桌面会话

安装依赖：

```powershell
python -m pip install fastmcp pyautogui pywin32 pywinauto pillow
```

`pyautogui` 的故障保护已开启。把鼠标快速移动到屏幕左上角可中止 PyAutoGUI 操作。

## 启动

在项目根目录运行：

```powershell
python windows_gui_mcp.py
```

VS Code MCP 配置位于 `.vscode/mcp.json`，通过 stdio 启动同一个兼容入口。

## 测试

运行所有不操作真实桌面的单元测试：

```powershell
python -m unittest discover -s tests -t . -v
```

运行语法编译检查：

```powershell
python -m compileall -q windows_gui_mcp.py windows_gui tests
```

运行真实 Windows GUI smoke test：

```powershell
python tests/smoke_test.py
```

Smoke test 只使用唯一命名的专用记事本文件，测试结果写入 `tests/smoke_artifacts/`。它不会删除文件、发送消息、关闭程序或操作已有文档；记事本会保留在桌面供人工确认。日志中的 `MANUAL CHECK` 表示需要观察截图或桌面状态。

## MCP 工具

当前服务器注册 23 个工具。

| 工具 | 用途 |
|---|---|
| `get_mouse_position` | 返回当前鼠标光标坐标。 |
| `move_mouse` | 把鼠标移动到指定屏幕坐标。 |
| `click_mouse` | 在当前位置单击左键、右键或中键。 |
| `screenshot` | 截取当前桌面并作为 FastMCP Image 返回。 |
| `double_click` | 在当前位置执行双击。 |
| `right_click` | 在当前位置执行右键单击。 |
| `scroll` | 发送 Windows 鼠标滚轮事件。 |
| `drag_mouse` | 从当前位置拖动到指定坐标。 |
| `focus_and_press` | 点击指定坐标取得焦点，然后按一个键。 |
| `type_text` | 向当前聚焦输入区域输入文本。 |
| `press_key` | 使用 Windows 键盘事件按下一个受支持的按键。 |
| `hotkey` | 执行由按键列表描述的快捷键。 |
| `list_windows` | 列出可见顶层窗口标题。 |
| `focus_window` | 按标题匹配并聚焦可见窗口。 |
| `focus_window_and_press` | 聚焦匹配窗口后按一个键。 |
| `focus_window_and_hotkey` | 聚焦匹配窗口后执行快捷键。 |
| `focus_window_and_type` | 聚焦匹配窗口后输入文本。 |
| `focus_window_and_scroll` | 聚焦匹配窗口、移动到窗口中心并滚动。 |
| `list_controls` | 列出匹配窗口中最多 150 个有用 UIA 控件。 |
| `click_control` | 按名称及可选控件类型激活 UIA 控件。 |
| `click_menu_item` | 打开指定菜单并激活其中的菜单项。 |
| `set_save_dialog_filename` | 在 Windows 保存对话框中设置文件名。 |
| `click_save_button` | 激活 Windows 保存对话框中的保存按钮。 |

## 安全说明

- GUI 操作会影响当前交互式桌面。调用前应确认目标窗口标题足够具体。
- 自动化测试必须 mock 所有真实鼠标、键盘和 UIA 副作用。
- 真实 GUI 验证应只使用 `tests/smoke_test.py` 创建的专用文件和窗口。
- 截图、Python 缓存和 smoke artifacts 已由 `.gitignore` 排除。
