# Astrbot_Plugin_MP
一个运行于astrbot平台的MP调用小工具
使用方法请使用命令"/MP菜单"查看
如需启用http转发，请在MP中添加聚合消息通知插件，选择非Get请求，并添加以下内容（亦可根据需要自行定义）：
请求头：
'''Content-Type:application/json'''
请求体
'''
{
  "title": "${title}",
  "message": "${text}",
  "type": "${type}"
}
'''
感谢KoWming大佬提供思路！

# 支持

[帮助文档](https://astrbot.app)
