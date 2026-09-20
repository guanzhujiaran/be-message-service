"""运行时配置管理端服务（2.64.0）。

管理端是 `msg_sys_config` 的**唯一写入口**：写入前按 key 校验值结构，写入后清本实例
缓存（本实例立即生效，其余实例 ≤ TTL）。读取侧不走本服务，见
`app/services/common/runtime_config.py`。
"""

from __future__ import annotations

from loguru import logger
from pydantic import ValidationError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.db.sys_config_tbl import SysConfig
from app.models.schemas.sys_config import SysConfigItem, SysConfigUpdateReq
from app.services.common import runtime_config


class SysConfigValueError(ValueError):
    """配置项未登记或配置值非法（接口层回 400）。"""


class SysConfigService:
    """运行时配置的读写（管理端）。"""

    @staticmethod
    async def list_all(session: AsyncSession) -> list[SysConfigItem]:
        """列出全部**已登记**的配置项（含尚未写入 DB 的，按 settings 默认值返回）。

        遍历 `runtime_config.CONFIG_SPECS` 而非只查表：管理端要能看到「当前生效的默认值」
        （`isDefault=True`），否则首次进入页面什么都看不到、也无法基于默认值调整。

        表未建 / 查询失败（如迁移尚未执行）时**全部按默认值返回**，与读取器的兜底口径一致：
        配置是旁路能力，管理端也不该因为配置表故障而整页报错。
        """
        try:
            # SAVEPOINT：失败只回滚到保存点，不污染外层事务
            async with session.begin_nested():
                rows = {r.key: r for r in (await session.exec(select(SysConfig))).all()}
        except Exception as e:  # noqa: BLE001 - 配置表不可用不影响管理端列表
            logger.warning(f"[SysConfig] 读取配置表失败，全部按默认值返回: {e}")
            rows = {}
        items: list[SysConfigItem] = []
        for key, spec in runtime_config.CONFIG_SPECS.items():
            row = rows.get(key)
            if row is None:
                items.append(
                    SysConfigItem(key=key, value=spec.default(), isDefault=True)
                )
                continue
            items.append(
                SysConfigItem(
                    key=row.key,
                    value=row.value or {},
                    remark=row.remark,
                    updatedBy=int(row.updatedBy or 0),
                    updated_at=row.updated_at,
                )
            )
        return items

    @staticmethod
    async def update(
        session: AsyncSession, req: SysConfigUpdateReq, operator_mid: int
    ) -> SysConfigItem:
        """写入（整体替换）某个配置项。

        校验两步：key 必须在 `runtime_config.CONFIG_SPECS` 已登记（避免写入无人消费的
        配置），值必须通过对应值模型（SQLModel）校验；通过后取模型的**规范化 JSON**
        （补齐默认值、剔除多余字段）落库——库里存的始终是模型认可的结构。任一不通过
        即拒绝，**不落库**。

        Raises:
            SysConfigValueError: key 未登记或值非法。
        """
        try:
            # 校验 + 规范化（JSON → SQLModel → JSON），与读取侧共用同一个值模型
            normalized = runtime_config.validate_config_value(req.key, req.value)
        except KeyError:
            raise SysConfigValueError(f"不支持的配置项：{req.key}") from None
        except ValidationError as e:
            raise SysConfigValueError(f"配置值非法：{e}") from e

        row = await session.get(SysConfig, req.key)
        if row is None:
            row = SysConfig(
                key=req.key, value=normalized, remark=req.remark, updatedBy=operator_mid
            )
        else:
            row.value = normalized
            if req.remark is not None:
                row.remark = req.remark
            row.updatedBy = operator_mid
        session.add(row)
        await session.commit()
        await session.refresh(row)

        # 本实例立即生效；其余实例靠 TTL 过期刷新（见 runtime_config 模块说明）
        runtime_config.invalidate(req.key)
        return SysConfigItem(
            key=row.key,
            value=row.value or {},
            remark=row.remark,
            updatedBy=int(row.updatedBy or 0),
            updated_at=row.updated_at,
        )


__all__ = ["SysConfigService", "SysConfigValueError"]
