import httpx
from typing import Tuple, Dict, Any

import httpx
from typing import Tuple, Dict, Any
from .config_manager import APP_CONFIG

class WeatherService:
    @staticmethod
    def _locate_local_city() -> str:
        """尝试通过高可靠国内 IP 接口获取城市名兜底"""
        try:
            # 太平洋电脑网国内 IP 纯净接口（无需 key，响应 <100ms）
            resp = httpx.get("https://whois.pconline.com.cn/ipJson.jsp?json=true", timeout=2.0)
            if resp.status_code == 200:
                data = resp.json()
                city = (data.get("city") or "").replace("市", "").strip()
                if city:
                    return city
        except Exception:
            pass
        return ""

    @classmethod
    def get_weather(cls, city: str = "") -> Tuple[bool, str, Dict[str, Any]]:
        """获取实时天气。若 city 为空，优先从配置或 IP 获取本地城市，杜绝海外服务解析漂移。
        返回 (是否成功, 播报摘要, 原始详情)
        """
        target = (city or "").strip()
        # 清洗可能误传的指令修饰词
        noise_words = ["查询", "查一下", "看下", "看看", "今天", "今日", "现在", "如何", "怎么样", "天气", "预报"]
        for nw in noise_words:
            target = target.replace(nw, "").strip()

        # 若用户没有指定具体的城市名，强制使用配置中的城市或国内 IP 接口定位城市
        if not target:
            target = APP_CONFIG.get("pet", "weather_city", "") or cls._locate_local_city() or "武汉"

        url = f"https://wttr.in/{target}?format=j1"
        
        try:
            resp = httpx.get(url, headers={"User-Agent": "curl/7.68.0"}, timeout=6.0)
            if resp.status_code == 200:
                data = resp.json()
                current = data.get("current_condition", [{}])[0]
                area = data.get("nearest_area", [{}])[0]
                
                area_name = area.get("areaName", [{}])[0].get("value", target or "当前地区")
                # 如果传入了中文城市但 wttr.in 显示了拼音，可友好显示中文
                display_city = target if target else area_name
                temp_c = current.get("temp_C", "--")
                feels_like = current.get("FeelsLikeC", temp_c)
                humidity = current.get("humidity", "--")
                desc_en = current.get("weatherDesc", [{}])[0].get("value", "Clear")
                
                # 简单天气描述中文化
                desc_map = {
                    "Sunny": "晴朗", "Clear": "晴天", "Partly cloudy": "多云", "Cloudy": "阴天",
                    "Overcast": "阴天/多云", "Mist": "薄雾", "Fog": "大雾", "Light rain": "小雨",
                    "Moderate rain": "中雨", "Heavy rain": "大雨", "Patchy rain possible": "局部有雨",
                    "Thundery outbreaks possible": "雷阵雨可能", "Light snow": "小雪"
                }
                desc_zh = desc_map.get(desc_en, desc_en)

                summary = (
                    f"【{display_city}实时天气】：{desc_zh}，当前气温 {temp_c}°C（体感约 {feels_like}°C），"
                    f"空气湿度 {humidity}%。"
                )
                return True, summary, current
            else:
                return False, f"天气服务响应状态码: {resp.status_code}", {}
        except Exception as e:
            return False, f"获取天气遇到困难：{str(e)}", {}

WEATHER_SERVICE = WeatherService()
