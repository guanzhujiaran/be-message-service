"""sync_models_to_production

Revision ID: d366ed68066f
Revises: fda501cc07b2
Create Date: 2026-08-10 10:31:56.093579

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'd366ed68066f'
down_revision: Union[str, None] = 'fda501cc07b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 真实模型差异：库里缺失、代码模型（pptr_db.py）已定义的对象 → 补建
    op.create_table('TUserExpRecord',
    sa.Column('pk', sa.BIGINT(), autoincrement=True, nullable=False),
    sa.Column('mid', sa.BIGINT(), nullable=False),
    sa.Column('createdAt', sa.DateTime(timezone=True), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
    sa.Column('action_type', sa.Integer(), nullable=False, comment='行为类型 int：1=daily_login（每日登录），与 ExpActionType 枚举对应'),
    sa.Column('exp', sa.Integer(), server_default=sa.text('0'), nullable=False, comment='本次增加的经验值'),
    sa.Column('ref_date', sqlmodel.sql.sqltypes.AutoString(length=10), nullable=False, comment='行为引用日期，格式 YYYY-MM-DD，用于每日/每周行为幂等检查'),
    sa.ForeignKeyConstraint(['mid'], ['TUserInfo.uid'], name='TUserExpRecord_mid_fkey'),
    sa.PrimaryKeyConstraint('pk', name='TUserExpRecord_pkey'),
    comment='用户经验增加记录表，记录所有行为（每日登录、发评论等）增加的经验值'
    )
    op.create_index('idx_exp_record_mid_action_ref_date', 'TUserExpRecord', ['mid', 'action_type', 'ref_date'], unique=False)
    op.create_unique_constraint('TUserDetail_uname_key', 'TUserDetail', ['uname'])

    # 2) 清理库里存在但 pptr 模型未定义的孤儿表（生产恢复时混入的其他服务/历史表）
    #    注意：这些表无 pptr 模型定义，drop 为不可逆清理操作，执行前请确认本库不被其他服务共享。
    for _t in [
        'TAccountBiliAtMsg',
        'TAccountBiliReplyMsg',
        'TAccountBiliWhisperMsg',
        'TAccountDetailInfo',
        'TAccountInfo',
        'TAccountInfo_DashBoardInfo',
        'TAccountInfo_LotteryLog',
        'TAccountInfo_ReserveLog',
        'TAtariInfo',
        'TBiliLotteryInfoRecord',
        'TBiliUser',
        'TBiliUserDetail',
        'TComment',
        'TCommentInteractRelation',
        'TCommonLog',
        'TDynamicInfo',
        'TLiveLotteryLog',
        'TLogBiliDailyTask',
        'TLotteryLogInfo',
        'TPersonalizedContent',
        'TPersonalizedContentType1',
        'TReserveLotteryInfo',
    ]:
        # 使用 CASCADE 避免表间外键依赖导致的 drop 顺序错误（孤儿表间可能互相引用）
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{_t}" CASCADE'))


def downgrade() -> None:
    # 回滚仅能删掉本次新建的对象；被 drop 的孤儿表无模型定义，无法自动重建。
    op.drop_constraint('TUserDetail_uname_key', 'TUserDetail', type_='unique')
    op.drop_index('idx_exp_record_mid_action_ref_date', table_name='TUserExpRecord')
    op.drop_table('TUserExpRecord')
