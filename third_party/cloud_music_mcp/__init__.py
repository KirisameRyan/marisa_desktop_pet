"""网易云音乐本地库（从 cloud-music-mcp 迁移而来，只保留 auth/api/log）。

直接 import 使用，不做 MCP Server。存储目录默认在本包 storage/，
使用前可通过 ncm_client 设置该包模块的 STORAGE_DIR 指到项目 data/ 下。
"""
