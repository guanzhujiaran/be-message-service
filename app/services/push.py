"""统一推送服务。

从 RPA-Browser 的 PushMessageService 移植全部渠道实现，作为 message-service 的
唯一推送执行体。所有渠道通过共享的 httpx.AsyncClient 发送，SMTP 为同步实现，
通过线程池执行。

发送语义（关键）：
- 每个渠道在「真正发送失败」时**抛异常**（网络错误、接口返回错误码等）。
- `send()` 按 FALLBACK_ORDER 顺序依次尝试已启用的渠道：
  只要有一个渠道成功即停止；全部失败则抛出 RuntimeError，由消费者判定为彻底失败。
"""

import asyncio
import base64
import hashlib
import inspect
import time
import traceback
import urllib.parse
import smtplib
import json
import re
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr
import httpx
from loguru import logger

from app.core.config import settings
from app.models import PushChannelConfig

_client: httpx.AsyncClient | None = None


def get_client() -> httpx.AsyncClient:
    """获取（懒创建）共享的 httpx 异步客户端。"""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=15.0)
    return _client


# pushme 类型 -> pushplus 模板 的映射
_PUSHME_TO_PUSHPLUS_TEMPLATE = {
    "text": "txt",
    "data": "json",
    "markdata": "markdown",
    "html": "html",
    "txt": "txt",
    "json": "json",
    "markdown": "markdown",
    "cloudMonitor": "cloudMonitor",
    "jenkins": "jenkins",
    "route": "route",
    "pay": "pay",
}

# 渠道降级顺序：优先使用用户明确要求的 PushMe / PushPlus / 邮箱，再依次尝试其余渠道
FALLBACK_ORDER = [
    "pushme",
    "pushplus_bot",
    "smtp",
    "telegram_bot",
    "bark",
    "dingding_bot",
    "feishu_bot",
    "serverJ",
    "pushdeer",
    "qmsg_bot",
    "wecom_bot",
    "wecom_app",
    "gotify",
    "ntfy",
    "wxpusher_bot",
    "iGot",
    "go_cqhttp",
    "chronocat",
    "aibotk",
    "chat",
    "weplus_bot",
    "webhook",
]


class WeCom:
    def __init__(self, conf: PushChannelConfig):
        self.conf = conf
        self.CORPID = None
        self.CORPSECRET = None
        self.AGENTID = None
        self.ORIGIN = "https://qyapi.weixin.qq.com"
        if conf.qywx_origin:
            self.ORIGIN = conf.qywx_origin

    async def get_access_token(self):
        if self.conf.qywx_am:
            parts = re.split(",", self.conf.qywx_am)
            if len(parts) >= 2:
                self.CORPID = parts[0]
                self.CORPSECRET = parts[1]

        url = f"{self.ORIGIN}/cgi-bin/gettoken"
        values = {"corpid": self.CORPID, "corpsecret": self.CORPSECRET}
        req = await get_client().post(url, params=values)
        data = json.loads(req.text)
        return data["access_token"]

    async def send_text(self, message, touser="@all"):
        parts = re.split(",", self.conf.qywx_am)
        if len(parts) >= 4:
            touser = parts[2]
            self.AGENTID = parts[3]

        send_url = (
            f"{self.ORIGIN}/cgi-bin/message/send"
            f"?access_token={await self.get_access_token()}"
        )
        send_values = {
            "touser": touser,
            "msgtype": "text",
            "agentid": self.AGENTID,
            "text": {"content": message},
            "safe": "0",
        }
        respone = await get_client().post(send_url, data=json.dumps(send_values).encode("utf-8"))
        return respone.json()["errmsg"]

    async def send_mpnews(self, title, message, media_id, touser="@all"):
        parts = re.split(",", self.conf.qywx_am)
        if len(parts) >= 4:
            touser = parts[2]
            self.AGENTID = parts[3]

        send_url = (
            f"{self.ORIGIN}/cgi-bin/message/send"
            f"?access_token={await self.get_access_token()}"
        )
        send_values = {
            "touser": touser,
            "msgtype": "mpnews",
            "agentid": self.AGENTID,
            "mpnews": {
                "articles": [
                    {
                        "title": title,
                        "thumb_media_id": media_id,
                        "author": "Author",
                        "content_source_url": "",
                        "content": message.replace("\n", "<br/>"),
                        "digest": message,
                    }
                ]
            },
        }
        respone = await get_client().post(send_url, data=json.dumps(send_values).encode("utf-8"))
        return respone.json()["errmsg"]


class PushMessageService:
    """统一推送消息服务类，整合所有推送渠道到一个类中。"""

    def __init__(self, conf: PushChannelConfig, push_type: str | None = None):
        self.conf = conf
        self.push_type = push_type

    # ---------- 各渠道实现（失败即抛异常，供 send() 做降级） ----------

    async def bark(self, title: str, content: str) -> None:
        if not self.conf.bark_push:
            return
        logger.info("bark 服务启动")
        url = self.conf.bark_push if self.conf.bark_push.startswith("http") \
            else f"https://api.day.app/{self.conf.bark_push}"
        bark_params = {
            "BARK_ARCHIVE": "isArchive", "BARK_GROUP": "group", "BARK_SOUND": "sound",
            "BARK_ICON": "icon", "BARK_LEVEL": "level", "BARK_URL": "url",
        }
        data = {"title": title, "body": content}
        config_dict = {
            "BARK_ARCHIVE": self.conf.bark_archive, "BARK_GROUP": self.conf.bark_group,
            "BARK_SOUND": self.conf.bark_sound, "BARK_ICON": self.conf.bark_icon,
            "BARK_LEVEL": self.conf.bark_level, "BARK_URL": self.conf.bark_url,
        }
        for pair in filter(
            lambda p: p[0].startswith("BARK_") and p[0] != "BARK_PUSH" and p[1] and bark_params.get(p[0]),
            config_dict.items(),
        ):
            data[bark_params.get(pair[0])] = pair[1]
        headers = {"Content-Type": "application/json;charset=utf-8"}
        resp = await get_client().post(url=url, data=json.dumps(data), headers=headers, timeout=15)
        resp_data = resp.json()
        if resp_data["code"] == 200:
            logger.info("bark 推送成功！")
        else:
            raise RuntimeError(f"bark 推送失败：url={url} body={json.dumps(data, ensure_ascii=False)} resp={resp_data}")

    async def dingding_bot(self, title: str, content: str) -> None:
        if not self.conf.dd_bot_secret or not self.conf.dd_bot_token:
            return
        logger.info("钉钉机器人 服务启动")
        timestamp = str(round(time.time() * 1000))
        secret_enc = self.conf.dd_bot_secret.encode("utf-8")
        string_to_sign = f"{timestamp}\n{self.conf.dd_bot_secret}"
        hmac_code = hmac.new(secret_enc, string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
        url = (f"https://oapi.dingtalk.com/robot/send?access_token={self.conf.dd_bot_token}"
               f"&timestamp={timestamp}&sign={sign}")
        headers = {"Content-Type": "application/json;charset=utf-8"}
        data = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
        resp = await get_client().post(url=url, data=json.dumps(data), headers=headers, timeout=15)
        resp_data = resp.json()
        if not resp_data["errcode"]:
            logger.info("钉钉机器人 推送成功！")
        else:
            raise RuntimeError(f"钉钉机器人 推送失败：{resp_data}")

    async def feishu_bot(self, title: str, content: str) -> None:
        if not self.conf.fskey:
            return
        logger.info("飞书 服务启动")
        url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{self.conf.fskey}"
        data = {"msg_type": "text", "content": {"text": f"{title}\n\n{content}"}}
        resp = await get_client().post(url, data=json.dumps(data))
        resp_data = resp.json()
        if resp_data.get("StatusCode") == 0 or resp_data.get("code") == 0:
            logger.info("飞书 推送成功！")
        else:
            raise RuntimeError(f"飞书 推送失败：url={url} body={json.dumps(data, ensure_ascii=False)} resp={resp_data}")

    async def go_cqhttp(self, title: str, content: str) -> None:
        if not self.conf.gobot_url or not self.conf.gobot_qq:
            return
        logger.info("go-cqhttp 服务启动")
        url = (f"{self.conf.gobot_url}?access_token={self.conf.gobot_token}"
               f"&{self.conf.gobot_qq}&message=标题:{title}\n内容:{content}")
        resp = await get_client().get(url)
        resp_data = resp.json()
        if resp_data["status"] == "ok":
            logger.info("go-cqhttp 推送成功！")
        else:
            raise RuntimeError(f"go-cqhttp 推送失败：{resp_data}")

    async def gotify(self, title: str, content: str) -> None:
        if not self.conf.gotify_url or not self.conf.gotify_token:
            return
        logger.info("gotify 服务启动")
        url = f"{self.conf.gotify_url}/message?token={self.conf.gotify_token}"
        data = {"title": title, "message": content, "priority": self.conf.gotify_priority}
        resp = await get_client().post(url, data=data)
        resp_data = resp.json()
        if resp_data.get("id"):
            logger.info("gotify 推送成功！")
        else:
            raise RuntimeError(f"gotify 推送失败：url={url} body={data} resp={resp_data}")

    async def iGot(self, title: str, content: str) -> None:
        if not self.conf.igot_push_key:
            return
        logger.info("iGot 服务启动")
        url = f"https://push.hellyw.com/{self.conf.igot_push_key}"
        data = {"title": title, "content": content}
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = await get_client().post(url, data=data, headers=headers)
        resp_data = resp.json()
        if resp_data["ret"] == 0:
            logger.info("iGot 推送成功！")
        else:
            raise RuntimeError(f'iGot 推送失败：{resp_data}')

    async def serverJ(self, title: str, content: str) -> None:
        if not self.conf.push_key:
            return
        logger.info("serverJ 服务启动")
        data = {"text": title, "desp": content.replace("\n", "\n\n")}
        match = re.match(r"sctp(\d+)t", self.conf.push_key)
        url = (f"https://{match.group(1)}.push.ft07.com/send/{self.conf.push_key}.send"
               if match else f"https://sctapi.ftqq.com/{self.conf.push_key}.send")
        resp = await get_client().post(url, data=data)
        resp_data = resp.json()
        if resp_data.get("errno") == 0 or resp_data.get("code") == 0:
            logger.info("serverJ 推送成功！")
        else:
            raise RuntimeError(f'serverJ 推送失败：url={url} body={data} resp={resp_data}')

    async def pushdeer(self, title: str, content: str) -> None:
        if not self.conf.deer_key:
            return
        logger.info("PushDeer 服务启动")
        data = {"text": title, "desp": content, "type": "markdown", "pushkey": self.conf.deer_key}
        url = self.conf.deer_url or "https://api2.pushdeer.com/message/push"
        resp = await get_client().post(url, data=data)
        resp_data = resp.json()
        if len(resp_data.get("content", {}).get("result", [])) > 0:
            logger.info("PushDeer 推送成功！")
        else:
            raise RuntimeError(f"PushDeer 推送失败：url={url} body={data} resp={resp_data}")

    async def chat(self, title: str, content: str) -> None:
        if not self.conf.chat_url or not self.conf.chat_token:
            return
        logger.info("chat 服务启动")
        data = "payload=" + json.dumps({"text": title + "\n" + content})
        url = self.conf.chat_url + self.conf.chat_token
        resp = await get_client().post(url, data=data)
        if resp.status_code == 200:
            logger.info("Chat 推送成功！")
        else:
            raise RuntimeError(f"Chat 推送失败：url={url} body={data} resp={resp}")

    async def pushplus_bot(self, title: str, content: str) -> None:
        if not self.conf.push_plus_token:
            return
        logger.info("PUSHPLUS 服务启动")
        template = self.conf.push_plus_template or "html"
        if self.push_type:
            mapped = _PUSHME_TO_PUSHPLUS_TEMPLATE.get(self.push_type)
            if mapped:
                template = mapped
        url = self.conf.push_plus_url or settings.pushplus_url or "http://www.pushplus.plus/send"
        data = {
            "token": self.conf.push_plus_token,
            "title": title,
            "content": content,
            "topic": self.conf.push_plus_user,
            "template": template,
            "channel": self.conf.push_plus_channel,
            "webhook": self.conf.push_plus_webhook,
            "callbackUrl": self.conf.push_plus_callbackurl,
            "to": self.conf.push_plus_to,
        }
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        resp = await get_client().post(url=url, data=body, headers=headers)
        resp_data = resp.json()
        code = resp_data["code"]
        if code == 200:
            logger.info("PUSHPLUS 推送请求成功，可根据流水号查询推送结果:" + str(resp_data.get("data", "")))
            return
        if code in (900, 903, 905, 999):
            raise RuntimeError(f"PUSHPLUS 推送失败：url={url} body={body.decode('utf-8')} resp={resp_data['msg']}")
        # 回落到 hxtrip 节点
        url_old = "http://pushplus.hxtrip.com/send"
        headers["Accept"] = "application/json"
        resp = await get_client().post(url=url_old, data=body, headers=headers)
        resp_data = resp.json()
        if resp_data["code"] == 200:
            logger.info("PUSHPLUS(hxtrip) 推送成功！")
        else:
            raise RuntimeError(f"PUSHPLUS(hxtrip) 推送失败：url={url_old} body={body.decode('utf-8')} resp={resp_data}")

    async def weplus_bot(self, title: str, content: str) -> None:
        if not self.conf.we_plus_bot_token:
            return
        logger.info("微加机器人 服务启动")
        template = "txt"
        if len(content) > 800:
            template = "html"
        url = "https://www.weplusbot.com/send"
        data = {
            "token": self.conf.we_plus_bot_token,
            "title": title,
            "content": content,
            "template": template,
            "receiver": self.conf.we_plus_bot_receiver,
            "version": self.conf.we_plus_bot_version,
        }
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        resp = await get_client().post(url=url, data=body, headers=headers)
        resp_data = resp.json()
        if resp_data["code"] == 200:
            logger.info("微加机器人 推送成功！")
        else:
            raise RuntimeError(f"微加机器人 推送失败：url={url} body={body.decode('utf-8')} resp={resp_data}")

    async def qmsg_bot(self, title: str, content: str) -> None:
        if not self.conf.qmsg_key or not self.conf.qmsg_type:
            return
        logger.info("qmsg 服务启动")
        url = f"https://qmsg.zendee.cn/{self.conf.qmsg_type}/{self.conf.qmsg_key}"
        payload = {"msg": f'{title}\n\n{content.replace("----", "-")}'.encode("utf-8")}
        resp = await get_client().post(url=url, params=payload)
        resp_data = resp.json()
        if resp_data["code"] == 0:
            logger.info("qmsg 推送成功！")
        else:
            raise RuntimeError(f'qmsg 推送失败：url={url} body={payload} resp={resp_data["reason"]}')

    async def wecom_app(self, title: str, content: str) -> None:
        if not self.conf.qywx_am:
            return
        parts = re.split(",", self.conf.qywx_am)
        if 4 < len(parts) > 5:
            raise RuntimeError("QYWX_AM 设置错误!!")
        logger.info("企业微信 APP 服务启动")
        try:
            media_id = parts[4]
        except IndexError:
            media_id = ""
        wx = WeCom(self.conf)
        if not media_id:
            message = title + "\n\n" + content
            response = await wx.send_text(message)
        else:
            response = await wx.send_mpnews(title, content, media_id)
        if response == "ok":
            logger.info("企业微信推送成功！")
        else:
            raise RuntimeError(f"企业微信推送失败：{response}")

    async def wecom_bot(self, title: str, content: str) -> None:
        if not self.conf.qywx_key:
            return
        logger.info("企业微信机器人服务启动")
        origin = self.conf.qywx_origin or "https://qyapi.weixin.qq.com"
        url = f"{origin}/cgi-bin/webhook/send?key={self.conf.qywx_key}"
        headers = {"Content-Type": "application/json;charset=utf-8"}
        data = {"msgtype": "text", "text": {"content": f"{title}\n\n{content}"}}
        resp = await get_client().post(url=url, data=json.dumps(data), headers=headers, timeout=15)
        resp_data = resp.json()
        if resp_data["errcode"] == 0:
            logger.info("企业微信机器人推送成功！")
        else:
            raise RuntimeError(f"企业微信机器人推送失败：url={url} body={json.dumps(data, ensure_ascii=False)} resp={resp_data}")

    async def telegram_bot(self, title: str, content: str) -> None:
        if not self.conf.tg_bot_token or not self.conf.tg_user_id:
            return
        logger.info("tg 服务启动")
        host = self.conf.tg_api_host or "https://api.telegram.org"
        url = f"{host}/bot{self.conf.tg_bot_token}/sendMessage"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        payload = {
            "chat_id": str(self.conf.tg_user_id),
            "text": f"{title}\n\n{content}",
            "disable_web_page_preview": "true",
        }
        proxies = None
        if self.conf.tg_proxy_host and self.conf.tg_proxy_port:
            host_part = self.conf.tg_proxy_host
            if self.conf.tg_proxy_auth is not None and "@" not in host_part:
                host_part = self.conf.tg_proxy_auth + "@" + host_part
            proxies = {"http": f"http://{host_part}:{self.conf.tg_proxy_port}",
                       "https": f"http://{host_part}:{self.conf.tg_proxy_port}"}
        resp = await get_client().post(url=url, headers=headers, params=payload, proxies=proxies)
        resp_data = resp.json()
        if resp_data["ok"]:
            logger.info("tg 推送成功！")
        else:
            raise RuntimeError(f"tg 推送失败：{resp_data}")

    async def aibotk(self, title: str, content: str) -> None:
        if not self.conf.aibotk_key or not self.conf.aibotk_type or not self.conf.aibotk_name:
            return
        logger.info("智能微秘书 服务启动")
        if self.conf.aibotk_type == "room":
            url = "https://api-bot.aibotk.com/openapi/v1/chat/room"
            data = {"apiKey": self.conf.aibotk_key, "roomName": self.conf.aibotk_name,
                    "message": {"type": 1, "content": f"【青龙快讯】\n\n{title}\n{content}"}}
        else:
            url = "https://api-bot.aibotk.com/openapi/v1/chat/contact"
            data = {"apiKey": self.conf.aibotk_key, "name": self.conf.aibotk_name,
                    "message": {"type": 1, "content": f"【青龙快讯】\n\n{title}\n{content}"}}
        body = json.dumps(data).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        resp = await get_client().post(url=url, data=body, headers=headers)
        resp_data = resp.json()
        if resp_data["code"] == 0:
            logger.info("智能微秘书 推送成功！")
        else:
            raise RuntimeError(f'智能微秘书 推送失败：url={url} body={body.decode("utf-8")} resp={resp_data["error"]}')

    def smtp(self, title: str, content: str) -> None:
        if (not self.conf.smtp_server or not self.conf.smtp_ssl or not self.conf.smtp_email
                or not self.conf.smtp_password or not self.conf.smtp_name):
            return
        logger.info("SMTP 邮件 服务启动")
        message = MIMEText(content, "plain", "utf-8")
        message["From"] = formataddr((Header(self.conf.smtp_name, "utf-8").encode(), self.conf.smtp_email))
        message["To"] = formataddr((Header(self.conf.smtp_name, "utf-8").encode(), self.conf.smtp_email))
        message["Subject"] = Header(title, "utf-8")
        try:
            # 兼容 smtp_server 是否带端口（如 "smtp.163.com:465"）
            host, _, port_str = self.conf.smtp_server.rpartition(":")
            if port_str.isdigit():
                port = int(port_str)
                host = host or self.conf.smtp_server
            else:
                host = self.conf.smtp_server
                port = 465 if self.conf.smtp_ssl == "true" else 25
            conn = (smtplib.SMTP_SSL(host, port)
                    if self.conf.smtp_ssl == "true" else smtplib.SMTP(host, port))
            conn.login(self.conf.smtp_email, self.conf.smtp_password)
            conn.sendmail(self.conf.smtp_email, self.conf.smtp_email, message.as_bytes())
            conn.close()
            logger.info("SMTP 邮件 推送成功！")
        except Exception as e:
            raise RuntimeError(f"SMTP 邮件 推送失败：host={host} port={port} {e}")

    async def pushme(self, title: str, content: str) -> None:
        if not self.conf.pushme_key:
            return
        logger.info("PushMe 服务启动")
        url = self.conf.pushme_url or "https://push.i-i.me/"
        data = {
            "push_key": self.conf.pushme_key,
            "title": title,
            "content": content,
            "date": "",
            "type": self.push_type or "",
        }
        resp = await get_client().post(url, data=data)
        if resp.status_code == 200 and resp.text == "success":
            logger.info("PushMe 推送成功！")
        else:
            raise RuntimeError(f"PushMe 推送失败：url={url} body={data} resp={resp.status_code} {resp.text}")

    async def chronocat(self, title: str, content: str) -> None:
        if not self.conf.chronocat_url or not self.conf.chronocat_qq or not self.conf.chronocat_token:
            return
        logger.info("CHRONOCAT 服务启动")
        user_ids = re.findall(r"user_id=(\d+)", self.conf.chronocat_qq)
        group_ids = re.findall(r"group_id=(\d+)", self.conf.chronocat_qq)
        url = f"{self.conf.chronocat_url}/api/message/send"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.conf.chronocat_token}"}
        for chat_type, ids in [(1, user_ids), (2, group_ids)]:
            if not ids:
                continue
            for chat_id in ids:
                data = {"peer": {"chatType": chat_type, "peerUin": chat_id},
                        "elements": [{"elementType": 1, "textElement": {"content": f"{title}\n\n{content}"}}]}
                resp = await get_client().post(url, headers=headers, data=json.dumps(data))
                if resp.status_code == 200:
                    logger.info(f"QQ{'个人' if chat_type == 1 else '群'}消息:{ids}推送成功！")
                else:
                    raise RuntimeError(f"QQ{'个人' if chat_type == 1 else '群'}消息:{ids}推送失败：url={url} body={json.dumps(data, ensure_ascii=False)} resp={resp.text}")

    async def ntfy(self, title: str, content: str) -> None:
        if not self.conf.ntfy_topic:
            return
        logger.info("ntfy 服务启动")

        def encode_rfc2047(text: str) -> str:
            return f"=?utf-8?B?{base64.b64encode(text.encode('utf-8')).decode('utf-8')}?="

        priority = self.conf.ntfy_priority or "3"
        encoded_title = encode_rfc2047(title)
        data = content.encode("utf-8")
        headers = {"Title": encoded_title, "Priority": priority,
                   "Icon": "https://qn.whyour.cn/logo.png"}
        if self.conf.ntfy_token:
            headers["Authorization"] = "Bearer " + self.conf.ntfy_token
        elif self.conf.ntfy_username and self.conf.ntfy_password:
            auth = base64.b64encode(f"{self.conf.ntfy_username}:{self.conf.ntfy_password}".encode()).decode()
            headers["Authorization"] = "Basic " + auth
        if self.conf.ntfy_actions:
            headers["Actions"] = encode_rfc2047(self.conf.ntfy_actions)
        url = (self.conf.ntfy_url or "https://ntfy.sh") + "/" + self.conf.ntfy_topic
        resp = await get_client().post(url, data=data, headers=headers)
        if resp.status_code == 200:
            logger.info("Ntfy 推送成功！")
        else:
            raise RuntimeError(f"Ntfy 推送失败：url={url} headers={headers} body={data} resp={resp.text}")

    async def wxpusher_bot(self, title: str, content: str) -> None:
        if not self.conf.wxpusher_app_token:
            return
        logger.info("wxpusher 服务启动")
        url = "https://wxpusher.zjiecode.com/api/send/message"
        topic_ids = [int(i.strip()) for i in self.conf.wxpusher_topic_ids.split(";") if i.strip()] \
            if self.conf.wxpusher_topic_ids else []
        uids = [u.strip() for u in self.conf.wxpusher_uids.split(";") if u.strip()] \
            if self.conf.wxpusher_uids else []
        if not topic_ids and not uids:
            raise RuntimeError("wxpusher 服务的 WXPUSHER_TOPIC_IDS 和 WXPUSHER_UIDS 至少设置一个!!")
        data = {
            "appToken": self.conf.wxpusher_app_token,
            "content": f"<h1>{title}</h1><br/><div style='white-space: pre-wrap;'>{content}</div>",
            "summary": title, "contentType": 2,
            "topicIds": topic_ids, "uids": uids, "verifyPayType": 0,
        }
        headers = {"Content-Type": "application/json"}
        resp = await get_client().post(url=url, json=data, headers=headers)
        resp_data = resp.json()
        if resp_data.get("code") == 1000:
            logger.info("wxpusher 推送成功！")
        else:
            raise RuntimeError(f"wxpusher 推送失败：url={url} body={json.dumps(data, ensure_ascii=False)} resp={resp_data.get('msg')}")

    async def webhook(self, title: str, content: str) -> None:
        if not self.conf.webhook_url:
            return
        logger.info("自定义 Webhook 服务启动")
        url = self.conf.webhook_url
        method = (self.conf.webhook_method or "POST").upper()
        ctype = self.conf.webhook_content_type or "application/json"
        headers = {"Content-Type": ctype}
        try:
            if self.conf.webhook_headers:
                for line in self.conf.webhook_headers.split("\n"):
                    if ":" in line:
                        k, v = line.split(":", 1)
                        headers[k.strip()] = v.strip()
        except Exception as e:
            logger.error(f"解析 webhook headers 失败: {e}")
        body = self.conf.webhook_body or json.dumps({"title": title, "content": content})
        if ctype == "application/json":
            try:
                body = body.replace("{{title}}", title).replace("{{content}}", content)
            except Exception:
                pass
        if method == "GET":
            resp = await get_client().get(url, headers=headers, params=json.loads(body) if ctype == "application/json" else None)
        else:
            if ctype == "application/json":
                resp = await get_client().post(url, headers=headers, content=body)
            else:
                resp = await get_client().post(url, headers=headers, data=body)
        if resp.status_code < 400:
            logger.info("自定义 Webhook 推送成功！")
        else:
            raise RuntimeError(f"自定义 Webhook 推送失败：url={url} body={body} resp={resp.status_code} {resp.text}")

    # ---------- 分发逻辑（降级链） ----------

    def get_available_methods(self):
        methods = []
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if not name.startswith("_") and name not in ("get_available_methods", "send", "_is_enabled"):
                methods.append(name)
        return methods

    def _is_enabled(self, name: str) -> bool:
        c = self.conf
        return {
            "bark": bool(c.bark_push),
            "dingding_bot": bool(c.dd_bot_token and c.dd_bot_secret),
            "feishu_bot": bool(c.fskey),
            "go_cqhttp": bool(c.gobot_url and c.gobot_qq),
            "gotify": bool(c.gotify_url and c.gotify_token),
            "iGot": bool(c.igot_push_key),
            "serverJ": bool(c.push_key),
            "pushdeer": bool(c.deer_key),
            "chat": bool(c.chat_url and c.chat_token),
            "pushplus_bot": bool(c.push_plus_token),
            "weplus_bot": bool(c.we_plus_bot_token),
            "qmsg_bot": bool(c.qmsg_key and c.qmsg_type),
            "wecom_app": bool(c.qywx_am),
            "wecom_bot": bool(c.qywx_key),
            "telegram_bot": bool(c.tg_bot_token and c.tg_user_id),
            "aibotk": bool(c.aibotk_key and c.aibotk_type and c.aibotk_name),
            "smtp": bool(c.smtp_server and c.smtp_ssl and c.smtp_email
                         and c.smtp_password and c.smtp_name),
            "pushme": bool(c.pushme_key),
            "chronocat": bool(c.chronocat_url and c.chronocat_qq and c.chronocat_token),
            "ntfy": bool(c.ntfy_topic),
            "wxpusher_bot": bool(c.wxpusher_app_token
                                 and (c.wxpusher_topic_ids or c.wxpusher_uids)),
            "webhook": bool(c.webhook_url),
        }.get(name, False)

    async def send(self, title: str, content: str) -> bool:
        """按 FALLBACK_ORDER 顺序降级推送，直到有一个渠道成功。

        Returns:
            True  : 至少有一个渠道推送成功
            False : 没有任何已启用的推送渠道（未尝试）
        Raises:
            RuntimeError: 所有已启用渠道均推送失败（彻底失败）
        """
        if not content:
            logger.warning(f"{title} 推送内容为空！")
            return False
        if self.conf.hitokoto:
            content += "\n\n" + await one()

        enabled = [m for m in self.get_available_methods() if self._is_enabled(m)]
        if not enabled:
            logger.warning("无可用的推送渠道，已跳过（请检查通知配置）")
            return False

        ordered = sorted(
            enabled,
            key=lambda m: FALLBACK_ORDER.index(m) if m in FALLBACK_ORDER else len(FALLBACK_ORDER),
        )
        logger.info(f"开始降级推送，渠道顺序：{ordered}")

        last_exc: Exception | None = None
        for name in ordered:
            method = getattr(self, name)
            try:
                if inspect.iscoroutinefunction(method):
                    await method(title, content)
                else:
                    await asyncio.get_event_loop().run_in_executor(None, method, title, content)
                logger.info(f"推送成功（渠道：{name}）")
                return True
            except Exception as e:  # noqa: BLE001
                last_exc = e
                logger.error(f"渠道 {name} 推送失败，尝试下一种：{e}")

        logger.critical(
            f"【彻底推送失败】所有渠道均失败 title={title}\n"
            f"最后错误：{last_exc}\n{traceback.format_exc()}"
        )
        raise RuntimeError(f"所有推送渠道均推送失败: {last_exc}")


async def one() -> str:
    """获取一条一言。"""
    try:
        res = await get_client().get(url="https://v1.hitokoto.cn/?c=a&c=b&c=c&c=f&c=g")
        res = res.json()
        return res.get("hitokoto", "") + "    ----" + res.get("from", "")
    except Exception as e:
        logger.warning(f"获取一言失败: {e}")
        return ""


async def send(title: str, content: str, conf: PushChannelConfig, push_type: str | None = None):
    """发送推送消息的全局函数接口。"""
    service = PushMessageService(conf, push_type=push_type)
    await service.send(title, content)
