"""关注关系领域值对象：把「双向关系」从一个字符串状态升级为可判定的对象。

`msg_user_follow` 一行只表达一个方向，业务上真正关心的是**成对**的信息
（我关注了 TA 吗？TA 关注了我吗？谁拉黑了谁？）。改造前这些判断散落在
`FollowService` 各处各查一次、各自 `==` 比较；现在统一由 `FollowRelationState`
承载，互关 / 拉黑等派生语义只算一次。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import FollowStatusEnum
from app.models.schemas.follow import FollowRelationResp


@dataclass(frozen=True, slots=True)
class FollowRelationState:
    """访问者与目标用户的双向关系（不可变快照）。"""

    viewer_mid: int
    target_mid: int
    #: 我 → TA
    following: bool = False
    #: TA → 我
    followed_by: bool = False
    #: 我 → TA 的拉黑
    i_blocked: bool = False
    #: TA → 我的拉黑
    blocked_by: bool = False

    @classmethod
    def from_statuses(
        cls,
        viewer_mid: int,
        target_mid: int,
        forward: FollowStatusEnum | None,
        reverse: FollowStatusEnum | None,
    ) -> FollowRelationState:
        """由两个方向的原始状态构造（`forward` = 我→TA，`reverse` = TA→我）。"""
        return cls(
            viewer_mid=int(viewer_mid),
            target_mid=int(target_mid),
            following=forward == FollowStatusEnum.FOLLOWING,
            followed_by=reverse == FollowStatusEnum.FOLLOWING,
            i_blocked=forward == FollowStatusEnum.BLOCKED,
            blocked_by=reverse == FollowStatusEnum.BLOCKED,
        )

    @property
    def mutual(self) -> bool:
        """是否互相关注（双向都处于 following）。"""
        return self.following and self.followed_by

    @property
    def blocked(self) -> bool:
        """是否存在**任一方向**的拉黑（黑名单互访拒绝口径）。"""
        return self.i_blocked or self.blocked_by

    def to_resp(self) -> FollowRelationResp:
        """转成对外 DTO（API 响应模型）。"""
        return FollowRelationResp(
            mid=self.viewer_mid,
            target_mid=self.target_mid,
            following=self.following,
            followed_by=self.followed_by,
            mutual=self.mutual,
            i_blocked=self.i_blocked,
            blocked_by=self.blocked_by,
        )


__all__ = ["FollowRelationState"]
