# CANoe 配置说明

## 正确链路

```text
CANoe CANFD Channel 1
  ↓ Vector Virtual CAN 1
Python Bridge
  ↓ ZLG CANFD Channel 0
真实 ECU / ACU
```

## 1. 新建最小 CANFD 工程验证

建议先不要用原工程，先新建最小工程排查：

```text
File → New → CAN/CAN FD configuration
```

然后把 CAN1 配置成 Virtual CAN。

目标是：

```text
CANoe 可以 Start Measurement
Trace 窗口正常打开
不再报 VN800/VN890
```

## 2. 原工程迁移

如果原工程来自别人电脑，里面可能绑定了 VN800/VN890/VN16xx 真实硬件。需要检查：

```text
Network Hardware / Channel Assignment
Diagnostic Channel Assignment
Simulation Setup
CAPL 节点通道绑定
```

所有不用的真实硬件通道先禁用。

## 3. Vector Hardware Config

打开 Windows 开始菜单：

```text
Vector Hardware Config
```

新增或修改应用：

```text
Application: ZLG_CANOE_BRIDGE
Application Channel 0 → Virtual CAN 1
```

Python 配置文件要对应：

```json
"app_name": "ZLG_CANOE_BRIDGE",
"applicationChannel": 0
```

## 4. CANFD 参数

CANoe、Python Bridge、ZLG、ECU 必须一致：

```text
Arbitration Bitrate: 500 kbit/s
Data Bitrate: 2 Mbit/s
BRS: Enable
ISO CAN FD: Enable
```

如果任意一边不一致，通常表现为：

```text
错误帧
收不到响应
BusOff
CANoe Trace 只有发送没有接收
```
