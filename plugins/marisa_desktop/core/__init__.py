# -*- coding: utf-8 -*-
"""桌宠本机能力包（配置、TTS、录音、窗口工具、MaiBot 客户端）。

显式声明为常规包：宿主 .venv 的 editable 安装会把项目 ``src`` 目录加进
``sys.path``，其中 ``src/core`` 也是常规包，会在命名空间包解析中抢走本目录；
补上 ``__init__.py`` 后，只要本目录在 ``sys.path`` 中靠前（run_pet.py 会保证），
``import core.xxx`` 就固定落在插件自己的实现上。
"""
