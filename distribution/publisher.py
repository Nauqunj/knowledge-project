#!/usr/bin/env python3
"""知识速递发布器 — 把每日速递并发推送到 Telegram / 飞书。

纯 OOP 架构，异步实现：

    BasePublisher   抽象基类，定义 send_message() / send_digest() 接口
    TelegramPublisher  通过 Telegram Bot API 发送 MarkdownV2 消息
    FeishuPublisher    通过飞书 Webhook 发送 interactive 卡片
    PublishResult   单次发布结果数据类
    publish_daily_digest()  统一异步入口，生成三种格式并并发发布

密钥来源（优先级从高到低）：
    构造参数 > ``distribution/config.json`` > 环境变量

配置文件样例见 ``distribution/config.example.json``；真实的
``config.json`` 已加入 .gitignore。

环境变量：
    TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID
    FEISHU_APP_ID / FEISHU_APP_SECRET
    FEISHU_CHAT_ID（接收方 ID，或 FEISHU_RECEIVE_ID）
    FEISHU_RECEIVE_ID_TYPE（默认 chat_id）

依赖：``aiohttp``
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from distribution.config import channel_config  # noqa: E402
from distribution.formatter import (  # noqa: E402
    DEFAULT_DIGEST_TOP_N,
    DEFAULT_KNOWLEDGE_DIR,
    generate_daily_digest,
)

DEFAULT_TIMEOUT = 30.0
TELEGRAM_API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"

FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
FEISHU_TOKEN_PATH = "/auth/v3/tenant_access_token/internal"
FEISHU_MESSAGE_PATH = "/im/v1/messages"
FEISHU_TOKEN_REFRESH_MARGIN = 60


def _pick(
    explicit: str | None,
    section: dict[str, Any],
    key: str,
    env_name: str,
) -> str:
    """按「显式参数 > 配置文件 > 环境变量」取值。

    Args:
        explicit: 构造函数传入的显式值。
        section: 渠道配置段（来自 ``distribution/config.json``）。
        key: 配置段内的键名。
        env_name: 兜底的环境变量名。

    Returns:
        去除首尾空白后的值；全空时返回空串。
    """
    value = (explicit or "").strip()
    if value:
        return value
    config_value = section.get(key)
    if config_value not in (None, ""):
        return str(config_value).strip()
    return os.getenv(env_name, "").strip()


def _card_of(item: dict[str, Any]) -> dict[str, Any]:
    """从 formatter 产物中取出飞书卡片对象。

    Args:
        item: :func:`json_to_feishu` 返回的
            ``{"msg_type": "interactive", "card": {...}}``，或直接的卡片。

    Returns:
        卡片对象；取不到时回退为 ``item`` 本身。
    """
    card = item.get("card")
    return card if isinstance(card, dict) else item


def _utc_now() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PublishResult:
    """单次发布的结果。

    Attributes:
        channel: 渠道名，``telegram`` / ``feishu``。
        success: 是否全部发送成功。
        message_id: 平台返回的消息 ID；多消息时为逗号分隔。
        error: 失败原因；成功时为 ``None``。
        count: 本次发送的消息条数。
        sent_at: 发送时间（ISO 8601，UTC）。
    """

    channel: str
    success: bool
    message_id: str = ""
    error: str | None = None
    count: int = 0
    sent_at: str = field(default_factory=_utc_now)

    def to_dict(self) -> dict[str, Any]:
        """转成可序列化的 dict。

        Returns:
            字段与数据类一致、便于日志/JSON 落盘的 dict。
        """
        return {
            "channel": self.channel,
            "success": self.success,
            "message_id": self.message_id,
            "error": self.error,
            "count": self.count,
            "sent_at": self.sent_at,
        }


class BasePublisher(ABC):
    """发布器抽象基类。

    Attributes:
        channel: 渠道标识。
        timeout: 单次请求超时秒数。
    """

    channel: str = "base"

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        """初始化发布器。

        Args:
            timeout: 单次 HTTP 请求超时秒数，默认 30。
        """
        self.timeout = timeout

    @abstractmethod
    def is_configured(self) -> bool:
        """判断该渠道所需的环境变量是否齐备。

        Returns:
            配置完整返回 ``True``，否则 ``False``。
        """

    @abstractmethod
    async def send_message(self, text: str) -> PublishResult:
        """发送一条纯文本/渠道格式的消息。

        Args:
            text: 已按渠道要求格式化好的消息内容。

        Returns:
            该次发布的结果。
        """

    @abstractmethod
    async def send_digest(self, digest: dict[str, Any]) -> PublishResult:
        """发送一份多渠道速递。

        Args:
            digest: :func:`generate_daily_digest` 返回的 dict。

        Returns:
            该次发布的结果。
        """


class TelegramPublisher(BasePublisher):
    """Telegram Bot 发布器（MarkdownV2）。

    Attributes:
        bot_token: Bot Token，来自构造参数 / 配置文件 / 环境变量。
        chat_id: 目标会话，来自构造参数 / 配置文件 / 环境变量。
    """

    channel = "telegram"

    def __init__(
        self,
        bot_token: str | None = None,
        chat_id: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化 Telegram 发布器。

        Args:
            bot_token: Bot Token；缺省时读配置文件的
                ``telegram.bot_token``，再读 ``TELEGRAM_BOT_TOKEN``。
            chat_id: 目标会话 ID；缺省时读配置文件的
                ``telegram.chat_id``，再读 ``TELEGRAM_CHAT_ID``。
            timeout: 请求超时秒数。
        """
        super().__init__(timeout)
        section = channel_config("telegram")
        self.bot_token = _pick(
            bot_token, section, "bot_token", "TELEGRAM_BOT_TOKEN"
        )
        self.chat_id = _pick(chat_id, section, "chat_id", "TELEGRAM_CHAT_ID")

    def is_configured(self) -> bool:
        """是否已配置 Bot Token 与 Chat ID。"""
        return bool(self.bot_token and self.chat_id)

    async def send_message(self, text: str) -> PublishResult:
        """通过 ``sendMessage`` 发送 MarkdownV2 文本。

        Args:
            text: MarkdownV2 文本（特殊字符须已转义）。

        Returns:
            发布结果；网络或 API 报错时 ``success`` 为 ``False``。
        """
        if not self.is_configured():
            return PublishResult(
                self.channel,
                False,
                error="缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID",
            )

        import aiohttp

        url = TELEGRAM_API_TEMPLATE.format(token=self.bot_token)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or not data.get("ok"):
                        detail = data.get("description") or (
                            f"HTTP {response.status}"
                        )
                        return PublishResult(self.channel, False, error=detail)
                    result = data.get("result") or {}
                    message_id = str(result.get("message_id", ""))
                    return PublishResult(
                        self.channel, True, message_id=message_id, count=1
                    )
        except asyncio.TimeoutError:
            return PublishResult(self.channel, False, error="请求超时")
        except aiohttp.ClientError as exc:
            return PublishResult(self.channel, False, error=str(exc))

    async def send_digest(self, digest: dict[str, Any]) -> PublishResult:
        """发送速递的 Telegram 文本部分。

        Args:
            digest: :func:`generate_daily_digest` 的返回 dict。

        Returns:
            发布结果。
        """
        text = str(digest.get("telegram", "")).strip()
        if not text:
            return PublishResult(self.channel, False, error="速递为空")
        return await self.send_message(text)


class FeishuPublisher(BasePublisher):
    """飞书企业自建应用机器人发布器（interactive 卡片）。

    通过 ``app_id`` / ``app_secret`` 换取 ``tenant_access_token``，再调用
    消息接口发送。Token 缓存至过期前 60 秒。

    Attributes:
        app_id: 应用 App ID，来自 ``FEISHU_APP_ID``。
        app_secret: 应用 App Secret，来自 ``FEISHU_APP_SECRET``。
        receive_id: 接收方 ID，来自 ``FEISHU_CHAT_ID`` 或
            ``FEISHU_RECEIVE_ID``。
        receive_id_type: 接收方 ID 类型，默认 ``chat_id``。
    """

    channel = "feishu"

    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        receive_id: str | None = None,
        receive_id_type: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """初始化飞书应用机器人发布器。

        Args:
            app_id: 应用 App ID；缺省读配置 ``feishu.app_id``，再读
                ``FEISHU_APP_ID``。
            app_secret: 应用 App Secret；缺省读配置 ``feishu.app_secret``，
                再读 ``FEISHU_APP_SECRET``。
            receive_id: 接收方 ID；缺省读配置 ``feishu.receive_id``，再读
                ``FEISHU_CHAT_ID`` / ``FEISHU_RECEIVE_ID``。
            receive_id_type: 接收方类型；缺省读配置
                ``feishu.receive_id_type``，再读 ``FEISHU_RECEIVE_ID_TYPE``，
                最后回退 ``chat_id``。
            timeout: 请求超时秒数。
        """
        super().__init__(timeout)
        section = channel_config("feishu")
        self.app_id = _pick(app_id, section, "app_id", "FEISHU_APP_ID")
        self.app_secret = _pick(
            app_secret, section, "app_secret", "FEISHU_APP_SECRET"
        )
        self.receive_id = _pick(
            receive_id, section, "receive_id", "FEISHU_CHAT_ID"
        ) or os.getenv("FEISHU_RECEIVE_ID", "").strip()
        self.receive_id_type = (
            _pick(
                receive_id_type,
                section,
                "receive_id_type",
                "FEISHU_RECEIVE_ID_TYPE",
            )
            or "chat_id"
        )
        self._token = ""
        self._token_expire_at = 0.0
        self._token_lock = asyncio.Lock()

    def is_configured(self) -> bool:
        """是否已配置 App 凭证与接收方 ID。"""
        return bool(self.app_id and self.app_secret and self.receive_id)

    async def send_message(self, text: str) -> PublishResult:
        """以纯文本消息发送。

        Args:
            text: 消息正文。

        Returns:
            发布结果。
        """
        return await self._post_message("text", {"text": text})

    async def send_digest(self, digest: dict[str, Any]) -> PublishResult:
        """并发发送速递中的每一张卡片。

        Args:
            digest: :func:`generate_daily_digest` 的返回 dict；其
                ``feishu`` 字段为卡片消息列表。

        Returns:
            聚合结果；只要有一张失败即 ``success`` 为 ``False``。
        """
        cards = digest.get("feishu") or []
        if not cards:
            return PublishResult(self.channel, False, error="速递为空")

        results = await asyncio.gather(
            *(
                self._post_message("interactive", _card_of(item))
                for item in cards
            )
        )
        ids = [
            item.message_id
            for item in results
            if item.success and item.message_id
        ]
        errors = [
            item.error
            for item in results
            if not item.success and item.error
        ]
        return PublishResult(
            self.channel,
            success=not errors,
            message_id=",".join(ids),
            error="; ".join(errors) if errors else None,
            count=len(results),
        )

    async def _ensure_token(self) -> str:
        """获取并缓存 ``tenant_access_token``。

        Returns:
            有效的 tenant_access_token。

        Raises:
            RuntimeError: 网络异常或接口返回码非 0。
        """
        now = time.monotonic()
        if self._token and now < self._token_expire_at:
            return self._token

        async with self._token_lock:
            now = time.monotonic()
            if self._token and now < self._token_expire_at:
                return self._token

            import aiohttp

            url = f"{FEISHU_API_BASE}{FEISHU_TOKEN_PATH}"
            body = {"app_id": self.app_id, "app_secret": self.app_secret}
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(url, json=body) as response:
                        status = response.status
                        data = await response.json(content_type=None)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("获取 tenant_access_token 超时") from exc
            except aiohttp.ClientError as exc:
                raise RuntimeError(
                    f"获取 tenant_access_token 失败: {exc}"
                ) from exc
            except json.JSONDecodeError as exc:
                raise RuntimeError("token 响应不是合法 JSON") from exc

            if status != 200 or data.get("code") != 0:
                detail = data.get("msg") or f"HTTP {status}"
                raise RuntimeError(f"获取 tenant_access_token 失败: {detail}")

            self._token = str(data.get("tenant_access_token", ""))
            expire = int(data.get("expire", 7200) or 7200)
            self._token_expire_at = time.monotonic() + max(
                0, expire - FEISHU_TOKEN_REFRESH_MARGIN
            )
            return self._token

    async def _post_message(
        self, msg_type: str, content: dict[str, Any]
    ) -> PublishResult:
        """调用消息接口发送一条消息。

        Args:
            msg_type: ``text`` 或 ``interactive``。
            content: 消息内容对象，会序列化为 JSON 字符串放入 ``content``。

        Returns:
            发布结果。
        """
        if not self.is_configured():
            return PublishResult(
                self.channel,
                False,
                error="缺少 FEISHU_APP_ID/SECRET 或接收方 ID",
            )

        import aiohttp

        try:
            token = await self._ensure_token()
        except RuntimeError as exc:
            return PublishResult(self.channel, False, error=str(exc))

        url = (
            f"{FEISHU_API_BASE}{FEISHU_MESSAGE_PATH}"
            f"?receive_id_type={self.receive_id_type}"
        )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        body = {
            "receive_id": self.receive_id,
            "msg_type": msg_type,
            "content": json.dumps(content, ensure_ascii=False),
        }
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    url, json=body, headers=headers
                ) as response:
                    data = await response.json(content_type=None)
                    if response.status != 200 or data.get("code") != 0:
                        detail = data.get("msg") or f"HTTP {response.status}"
                        return PublishResult(
                            self.channel, False, error=detail
                        )
                    result = data.get("data") or {}
                    message_id = str(result.get("message_id", ""))
                    return PublishResult(
                        self.channel, True, message_id=message_id, count=1
                    )
        except asyncio.TimeoutError:
            return PublishResult(self.channel, False, error="请求超时")
        except aiohttp.ClientError as exc:
            return PublishResult(self.channel, False, error=str(exc))
        except json.JSONDecodeError:
            return PublishResult(
                self.channel, False, error="响应不是合法 JSON"
            )


def default_publishers() -> list[BasePublisher]:
    """构造默认的全部渠道发布器。

    Returns:
        包含 :class:`TelegramPublisher` 与 :class:`FeishuPublisher` 的列表。
    """
    return [TelegramPublisher(), FeishuPublisher()]


async def publish_daily_digest(
    knowledge_dir: str | Path = DEFAULT_KNOWLEDGE_DIR,
    date: str | None = None,
    top_n: int = DEFAULT_DIGEST_TOP_N,
    publishers: Sequence[BasePublisher] | None = None,
    digest: dict[str, Any] | str | None = None,
) -> list[PublishResult]:
    """生成每日速递并并发发布到所有已配置渠道。

    Args:
        knowledge_dir: 知识条目目录，默认 ``knowledge/articles``。
        date: ``YYYY-MM-DD``；默认取 UTC 当天。
        top_n: 速递保留的条数上限。
        publishers: 自定义发布器列表；默认使用 :func:`default_publishers`。
        digest: 已构建的速递内容（如经质量过滤后由
            :func:`~distribution.formatter.build_digest` 生成）。给定时
            跳过 ``knowledge_dir`` / ``date`` / ``top_n`` 的自动生成。

    Returns:
        每个已配置渠道一条 :class:`PublishResult`；无可用渠道时返回空列表。
        单渠道异常会被隔离为该渠道的失败结果，不影响其它渠道。
    """
    if digest is None:
        digest = generate_daily_digest(
            knowledge_dir=knowledge_dir, date=date, top_n=top_n
        )
    all_publishers = list(
        publishers if publishers is not None else default_publishers()
    )
    active = [
        publisher
        for publisher in all_publishers
        if publisher.is_configured()
    ]
    if not active:
        return []

    if isinstance(digest, str):
        tasks = [publisher.send_message(digest) for publisher in active]
    else:
        tasks = [publisher.send_digest(digest) for publisher in active]

    raw = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[PublishResult] = []
    for publisher, item in zip(active, raw):
        if isinstance(item, PublishResult):
            results.append(item)
        else:
            results.append(
                PublishResult(
                    publisher.channel,
                    False,
                    error=f"{type(item).__name__}: {item}",
                )
            )
    return results


async def _main() -> None:
    """手动运行：发布当天速递并打印每个渠道的结果。"""
    results = await publish_daily_digest()
    if not results:
        print("没有已配置的发布渠道（检查环境变量）。")
        return
    for result in results:
        status = "OK" if result.success else "FAIL"
        print(
            f"[{status}] {result.channel} "
            f"count={result.count} id={result.message_id} "
            f"error={result.error}"
        )


if __name__ == "__main__":
    asyncio.run(_main())
