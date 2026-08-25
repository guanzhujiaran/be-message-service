[← 返回目录](./README.md)

## 附录：参考文件索引

| 文件 | 用途 |
|---|---|
| [app/models/db/base.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/db/base.py) | MySQL 主库 ORM 模型公共基类（TimestampMixin / IntEnum / StrEnum） |
| [app/models/db/comment.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/db/comment.py) | MySQL 主库现有模型规范参考（camelCase 列名、sa.JSON、枚举列类型） |
| [enums.py](file:///home/minato/BilibiliExplosion/be-message-service/app/models/enums.py) | 枚举定义规范参考 |
| [sharding.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/sharding.py) | 雪花 ID 生成器 |
| [migration.py](file:///home/minato/BilibiliExplosion/be-message-service/app/core/migration.py) | Alembic 迁移入口 |
| [dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/common/dynamic.proto) | B站Moment通用模型 |
| [/x/space/wbi/acc/info?mid=](https://api.bilibili.com/x/space/wbi/acc/info?mid=1) | B站 用户空间完整资料接口（本计划书 5.9 节 `GET /user/space/info` 对标该返回结构） |
| [/x/space/upstat?mid=](https://api.bilibili.com/x/space/upstat?mid=1) | B站 空间统计接口（`GET /moment/upstat` 对标） |
| [/x/relation/stat?vmid=](https://api.bilibili.com/x/relation/stat?vmid=1) | B站 关注/粉丝/互关统计接口（`GET /message/follow/stat` 对标） |
| [gateway.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/gw/gateway.proto) | B站Moment网关模型 |
| [api.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/dynamic/interfaces/feed/v1/api.proto) | B站Moment Feed 接口 |
| [v2/dynamic.proto](file:///home/minato/BilibiliExplosion/be-bilibili-crawler/Service/GrpcModule/Grpc/GrpcProto/bilibili/app/dynamic/v2/dynamic.proto) | B站 V2 Moment服务 |
