# 离线授权签发说明

## 直接随上位机发送

生成可在任意电脑注册的便携授权：

```bat
python tools\generate_license.py --owner "客户名称" --portable --expires 2099-12-31 --output dist\ZLG_CANoe_Bridge.lic
```

将以下两个文件一起发送给客户：

```text
dist\ZLG_CANoe_Bridge.exe
dist\ZLG_CANoe_Bridge.lic
```

客户打开上位机，进入【授权管理】，点击【导入授权文件】，选择 `.lic` 后点击【注册授权】。

便携授权可以被客户复制到其他电脑。如果需要限制为一台电脑，请使用机器绑定授权。

## 机器绑定授权

1. 客户在【授权管理】中点击【复制机器码】并发回机器码。
2. 供应方执行：

```bat
python tools\generate_license.py --owner "客户名称" --machine 客户机器码 --expires 2099-12-31 --output 客户名称.lic
```

3. 只把生成的 `.lic` 文件发给该客户。

## 私钥保护

签发私钥位于：

```text
license_private\license_private_key.json
```

该目录已加入 `.gitignore`，不会被 PyInstaller 打包。请将私钥离线备份，不要把它放入交付压缩包、代码仓库、邮件或客户电脑。客户上位机只包含 `zlg_canoe_bridge/license_public_key.py` 中的公钥。

不要重新运行密钥生成脚本或使用 `--force`，除非确定要让所有旧授权失效。
