# CANoe-ZLG CAN/CANFD 桥接工具 用户说明

版本：V1.0.0

## 1. 工具定位

CANoe-ZLG CAN/CANFD 桥接工具用于把 Vector CANoe 的 Vector Virtual CAN / CANFD 通道桥接到周立功 ZLG CAN / CANFD 硬件通道，使 CANoe 可以访问真实 ECU。

本工具只做 CAN / CANFD 报文透明转发，不实现 UDS、ISO-TP、CDD、ODX、DBC 解析。UDS、CDD、ODX、DBC、CAPL、Trace 和 Diagnostic Console 仍由 CANoe 负责。

## 2. 默认环境

- CANoe 程序路径：`C:/Program Files (x86)/CANwin/Exec64/CANoe64.exe`
- Vector 应用名称：`ZLG_CANOE_BRIDGE`
- Vector 应用通道：`0`
- ZLG 硬件：`USBCANFD-100U-mini`
- ZLG deviceType：`43`
- ZLG channelIndex：`0`
- ZLG DLL：`C:/Program Files (x86)/00_SoftWare/ZXDoc/zlgcan.dll`
- 默认通道：`CH0`
- 默认模式：`Classical CAN`

## 3. 主界面说明

主界面包含四个状态卡片：

- 桥接状态：未启动 / 运行中 / 已暂停 / 异常
- CANoe 状态：未连接 / 已启动 / 已连接 Vector Virtual CAN
- ZLG 状态：未连接 / 已连接 / 设备占用 / 打开失败
- 总线状态：未知 / 正常 / Error Passive / Bus Off

操作按钮：

- 启动桥接：先确认 CANoe 已启动；如果启用了自动启动 CANoe，会先启动 CANoe，再执行桥接自检并打开 Vector/ZLG 通道。
- 暂停桥接：停止桥接线程并释放 Vector/ZLG 通道，不在后台继续占用硬件。
- 停止桥接：停止桥接并恢复到未启动状态。
- 启动 CANoe：按配置启动 CANoe，可同时打开指定 CANoe 工程。
- 关闭 CANoe：先停止桥接并释放 Vector/ZLG 通道，再向 CANoe 主窗口发送关闭请求。
- 设备检测：尝试打开并关闭 ZLG 设备，用于确认 DLL、驱动和设备类型配置。
- 参数设置：打开悬浮参数设置窗口。
- 保存日志：将当前运行日志保存到 `logs/bridge_YYYYMMDD_HHMMSS.log`。
- 清空日志：清空主界面日志显示。

按钮状态会随桥接状态变化：

- 未启动：启动桥接可用，暂停/停止不可用。
- 运行中：启动桥接不可用，暂停/停止可用。
- 已暂停：启动桥接可用，停止桥接可用。

## 4. 参数设置

点击【参数设置】打开悬浮配置窗口，配置仍保存到：

```text
config/bridge_config.json
```

参数设置分为五个页签。

### 4.1 基本设置

- 桥接模式：原生桥接 / 调试模式
- 日志等级：DEBUG / INFO / WARNING / ERROR
- 启动桥接前自动打开 CANoe
- 启动时自动检测 ZLG 设备
- 启用回环抑制
- 回环抑制时间窗口 ms

### 4.2 CANoe 设置

- CANoe 程序路径
- CANoe 工程路径
- Vector 应用名称
- Vector 应用通道
- 浏览 CANoe
- 浏览工程
- 检测 CANoe 配置

### 4.3 ZLG 硬件设置

- ZLG DLL 路径
- ZLG 设备型号：USBCANFD-100U-mini，deviceType=43 / 自定义
- 设备类型 deviceType
- 设备索引 deviceIndex
- 通道索引 channelIndex
- 浏览 DLL
- 检测设备

### 4.4 CAN / CAN FD 参数

- 通道启用
- 通道号：CH0 / CH1
- CAN 模式：Classical CAN / CAN FD
- 仲裁波特率：125K / 250K / 500K / 1M / 自定义
- 数据波特率：500K / 1M / 2M / 4M / 5M / 自定义
- BRS：启用 / 禁用
- ISO CAN FD：启用 / 禁用
- 采样点
- 终端电阻：仅提示，不强制配置

### 4.5 高级设置

- Vector DLL 名称
- Vector App Channel
- ZLG 接收队列长度
- 发送超时时间
- 自动重连开关
- 自动重连间隔

## 5. 启动前自检

点击【启动桥接】时会先执行自检：

1. CANoe 是否已经启动；未启动且未勾选自动启动 CANoe 时，不会打开桥接。
2. License 是否有效。
3. 配置文件是否存在。
4. CANoe 程序路径是否存在。
5. ZLG DLL 路径是否存在。
6. ZLG 设备是否可打开。
7. Vector 应用名称是否为空。
8. CAN/CANFD 波特率配置是否完整。
9. Classical CAN 模式下配置了 CANFD 参数时给出提示。
10. ZCAN_OpenDevice 失败时提示可能被 ZXDoc/ZCANPRO 占用。

典型 ZLG 打开失败提示：

```text
ZLG 设备打开失败。
可能原因：
1. ZXDoc 或 ZCANPRO 正在占用设备；
2. 设备类型选择错误；
3. ZLG DLL 路径错误；
4. 驱动未安装或版本不匹配。
建议先关闭 ZXDoc/ZCANPRO，然后点击【设备检测】重新检测。
```

## 6. 授权管理

本工具内置本机授权功能，用于标识版权归属并限制未授权启动桥接。

授权文件保存位置：

```text
%PROGRAMDATA%\WDJR\ZLG_CANoe_Bridge\license.json
```

注册流程：

1. 打开软件，点击【授权管理】。
2. 复制或查看窗口中的机器码。
3. 填写版权所有者，例如个人或公司名称。
4. 填写到期日期，例如 `2099-12-31`。
5. 点击【生成本机授权码】。
6. 点击【注册授权】写入本机授权。
7. 主界面显示“授权正常”后即可启动桥接。

也可以通过命令行生成授权码：

```bat
python tools\generate_license.py --owner "WDJR" --expires 2099-12-31
```

## 7. 打包发布

打包前确认 PyInstaller 可用：

```bat
python -m PyInstaller --version
```

执行打包：

```bat
tools\build_exe.bat
```

发布目录包含：

```text
dist\ZLG_CANoe_Bridge_GUI.exe
dist\config\bridge_config.json
dist\USER_MANUAL_CN.md
dist\logs\
```

exe 图标来自：

```text
assets\app_icon.ico
```

## 8. 已实现功能

- 中文发布版 GUI。
- 主界面状态卡片、操作按钮、通道状态表格、运行日志。
- 参数设置悬浮窗口和五类配置页签。
- 启动桥接、暂停桥接、停止桥接。
- 暂停/停止时释放 Vector/ZLG 通道，不在后台持续桥接。
- 启动桥接前检查/启动 CANoe；关闭 CANoe 时联动停止桥接。
- 启动 CANoe、关闭 CANoe。
- 高频报文日志默认不刷屏，避免 GUI 未响应。
- 启动前自检。
- ZLG 设备检测。
- 中文错误提示和处理建议。
- 日志清空、日志保存。
- 本机授权注册。
- PyInstaller 打包。
- `channels[]` 多通道配置结构预留。

## 9. 待实现功能

- CH0 + CH1 多通道同时运行的完整界面编辑能力。
- 实时总线状态读取：Error Passive / Bus Off。
- 自动重连 Vector/ZLG 通道。
- 更完整的 CAN FD 硬件烟测流程。
- ASC / CSV 格式日志导出。
- 安装包生成，例如 Inno Setup / MSI。
- 更严格的商业授权体系，例如非对称签名、公钥验签、授权导入导出。

## 10. 常见问题

### ZCAN_OpenDevice 返回 0

优先检查：

- ZXDoc / ZCANPRO 是否已关闭。
- `deviceType` 是否正确，USBCANFD-100U-mini 使用 `43`。
- `deviceIndex` 是否正确，单设备通常为 `0`。
- DLL 是否与驱动版本匹配。
- DLL 和 EXE/Python 是否同为 64 位或同为 32 位。

### CANoe 收不到 ECU 报文

检查：

- CANoe 工程是否使用 Vector Virtual CAN / CANFD，而不是 VN 真实硬件通道。
- Vector Application Name 是否与工具配置一致。
- Application Channel 是否一致。
- ZLG 物理通道是否连接真实 ECU。
- ECU 波特率是否与工具配置一致。

### ECU 没有响应 CANoe 请求

检查：

- Diagnostic Console 请求 ID 是否与 ECU 一致。
- Classical CAN / CAN FD 模式是否一致。
- CAN FD 的仲裁段、数据段、BRS、ISO CAN FD 配置是否一致。
- 是否误把其他工具保持在占用 ZLG 硬件状态。
