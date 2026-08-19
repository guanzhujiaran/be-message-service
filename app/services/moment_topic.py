"""动态话题 & @ & LBS 服务（Phase 5）。

覆盖 P5-T1 ~ P5-T6：

- 话题广场列表（P5-T1）：列 ``TMomentTopic``，按 isHot / sortWeight / dynCount 倒序。
- 话题 Feed 流（P5-T2）：复用 ``MomentFeedService.topic_feed``（见
  ``app/services/moment_feed.py``），以 ``topicId`` 过滤、仅 normal + 未软删。
- @用户推荐列表（P5-T3）：关注 / 粉丝分组，经 ``FollowService`` 取 mid，
  再由 ``PptrUserService.get_many`` 只读回查昵称 / 头像（与评论系统一致，不冗余）。
- @用户搜索（P5-T4）：``PptrUserService.search_by_uname`` 按昵称前缀匹配。
- POI LBS 附近 / 关键词搜索（P5-T5 / P5-T6）：MVP **本地模式**，未接外部地图 API，
  基于已发 Moment 的 ``lbsPoi`` 去重聚合返回（含经纬度、使用该 POI 的 Moment 数）。
"""

from sqlmodel import col, func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db import TMoment, TMomentTopic
from app.models.enums import MomentTopicAuditStatusEnum
from app.models.schemas.moment import (
    MomentAtListResp,
    MomentAtSearchResp,
    MomentAtUserItem,
    MomentPoiItem,
    MomentPoiResp,
    MomentTopicCreateReq,
    MomentTopicCreateResp,
    MomentTopicDetailItem,
    MomentTopicDetailResp,
    MomentTopicInfo,
    MomentTopicMineItem,
    MomentTopicMineResp,
    MomentTopicSquareResp,
)
from app.core.sharding import generate_topic_id
from app.services.follow import FollowService
from app.services.pptr_user import PptrUserService

# POI 面板单页上限
_POI_PAGE_SIZE = 20


def _brief_to_at_item(b, *, remark: str | None = None) -> MomentAtUserItem:
    """把 pptr 用户简档（CommentUserBrief）映射到 @ 用户项。"""
    return MomentAtUserItem(
        mid=b.mid,
        uname=b.uname,
        face=b.avatar,
        remark=remark,
    )


async def _query_poi(
    session: AsyncSession,
    *,
    keyword: str | None,
    page: int,
    page_size: int,
) -> MomentPoiResp:
    """基于已发 Moment 的 lbsPoi 去重聚合 POI（本地模式，无外部地图 API）。"""
    cnt_col = func.count(TMoment.dynId)
    stmt = (
        select(
            TMoment.lbsPoi,
            cnt_col.label("cnt"),
            func.max(TMoment.lbsLat).label("lat"),
            func.max(TMoment.lbsLng).label("lng"),
        )
        .where(col(TMoment.lbsPoi).isnot(None))
        .where(col(TMoment.deletedAt).is_(None))
    )
    if keyword:
        # 转义 LIKE 通配符，避免用户输入 % / _ 造成异常匹配
        escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(col(TMoment.lbsPoi).like(f"%{escaped}%"))
    stmt = (
        stmt.group_by(col(TMoment.lbsPoi))
        .order_by(cnt_col.desc(), col(TMoment.lbsPoi))
        .limit(page_size + 1)
        .offset((page - 1) * page_size)
    )
    rows = (await session.exec(stmt)).all()
    has_more = len(rows) > page_size
    items = [
        MomentPoiItem(
            poi=r[0],
            lat=r[2],
            lng=r[3],
            dynCount=int(r[1] or 0),
        )
        for r in rows[:page_size]
    ]
    return MomentPoiResp(items=items, hasMore=has_more)


class MomentTopicService:
    """动态话题 / @ / POI LBS 服务（静态方法集合，无状态）。"""

    # ==================== 话题广场（P5-T1）====================

    @staticmethod
    async def topic_square(
        session: AsyncSession,
        *,
        page: int = 1,
        page_size: int = 20,
        hot_only: bool = False,
    ) -> MomentTopicSquareResp:
        """话题广场列表（按 isHot / sortWeight / dynCount 倒序）。

        ``hot_only=True`` 时仅返回热门话题（isHot=1），供 /topic/hot-search 复用。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)

        # 2.19.0：广场/热搜仅展示审核通过的话题
        stmt = select(TMomentTopic).where(
            col(TMomentTopic.auditStatus) == MomentTopicAuditStatusEnum.NORMAL
        )
        if hot_only:
            stmt = stmt.where(col(TMomentTopic.isHot) == 1)
        stmt = stmt.order_by(
            col(TMomentTopic.isHot).desc(),
            col(TMomentTopic.sortWeight).desc(),
            col(TMomentTopic.dynCount).desc(),
            col(TMomentTopic.topicId).desc(),
        ).limit(page_size + 1).offset((page - 1) * page_size)

        rows = (await session.exec(stmt)).all()
        has_more = len(rows) > page_size
        items = [
            MomentTopicInfo(
                topicId=t.topicId,
                topicName=t.topicName,
                topicCover=t.topicCover,
                topicDesc=t.topicDesc,
                jumpUrl=t.jumpUrl,
                dynCount=t.dynCount,
                viewCount=t.viewCount,
                isHot=t.isHot,
            )
            for t in rows[:page_size]
        ]
        return MomentTopicSquareResp(items=items, hasMore=has_more)

    # ==================== 话题详情（对齐 B 站 top_details）====================

    @staticmethod
    async def topic_detail(
        session: AsyncSession,
        topic_id: int,
        viewer_mid: int | None = None,
    ) -> MomentTopicDetailResp | None:
        """话题详情（对齐 B 站 `top_details` 结构）。

        - ``topic_item``：id/name/view/discuss/fav/dynamics/like/share/jump_url/
          back_color/share_pic/description/ctime；
        - ``topic_creator``：话题创建者简要（uid/face/name）——TMomentTopic
          目前无创建者字段，返回空（后续扩展）；
        - ``has_create_jurisdiction``：暂无创建/管理判定，默认 False。
        """
        topic = (
            await session.exec(
                select(TMomentTopic).where(col(TMomentTopic.topicId) == topic_id)
            )
        ).one_or_none()
        if topic is None:
            return None
        # 2.19.0：话题详情仅展示审核通过的话题（auditing/rejected 对所有人不可见）
        if topic.auditStatus is not MomentTopicAuditStatusEnum.NORMAL:
            return None

        topic_item = {
            "id": topic.topicId,
            "name": topic.topicName,
            "view": topic.viewCount or 0,
            "discuss": 0,  # TMomentTopic 暂无讨论数，后续扩展
            "fav": 0,
            "dynamics": topic.dynCount or 0,
            "like": 0,
            "share": 0,
            "jump_url": topic.jumpUrl,
            "back_color": None,
            "description": topic.topicDesc,
            "share_pic": topic.topicCover,
            "ctime": int(topic.created_at.timestamp()) if topic.created_at else 0,
        }
        return MomentTopicDetailResp(
            top_details=MomentTopicDetailItem(
                topic_item=topic_item,
                topic_creator={},
                has_create_jurisdiction=False,
                close_pub_layer_entry=False,
            )
        )

    # ==================== 创建话题（2.19.0）====================

    @staticmethod
    async def create_topic(
        session: AsyncSession,
        *,
        mid: int,
        req: MomentTopicCreateReq,
    ) -> MomentTopicCreateResp:
        """创建话题：名称唯一校验 + 合法性校验；创建即 auditStatus=auditing，不公开展示。

        - 名称 1-30 字，trim 后非空；
        - 名称全局唯一（UniqueConstraint），重复抛 ValueError（接口转 422）；
        - 封面仅允许 http/https URL（选填）；
        - 描述 ≤200 字（选填）。
        """
        topic_name = (req.topicName or "").strip()
        if not topic_name:
            raise ValueError("话题名称不能为空")
        if len(topic_name) > 30:
            raise ValueError("话题名称最长 30 字")
        if req.topicCover and not req.topicCover.startswith(("http://", "https://")):
            raise ValueError("话题封面仅支持 http/https 链接")
        if req.topicDesc and len(req.topicDesc) > 200:
            raise ValueError("话题描述最长 200 字")

        existing = (
            await session.exec(
                select(TMomentTopic).where(
                    col(TMomentTopic.topicName) == topic_name
                )
            )
        ).one_or_none()
        if existing is not None:
            raise ValueError("话题已存在")

        topic = TMomentTopic(
            topicId=generate_topic_id(),
            topicName=topic_name,
            topicCover=req.topicCover,
            topicDesc=req.topicDesc,
            creatorMid=mid,
            auditStatus=MomentTopicAuditStatusEnum.AUDITING,
            pubTime=None,
        )
        session.add(topic)
        await session.commit()
        await session.refresh(topic)
        return MomentTopicCreateResp(
            topicId=topic.topicId,
            topicName=topic.topicName,
            auditStatus=topic.auditStatus.value,
        )

    # ==================== 我创建的话题（2.19.0）====================

    @staticmethod
    async def mine(
        session: AsyncSession,
        *,
        mid: int,
        page_num: int = 1,
        page_size: int = 20,
    ) -> MomentTopicMineResp:
        """我创建的话题（含全部审核状态，按创建时间倒序分页）。"""
        page_num = max(1, page_num)
        page_size = min(max(1, page_size), 50)
        rows = (
            await session.exec(
                select(TMomentTopic)
                .where(col(TMomentTopic.creatorMid) == mid)
                .order_by(col(TMomentTopic.created_at).desc())
                .limit(page_size + 1)
                .offset((page_num - 1) * page_size)
            )
        ).all()
        has_more = len(rows) > page_size
        items = [
            MomentTopicMineItem(
                topicId=t.topicId,
                topicName=t.topicName,
                topicCover=t.topicCover,
                topicDesc=t.topicDesc,
                auditStatus=t.auditStatus.value,
                auditRejectReason=t.auditRejectReason,
                pubTime=t.pubTime.isoformat() if t.pubTime else None,
                createdAt=t.created_at.isoformat() if t.created_at else None,
            )
            for t in rows[:page_size]
        ]
        return MomentTopicMineResp(items=items, hasMore=has_more)

    # ==================== @用户推荐（P5-T3）====================

    @staticmethod
    async def at_recommend(
        session: AsyncSession,
        mid: int,
        *,
        page_size: int = 20,
    ) -> MomentAtListResp:
        """@用户推荐列表：关注 / 粉丝分组。

        关注 / 粉丝的 mid 取自 msg_user_follow（be-message 主库），
        昵称 / 头像经 PptrUserService.get_many 一次性回查（避免 N+1）。
        """
        page_size = min(max(1, page_size), 50)
        following_resp = await FollowService.list_following(
            session, mid, page_num=1, page_size=page_size
        )
        followers_resp = await FollowService.list_followers(
            session, mid, page_num=1, page_size=page_size
        )
        following_mids = [it.mid for it in following_resp.items]
        follower_mids = [it.mid for it in followers_resp.items]
        briefs = await PptrUserService.get_many(following_mids + follower_mids)

        following = [
            _brief_to_at_item(
                briefs[it.mid], remark="互相关注" if it.mutual else None
            )
            if it.mid in briefs
            else MomentAtUserItem(mid=it.mid, remark="互相关注" if it.mutual else None)
            for it in following_resp.items
        ]
        followers = [
            _brief_to_at_item(
                briefs[it.mid], remark="互相关注" if it.mutual else None
            )
            if it.mid in briefs
            else MomentAtUserItem(mid=it.mid, remark="互相关注" if it.mutual else None)
            for it in followers_resp.items
        ]
        return MomentAtListResp(following=following, followers=followers)

    # ==================== @用户搜索（P5-T4）====================

    @staticmethod
    async def at_search(
        keyword: str,
        *,
        page_size: int = 20,
    ) -> MomentAtSearchResp:
        """@用户搜索：按昵称 / 注册名前缀匹配（走 pptr 真实搜索）。"""
        page_size = min(max(1, page_size), 50)
        briefs = await PptrUserService.search_by_uname(keyword, limit=page_size)
        items = [_brief_to_at_item(b) for b in briefs]
        return MomentAtSearchResp(items=items, hasMore=len(briefs) >= page_size)

    # ==================== POI LBS（P5-T5 / P5-T6）====================

    @staticmethod
    async def poi_nearby(
        session: AsyncSession,
        *,
        lat: float | None = None,
        lng: float | None = None,
        page: int = 1,
        page_size: int = _POI_PAGE_SIZE,
    ) -> MomentPoiResp:
        """附近地点（MVP 本地模式）：返回已发 Moment 的 lbsPoi 去重列表。

        未接外部地图 API，``lat`` / ``lng`` 暂仅透传，不作为硬过滤，
        后续接入地图服务时可按距离排序 / 范围圈选。
        """
        page = max(1, page)
        page_size = min(max(1, page_size), 50)
        return await _query_poi(session, keyword=None, page=page, page_size=page_size)

    @staticmethod
    async def poi_search(
        session: AsyncSession,
        keyword: str,
        *,
        lat: float | None = None,
        lng: float | None = None,
        page: int = 1,
        page_size: int = _POI_PAGE_SIZE,
    ) -> MomentPoiResp:
        """POI 关键词搜索（MVP 本地模式）：按 lbsPoi 模糊匹配去重聚合。"""
        page = max(1, page)
        page_size = min(max(1, page_size), 50)
        return await _query_poi(
            session, keyword=keyword or "", page=page, page_size=page_size
        )


__all__ = ["MomentTopicService"]
