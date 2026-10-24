"""内建消息聚合阶段（从独立插件 `astrbot_plugin_msg_aggregator` 迁入）。

对外只暴露 `AggregationService` 与配置加载函数；具体模块保持独立，便于把独立
插件的测试与实现逐项对照。
"""

from .config import Config, load_config
from .service import SEEN_ATTR, AggregationService

__all__ = ["AggregationService", "Config", "SEEN_ATTR", "load_config"]
