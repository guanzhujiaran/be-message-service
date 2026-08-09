"""drop non-pptr user tables (account / comment / log / personalized content)

These tables are not core to pptr (be-gateway) and are being removed as pptr
transitions to a pure gateway. All user data is now managed through the core
user tables (TUserInfo / TUserDetail / TUserLevel / TUserVip) and the
TUserExpRecord / TUserPwdRecord audit tables.

Dropped tables:
- TAccountInfo, TAccountDetailInfo
- TAccountBiliAtMsg, TAccountBiliReplyMsg, TAccountBiliWhisperMsg
- TPersonalizedContent, TPersonalizedContentType1
- TUserNameRecord
- TComment, TCommentInteractRelation
- TAccountInfo_DashBoardInfo, TAccountInfo_LotteryLog, TAccountInfo_ReserveLog
- TAtariInfo, TCommonLog, TLiveLotteryLog, TLogBiliDailyTask

Revision ID: 8a7b6c5d4e3f
Revises: e8f9a0b1c2d3
Create Date: 2026-08-09 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '8a7b6c5d4e3f'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop dependent tables first, then parent tables.
    # 使用原生 SQL `DROP TABLE IF EXISTS ... CASCADE`：
    # 1) op.drop_table 不支持 cascade 关键字（会触发 TypeError）；
    # 2) IF EXISTS 容忍部分表已被手动删除的场景，避免迁移失败卡住版本号。
    drop_tables = [
        'TCommentInteractRelation',
        'TComment',
        'TPersonalizedContentType1',
        'TPersonalizedContent',
        'TAccountBiliWhisperMsg',
        'TAccountBiliReplyMsg',
        'TAccountBiliAtMsg',
        'TAccountDetailInfo',
        'TAccountInfo_DashBoardInfo',
        'TAccountInfo_LotteryLog',
        'TAccountInfo_ReserveLog',
        'TAtariInfo',
        'TCommonLog',
        'TLiveLotteryLog',
        'TLogBiliDailyTask',
        'TUserNameRecord',
        'TAccountInfo',
    ]
    for tbl in drop_tables:
        op.execute(f'DROP TABLE IF EXISTS "{tbl}" CASCADE')


def downgrade() -> None:
    """Downgrade not supported — these tables are not recoverable from Alembic.

    The tables were originally created by the pptr (puppeteer_Bili) Node.js
    Sequelize migrations, not by be-message Alembic. If restoration is needed,
    refer to the original Sequelize migration files under:
      puppeteer_Bili/ExpressServerEnd/migrations/
    """
    pass