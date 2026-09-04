import httpx
from typing import Tuple, Dict, Any

class WeatherService:
    @staticmethod
    def get_weather(city: str = "") -> Tuple[bool, str, Dict[str, Any]]:
        """获取实时天气。若 city 为空，自动基于 IP 定位本地天气。
        返回 (是否成功, 播报摘要, 原始详情)
        """
        target = city.strip() if city else ""
        url = f"https://wttr.in/{target}?format=j1" if target else "https://wttr.in/?format=j1"
        
        try:
            resp = httpx.get(url, headers={"User-Agent": "curl/7.68.0"}, timeout=6.0)
            if resp.status_code == 200:
                data = resp.json()
                current = data.get("current_condition", [{}])[0]
                area = data.get("nearest_area", [{}])[0]
                
                area_name = area.get("areaName", [{}])[0].get("value", "当前地区")
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
                    f"【{area_name}实时天气】：{desc_zh}，当前气温 {temp_c}°C（体感约 {feels_like}°C），"
                    f"空气湿度 {humidity}%。"
                )
                return True, summary, current
            else:
                return False, f"天气服务响应状态码: {resp.status_code}", {}
        except Exception as e:
            return False, f"获取天气遇到困难：{str(e)}", {}

WEATHER_SERVICE = WeatherService()
