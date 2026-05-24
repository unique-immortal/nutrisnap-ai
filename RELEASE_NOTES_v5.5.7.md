# NutriSnap AI v5.5.7 Release Notes

## 更新内容
1. 将调试密钥（debug.keystore）作为二进制文件提交到版本控制，确保不同构建环境下 APK 签名指纹完全一致。
2. 将签名配置（signingConfigs）直接注入到构建的 build.gradle 文件中，确保编译出的 APK 签名一致，彻底解决手动覆盖安装时出现的“签名冲突”错误。
