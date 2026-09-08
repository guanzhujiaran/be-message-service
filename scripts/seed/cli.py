"""统一 CLI：一个命令跑完全互动联调（阶段一）+ 大数据灌数（阶段二）。

参数与拆分前完全一致；新增场景时在对应功能包内加模块，再在此处加 ``--skip-*`` 开关挂进编排。
"""
import argparse
import asyncio
import sys

from loguru import logger

from .bulk.runner import run_bulk
from .config import _DYN_POOL_SIZE, _LOTTERY_POOL_SIZE
from .full import seed

# ---------------------------------------------------------------------------
# 阶段编排：全互动联调（阶段一）+ 大数据灌数（阶段二）
# ---------------------------------------------------------------------------


def _run_full_args(args: argparse.Namespace) -> dict:
    return dict(
        base_url=args.base_url,
        admin_mid=args.admin_mid,
        count=args.full_count,
        users_n=args.full_users,
        moment_concurrency=args.full_concurrency,
        skip_moment=False,
        skip_comment=False,
        skip_interact=False,
        skip_message=False,
        skip_follow=args.skip_follow,
        crawler_base_url=args.crawler_base_url,
    )


def _bulk_namespace(args: argparse.Namespace) -> argparse.Namespace:
    """把统一 CLI 的参数映射成 run_bulk 所需的 namespace。"""
    ns = argparse.Namespace()
    ns.count = args.full_count
    ns.base_url = args.base_url
    ns.admin_mid = args.admin_mid
    # 大数据灌数统一复用 --full-count（已删除独立的 --bulk-count）与 --full-concurrency（已删除独立的 --bulk-concurrency）
    ns.concurrency = args.full_concurrency
    ns.users_pool_size = args.bulk_users_pool
    ns.dry_run = args.dry_run
    ns.crawler_base_url = args.crawler_base_url
    return ns


async def _run_full(args: argparse.Namespace) -> None:
    """阶段一：全互动联调（覆盖 4 大类 18 项，断言失败响亮报错）。"""
    logger.info("========== 阶段一：全互动联调 ==========")
    if args.dry_run:
        # --dry-run 必须在**任何接口调用之前**短路：本阶段写的是 MySQL 主库真实
        # 业务数据（动态 / 评论 / 私信 / 关注关系），一旦跑起来只能靠 Ctrl-C 中断，
        # 已落库的数据无法自动回滚。
        logger.info(
            f"[dry-run] 阶段一将执行：发布并审核动态 {args.full_count} 条"
            f"（并发 {args.full_concurrency}），用户池 {args.full_users} 人；"
            f"评论体系在「动态 {_DYN_POOL_SIZE} + lottery {_LOTTERY_POOL_SIZE}」"
            f"混合资源池上跑（一级评论/楼中楼/赞踩/@/举报/置顶）；"
            f"随后关注+拉黑（每人归一保留 1 条黑名单）、私信双向全流程、"
            f"通用计数 / 举报 / 封禁 / 头像审核。本阶段不调用接口。"
        )
        return
    await seed(**_run_full_args(args))


async def _run_bulk(args: argparse.Namespace) -> None:
    """阶段二：基于真实动态的大数据灌数（走 API，软降级）。"""
    logger.info("========== 阶段二：大数据灌数 ==========")
    try:
        await run_bulk(_bulk_namespace(args))
    except KeyboardInterrupt:
        raise
    except Exception as e:  # noqa: BLE001
        # 大数据灌数失败（biliopusdb/连接问题）软降级，不阻断整体
        logger.error(f"大数据灌数阶段失败（已跳过）: {e}")


async def run_all(args: argparse.Namespace) -> None:
    # seed 默认安静：只输出「真正未知/失败」的 ERROR 级日志。
    # info/success（过程提示）与 warning（预期软降级 / 业务拒绝，如拉黑、资源不存在、
    # 评论被拒、转发失败等）一律不打印；进度由 tqdm 展示。真正异常走 logger.error / 崩溃报错。
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    if not args.skip_full:
        await _run_full(args)
    if not args.skip_bulk:
        await _run_bulk(args)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=(
            "统一 seed 入口：一个命令跑完全互动联调 + 大数据灌数"
            "（实现按功能分包在 scripts/seed/）"
        )
    )
    p.add_argument(
        "--base-url", default="http://127.0.0.1:18739", help="be-message 服务地址"
    )
    p.add_argument(
        "--admin-mid", type=int, default=11, help="role=root 管理员 mid（全场景共用）"
    )
    # 全互动阶段参数
    p.add_argument(
        "--full-count",
        type=int,
        default=1000,
        help="全互动动态条数与大数据灌数条数统一（默认 1000）",
    )
    p.add_argument(
        "--full-users", type=int, default=12, help="全互动阶段取真实用户数（默认 12）"
    )
    p.add_argument(
        "--full-concurrency",
        type=int,
        default=50,
        help="全互动动态创建与大数据灌数统一并发数（默认 50，asyncio.Semaphore 控制）",
    )
    p.add_argument(
        "--skip-follow", action="store_true", help="全互动阶段跳过关注流验证"
    )
    # 大数据灌数阶段参数
    p.add_argument(
        "--bulk-users-pool",
        type=int,
        default=20000,
        help="大数据灌数用户池大小（默认 20000）",
    )
    # 阶段开关
    p.add_argument(
        "--skip-full", action="store_true", help="跳过全互动阶段（只跑大数据灌数）"
    )
    p.add_argument(
        "--skip-bulk", action="store_true", help="跳过大数据灌数阶段（只跑全互动）"
    )
    p.add_argument(
        "--crawler-base-url",
        default="http://be-bilibili-crawler:23333",
        help="crawler HTTP 服务地址（用于 GetAllLottery 接口取真实 lottery_id）",
    )
    p.add_argument("--dry-run", action="store_true", help="只打印计划，不调用接口")
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run_all(args))
    except KeyboardInterrupt:
        logger.warning("用户中断")
        sys.exit(1)
