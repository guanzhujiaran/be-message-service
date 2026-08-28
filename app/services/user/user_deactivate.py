"""用户注销服务（P12，2.15.0 / 2.15.1）。

注销 = **物理删除** pptr Postgres 四表 + **彻底清除** be-message MySQL 该用户的全部业务数据，
账号不可恢复；**Casdoor 不管**（不同步禁用）。

**实现方式（2.15.1）**：本服务降级为**编排器**，不持有任何具体 SQL——各业务「按 uid
彻底清除」的删除逻辑拆分到独立可复用的领域服务 `app/services/cleanup/`（`cleanup_moment` /
`cleanup_comment` / `cleanup_follow` / `cleanup_report` / `cleanup_favorite` /
`cleanup_dm` / `cleanup_notify` / `cleanup_event` / `cleanup_misc` / `cleanup_pptr`，
各自 `delete_all_by_uid(session, uid)`），本服务按依赖顺序串行调用。

**调用方式**：注销接口只校验 + 投递 MQ（`publish_user_deactivate`），真实删除由
消费者异步执行。本服务的 `deactivate` 亦可直接被消费者 / 测试调用。

删除顺序关键（详见各 cleanup 服务 docstring）：
- pptr Postgres 四表 + 4 张日志表，所有 FK 无 `ondelete` 级联，须按依赖逆序删；
- be-message MySQL 动态子表对 `dynId` 均 `ondelete=CASCADE`，删 `TMoment WHERE mid=uid`
  自动级联；但「我点赞/浏览他人动态」痕迹（`TMomentLike`/`TInteractionViewLog` 的 mid）需单独删；
- 关注 / 私信 / 事件 / 封禁 / 管理 / 评论@ / 用户举报 等为双向关系，需按 `mid OR 对方字段` 删除。
"""

from loguru import logger

from app.core.database import new_pptr_session, new_session
from app.services.cleanup import (
    cleanup_comment,
    cleanup_dm,
    cleanup_event,
    cleanup_favorite,
    cleanup_follow,
    cleanup_misc,
    cleanup_moment,
    cleanup_notify,
    cleanup_pptr,
    cleanup_report,
)


class UserDeactivateService:
    """用户注销服务（编排器，静态方法集合）。

    各业务删除内聚在 `app/services/cleanup/` 的独立领域服务中，本类只负责
    按依赖顺序组合调用（先 pptr 后 be-message），不做具体 SQL。
    """

    # be-message MySQL 各领域删除服务（顺序：先清该用户的互动/关联，最后删其发布主体）
    _MSG_CLEANUP = [
        cleanup_moment.CleanupMomentService,  # 动态（含点赞/浏览痕迹）
        cleanup_comment.CleanupCommentService,  # 评论
        cleanup_follow.CleanupFollowService,  # 关注 / 拉黑
        cleanup_report.CleanupReportService,  # 举报
        cleanup_favorite.CleanupFavoriteService,  # 收藏
        cleanup_dm.CleanupDmService,  # 私信
        cleanup_notify.CleanupNotifyService,  # 通知
        cleanup_event.CleanupEventService,  # 事件
        cleanup_misc.CleanupMiscService,  # 设置 / 活跃 / 封禁 / 管理
    ]

    @staticmethod
    async def deactivate(uid: int) -> None:
        """注销用户：物理删除 pptr 四表 + 彻底清除 be-message 业务数据。

        幂等：已注销 / 无数据时各 DELETE 影响 0 行仍正常返回。

        Raises:
            ValueError: uid 非法。
        """
        if not uid or uid <= 0:
            raise ValueError("uid 不合法")
        await UserDeactivateService._delete_pptr_user(uid)
        await UserDeactivateService._delete_message_data(uid)
        logger.info(f"用户 {uid} 已注销（pptr 四表物理删除 + be-message 业务数据清除）")

    @staticmethod
    async def _delete_pptr_user(uid: int) -> None:
        """删除 pptr Postgres 四表 + 关联日志（单事务）。"""
        async with new_pptr_session() as s:
            await cleanup_pptr.CleanupPptrService.delete_all_by_uid(s, uid)
            await s.commit()

    @staticmethod
    async def _delete_message_data(uid: int) -> None:
        """彻底清除 be-message MySQL 业务数据（单事务）。"""
        async with new_session() as s:
            for svc in UserDeactivateService._MSG_CLEANUP:
                await svc.delete_all_by_uid(s, uid)
            await s.commit()


__all__ = ["UserDeactivateService"]
