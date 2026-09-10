"""资源类汇总导入（2.48.0）：**导入本模块即完成全部资源登记**。

各资源类在定义时由 `BaseBiz.__init_subclass__` 自动登记（继承即登记，无工厂映射表），
本模块只负责把它们集中 import 进来，保证登记副作用发生。

新增资源 = ① 在对应 `biz.py` 写类并声明 `_biz_type`；② 在此处加一行 import。
"""

from app.services.interaction_actions.comment.biz import CommentBiz
from app.services.interaction_actions.dynamic.biz import DynamicBiz
from app.services.interaction_actions.lottery.biz import LotteryBiz
from app.services.interaction_actions.rpa_action.biz import RpaActionBiz
from app.services.interaction_actions.rpa_browser.biz import RpaBrowserBiz
from app.services.interaction_actions.rpa_plugin.biz import RpaPluginBiz
from app.services.interaction_actions.rpa_tag.biz import RpaTagBiz
from app.services.interaction_actions.rpa_workflow.biz import RpaWorkflowBiz
from app.services.interaction_actions.user.biz import UserBiz

__all__ = [
    "CommentBiz",
    "DynamicBiz",
    "LotteryBiz",
    "RpaActionBiz",
    "RpaBrowserBiz",
    "RpaPluginBiz",
    "RpaTagBiz",
    "RpaWorkflowBiz",
    "UserBiz",
]
