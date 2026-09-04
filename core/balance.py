import httpx
from typing import Dict, Any, Tuple
from .config_manager import APP_CONFIG

class DeepSeekBalanceChecker:
    @staticmethod
    def query_balance(api_key: str = None) -> Tuple[bool, str, Dict[str, Any]]:
        """查询 DeepSeek 账户余额
        返回 (是否成功, 友好消息文本, 原始数据字典)
        """
        key = api_key or APP_CONFIG.get("deepseek", "api_key", "")
        if not key or key == "YOUR_DEEPSEEK_API_KEY_HERE":
            return False, "尚未配置有效的 DeepSeek API Key だぜ！", {}

        url = "https://api.deepseek.com/user/balance"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {key}"
        }

        try:
            resp = httpx.get(url, headers=headers, timeout=8.0)
            if resp.status_code == 200:
                data = resp.json()
                is_available = data.get("is_available", True)
                infos = data.get("balance_infos", [])
                if infos:
                    curr_info = infos[0]
                    currency = curr_info.get("currency", "CNY")
                    total = curr_info.get("total_balance", "0.00")
                    granted = curr_info.get("granted_balance", "0.00")
                    topped_up = curr_info.get("topped_up_balance", "0.00")
                    
                    symbol = "¥" if currency == "CNY" else "$"
                    msg = (
                        f"可用总额: {symbol}{total} {currency} "
                        f"(赠送: {symbol}{granted} / 充值: {symbol}{topped_up})"
                    )
                    return True, msg, data
                return True, "查询成功，未找到余额明细", data
            else:
                return False, f"查询失败 (HTTP {resp.status_code}): {resp.text[:80]}", {}
        except Exception as e:
            return False, f"请求异常: {str(e)}", {}

BALANCE_CHECKER = DeepSeekBalanceChecker()
