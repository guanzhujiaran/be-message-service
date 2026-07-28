"""用户相关模型。"""

from pydantic import BaseModel, ConfigDict


class MessageUser(BaseModel):
    """推送发起方的用户信息。

    由上游 FastapiApp / RPA-Browser 经 nodejs-pptr 代理通过 x-bili-* 请求头透传，
    message-service 解析后写入推送内容（标题前缀），便于区分「是谁触发的推送」。
    字段与 RPA-Browser / nodejs-pptr ProxyEndPort 中 setUserHeaders 注入的
    x-bili-* 头一一对应。
    """

    model_config = ConfigDict(extra="ignore")

    # 用户唯一 ID（B 站 mid）
    mid: str | None = None
    # 登录用户名
    user_name: str | None = None
    # 用户昵称（uname）
    uname: str | None = None
    # 用户等级
    level: str | None = None
    # 角色
    role: str | None = None
    # 个性签名
    sign: str | None = None
    # 性别
    sex: str | None = None
    # 邮箱
    email: str | None = None
    # 大会员状态
    vip_status: str | None = None
    # 大会员类型
    vip_type: str | None = None
