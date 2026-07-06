# ZLG-CANoe Bridge 用户说明

## 工具目标

本工具用于把 Vector CANoe 的 Vector Virtual CAN / CAN FD 通道桥接到周立功 ZLG CAN / CAN FD 硬件通道，使 CANoe 可以继续使用 Diagnostic Console、CDD、ODX、DBC、CAPL、Trace 等能力访问真实 ECU。

工具只做 CAN / CAN FD 报文透明转发，不实现 UDS、ISO-TP、CDD、ODX 或 DBC 解析。

## 当前默认环境

- CANoe: `C:/Program Files (x86)/CANwin/Exec64/CANoe64.exe`
- Vector Application Name: `ZLG_CANOE_BRIDGE`
- Vector Application Channel: `0`
- ZLG hardware: `USBCANFD-100U-mini`
- ZLG deviceType: `43`
- ZLG channelIndex: `0`
- ZLG DLL: `C:/Program Files (x86)/00_SoftWare/ZXDoc/zlgcan.dll`
- 当前默认通道模式: Classical CAN

## 使用步骤

1. 关闭会占用 ZLG 设备的工具，例如 ZXDoc、ZCANPRO。
2. 确认 CANoe 工程使用 Vector Virtual CAN / CAN FD 通道。
3. 启动 GUI:

   ```bat
   tools\run_gui.bat
   ```

4. 在 GUI 中确认配置：
   - `Vector App name`: `ZLG_CANOE_BRIDGE`
   - `Vector App channel`: `0`
   - `ZLG Device type`: `43`
   - `ZLG Channel index`: `0`
   - 如果当前 CANoe 是 Classical CAN，保持 `Enable CAN FD` 不勾选。
5. 完成 license 注册。
6. 点击 `Start Bridge`。
7. 如果勾选 `Open CANoe after bridge starts`，工具会在启动桥接后自动打开 CANoe。
8. 在 CANoe 中启动 Measurement，使用 Diagnostic Console 或 Trace。

## CANoe 启动和关闭

GUI 顶部提供两个按钮：

- `Start CANoe`: 按配置中的 `canoe.exePath` 打开 CANoe；如果填写了 `canoe.configPath`，会同时打开指定工程。
- `Close CANoe`: 向 CANoe 主窗口发送关闭请求，优先温和关闭，不强制杀进程。

配置项示例：

```json
"canoe": {
  "exePath": "C:/Program Files (x86)/CANwin/Exec64/CANoe64.exe",
  "configPath": "",
  "autoStartAfterBridge": true
}
```

## License 注册说明

本工具内置轻量本机 license 注册功能，用于标识版权归属和限制未注册启动桥接。

注册信息保存位置：

```text
%PROGRAMDATA%\WDJR\ZLG_CANoe_Bridge\license.json
```

注册流程：

1. 打开 GUI，点击 `License`。
2. 查看窗口中的 `Machine ID`。
3. 输入 `Owner`，例如你的公司或个人版权名称。
4. 输入到期日，例如 `2099-12-31`。
5. 点击 `Generate Local License` 生成注册码。
6. 点击 `Register` 写入本机 license。
7. 状态显示 `License: <Owner> / <Expires>` 后，可以启动桥接。

也可以用命令行生成 license：

```bat
python tools\generate_license.py --owner "WDJR" --expires 2099-12-31
```

说明：当前 license 是离线本机绑定机制，适合内部工具版权标识和基本授权控制，不等同于强防破解商业授权系统。

## 打包生成 exe

打包前确认当前 Python 环境已有 PyInstaller：

```bat
python -m PyInstaller --version
```

执行打包：

```bat
tools\build_exe.bat
```

生成文件：

```text
dist\ZLG_CANoe_Bridge_GUI.exe
dist\config\bridge_config.json
dist\USER_MANUAL_CN.md
```

exe 图标来自：

```text
assets\app_icon.ico
```

## 已实现功能

- GUI 启动/停止桥接。
- GUI 启动/关闭 CANoe。
- 启动桥接后自动打开 CANoe。
- Vector XL Driver API 连接 Vector Virtual CAN / CAN FD。
- ZLG ZCAN API 连接 USBCANFD-100U-mini。
- Classical CAN 透明转发。
- CAN FD 配置预留，可通过 `Enable CAN FD` 开关启用。
- CH0 单通道桥接。
- `channels[]` 多通道配置结构预留。
- Echo suppression，避免短时间 TX echo 造成转发回环。
- 实时日志和 CH0 计数显示。
- 本机 license 注册和校验。
- PyInstaller 一键打包 exe。
- Windows exe / 窗口图标。

## 待实现功能

- 多通道同时运行，例如 CH0 Classical CAN + CH1 CAN FD。
- 更完整的 CAN FD 硬件烟测流程。
- GUI 中显示实时速率、错误帧、bus-off 状态。
- 自动重连 ZLG 或 Vector 通道。
- 日志按 ASC / CSV 格式保存。
- 更严格的商业 license 体系，例如非对称签名、公钥验签、授权导入导出。
- 配置向导：自动检测 CANoe Virtual Channel 和 ZLG deviceType。
- 安装包生成，例如 Inno Setup / MSI。

## 常见问题

### ZCAN_OpenDevice 返回 0

优先检查：

- ZXDoc / ZCANPRO 是否已关闭。
- `deviceType` 是否正确，USBCANFD-100U-mini 使用 `43`。
- DLL 是否与当前驱动匹配。本机已验证 ZXDoc 目录下的 `zlgcan.dll` 可以打开设备。

### CANoe 不能收到 ECU 报文

检查：

- CANoe 工程是否使用 Virtual CAN，而不是 VN/VN16xx 真实 Vector 硬件。
- Vector Application Name 是否是 `ZLG_CANOE_BRIDGE`。
- Application Channel 是否是 `0`。
- Bridge 日志是否持续出现 `[CH0 ZLG -> CANoe]`。

### CANoe 发出去 ECU 没响应

检查：

- CANoe Diagnostic Console 使用的请求 ID 是否与 ECU 一致。
- ECU 物理总线波特率是否与配置一致。
- 如果是 Classical CAN，`Enable CAN FD` 不要勾选。
- 如果是 CAN FD，确认 CANoe、Bridge、ZLG、ECU 的仲裁段/数据段/BRS/ISO 配置一致。
