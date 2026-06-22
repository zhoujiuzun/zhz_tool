@echo off
REM 双击入口:调用 build.ps1 完成 打包 + zip + 安装包。
REM 发布(上传到已存在的 Release):命令行跑  build.bat -Publish
REM 只重打包不重新 PyInstaller: build.bat -SkipBuild
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
echo.
pause
