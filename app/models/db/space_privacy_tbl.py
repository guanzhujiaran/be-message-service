"""空间对外可见性设置表。

`TUserSpacePrivacy` 决定「别人打开 TA 的空间主页时能看到什么」，
粒度参考 B 站空间「隐私设置」（按**内容模块**开关注册器对外展示
沿用既有 `TUserFavoriteSetting.showFavorites`，不重复建列）。

设计要点：
- 每用户一行（`mid` 主键），`get_or_create` 懒创建，**默认全部不展示**：
  与 `showFavorites`（默认展示）相反，隐私表采取「默认关闭、主人显式开启」的口径，
  避免新建账号即对外泄露联系方式 / IP 属地；
- `viewer == mid` 时一律不过滤（自己的空间自己看），过滤逻辑在服务层按viewer 判定；
- 不设「总开关」：已有5 个独立开关，再套一层只会让状态组合爆炸；
- 统计数字（关注数 / 粉丝数 / 获赞 / 动态 / 被访问次数）**不设开关**，固定展示，
  因此不在本表；签名 / 等级 / 头像同样不设（关掉就没有内容了）。
"""

from sqlalchemy import BIGINT
from sqlmodel import Field, SQLModel
from sqlalchemy import PrimaryKeyConstraint

from app.models.db.base_tbl import TimestampMixin


class TUserSpacePrivacy(TimestampMixin, table=True):
    """空间资料对外可见性（每用户一行，默认全关）。"""

    __tablename__ = "TUserSpacePrivacy"
    __table_args__ = (
        PrimaryKeyConstraint("mid", name="TUserSpacePrivacy_pkey"),
        {"extend_existing": True, "comment": "空间对外可见性：每用户一行，默认全关"},
    )

    # DB 层mid 用 BIGINT int（对齐 msg_user_setting.mid / TUserFavoriteSetting.mid 约定）；
    # 前端传 str 由接口层 StrInt 包装转换，不在 DB 层做字符串存储
    mid: int = Field(
        default=None,
        primary_key=True,
        sa_type=BIGINT,
        sa_column_kwargs={"autoincrement": False},
        description="用户mid",
    )

    # ---- 基本信息（敏感原文，关闭时后端直接不下发字段）----
    show_personal_info: bool = Field(
        default=False, description="个人资料：性别/生日/邮箱是否对外展示"
    )
    show_login_info: bool = Field(
        default=False, description="上次登录时间与IP属地是否对外展示"
    )

    # ---- 行为记录（TA 主页时间线条目按类型过滤）----
    show_follow_list: bool = Field(
        default=False, description="时间线里「我关注了谁」条目是否对外可见"
    )
    show_like_list: bool = Field(
        default=False, description="时间线里「我点赞了什么」条目是否对外可见"
    )
    show_fans_list: bool = Field(default=False, description="我的粉丝列表是否对外可见")


__all__ = ["TUserSpacePrivacy"]
